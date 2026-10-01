"""M4: hybrid search, reranking, the groundedness check and the diagnosis pipeline (no real LLM: scripted stand-ins)."""
import json, pathlib, sys, tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_memory import WordEmbedder, burst, run   # noqa: E402

from copilot.diagnosis import Diagnosis, Verdict, check_groundedness, diagnose, incident_text
from copilot.kb import evaluate
from copilot.kb.index import KnowledgeIndex
from copilot.memory import Memory


@pytest.fixture(scope="module")
def index():
    idx = KnowledgeIndex(Memory(pathlib.Path(tempfile.mkdtemp()) / "m.sqlite").db, embedder=WordEmbedder())
    idx.rebuild()
    return idx


# ---- retrieval --------------------------------------------------------------------------------------

def test_keyword_search_finds_exact_error_strings(index):
    top = index.search("No space left on device", k=1, mode="keyword")[0]
    assert top["path"] == "runbooks/disk-full.md"


def test_keyword_baseline_on_the_eval_set(index):
    """Regression floor. Keyword alone misses the paraphrased questions: that's why hybrid is the default."""
    r = evaluate.retrieval(index, modes=("keyword",))["modes"]["keyword"]
    assert r["recall@k"] >= 0.84
    paraphrases = {q["q"] for q in json.loads((evaluate.EVAL_DIR / "retrieval.json").read_text())["questions"]
                   if q.get("paraphrase")}
    assert set(r["missed"]) <= paraphrases        # keyword only fails where the wording differs


def test_hybrid_fuses_both_rankings(index):
    kw = [h["chunk_id"] for h in index.search("guest agent failed to remove user", k=3, mode="keyword")]
    hy = [h["chunk_id"] for h in index.search("guest agent failed to remove user", k=3, mode="hybrid")]
    assert hy and set(hy) & set(kw)


class OrderLLM:
    """Scripted reranker: returns the given order (can include ids that don't exist)."""
    def __init__(self, order): self.order = order
    def with_structured_output(self, schema):
        outer = self
        class R:
            def invoke(self, prompt): return schema(chunk_ids=outer.order)
        return R()


def test_reranker_order_is_applied_and_unknown_ids_ignored(index):
    base = index.search("database unreachable", k=5, mode="keyword")
    want = base[3]["chunk_id"]
    out = index.search("database unreachable", k=5, mode="keyword", reranker=OrderLLM(["made-up#x", want]))
    assert out[0]["chunk_id"] == want and len(out) == 5 and "made-up#x" not in [h["chunk_id"] for h in out]


# ---- groundedness -----------------------------------------------------------------------------------

CASES = json.loads((evaluate.EVAL_DIR / "groundedness.json").read_text())["cases"]


class ScriptedJudge:
    """Stands in for the LLM judge: rejects exactly the claims the eval case marks as planted."""
    def __init__(self, bad: set[str]): self.bad = bad
    def with_structured_output(self, schema):
        outer = self
        class J:
            def invoke(self, prompt):
                claim = prompt.split("CLAIM: ", 1)[1].split("\n", 1)[0]
                return Verdict(supported=not any(b in claim for b in outer.bad), reason="scripted")
        return J()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_planted_fabrications_are_removed_and_clean_ones_kept(case, index):
    retrieved = {c: index.get(c) for c in case["retrieved"]}
    assert all(retrieved.values()), "eval case cites a section that isn't in the knowledge base"
    judge = None
    if case.get("needs_judge"):
        d = case["diagnosis"]
        where = case["expect_removed"][0]
        bad = d["root_cause"]["text"] if where == "root cause" else d["steps"][int(where.split()[1]) - 1]["text"]
        judge = ScriptedJudge({bad[:40]})
    g = check_groundedness(Diagnosis(**case["diagnosis"]), retrieved, judge=judge, incident=case["incident"])
    assert sorted(r.where for r in g.removed) == sorted(case["expect_removed"]), [r.problem for r in g.removed]
    assert g.grounded == ("root cause" not in case["expect_removed"])


def test_rules_alone_catch_every_structural_fabrication(index):
    g = evaluate.groundedness(index, judge=None)
    assert g["catch_rate"] == 1.0 and g["false_removals"] == 0


def test_no_grounded_root_cause_means_escalate(index):
    d = Diagnosis(summary="guess", confidence=0.9, root_cause={"text": "cosmic rays", "citations": []}, steps=[])
    g = check_groundedness(d, {}, judge=None)
    assert not g.grounded and g.status.startswith("insufficient knowledge") and g.diagnosis.confidence <= 0.2


# ---- the pipeline -----------------------------------------------------------------------------------

class DraftLLM:
    """Scripted diagnosis writer: cites the first retrieved section it's shown, plus one it made up."""
    def __init__(self): self.prompt = None
    def with_structured_output(self, schema):
        outer = self
        class W:
            def invoke(self, prompt):
                outer.prompt = prompt
                ids = [l[1:-1] for l in prompt.splitlines() if l.startswith("[") and l.endswith("]")]
                ga = next(i for i in ids if i.startswith("runbooks/guest-agent-errors.md#cause"))
                return schema(summary="Known guest agent noise.", confidence=0.8,
                              root_cause={"text": "Stale local users; the agent cannot remove them.", "citations": [ga]},
                              steps=[{"text": "No action.", "action": "none", "citations": [ga]},
                                     {"text": "Reboot it.", "action": "vm.reset", "citations": ["runbooks/made-up.md#x"]}])
        return W()


def test_diagnose_retrieves_drafts_and_checks(index, tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    run(burst("mon", 0), m)
    case = run(burst("tue", 1440), m).open_cases()[0]
    llm = DraftLLM()
    g = diagnose(case, index, llm, judge=None, memory=m)
    assert g.grounded and [s.action for s in g.diagnosis.steps] == ["none"]
    assert g.removed[0].where == "step 2" and "not retrieved" in g.removed[0].problem     # the made-up citation
    assert "Similar past incidents" in llm.prompt and "1.0 similar" in llm.prompt         # memory feeds the draft
    assert "guest-agent-errors.md" in " ".join(g.sources)


def test_incident_text_lists_each_condition_once(tmp_path):
    case = run(burst("a", 0), Memory(tmp_path / "m.sqlite")).open_cases()[0]
    t = incident_text(case)
    assert t.count("GCEGuestAgent") == 1 and "[context]" in t


def test_render_shows_each_citation_once():
    from copilot.diagnosis import GroundedDiagnosis, render
    cite = "runbooks/guest-agent-errors.md#cause"
    d = Diagnosis(summary=f"Known noise. [{cite}]", confidence=0.9,
                  root_cause={"text": f"Stale users. [{cite}][postmortems/2026-09-guest-agent-noise.md#root-cause]",
                              "citations": [cite]}, steps=[])
    out = render(GroundedDiagnosis(diagnosis=d, removed=[], grounded=True, sources=[cite]))
    assert out.count(cite) == 1 and "Stale users.  [" in out


# ---- applicability (novel incidents, queued fix) ---------------------------------------------------------------

def test_documents_declare_what_they_apply_to():
    from copilot.diagnosis import applies_to
    assert applies_to("runbooks/vm-stopped.md") == "all"
    assert applies_to("runbooks/third-party-outage.md") == {"payments-provider", "storefront"}
    assert applies_to("runbooks/k8s-pod-oomkilled.md") == set()            # decoy: none of our services


def test_every_kb_document_has_an_applies_to_line():
    import re
    from copilot.kb.index import KB_DIR
    for p in KB_DIR.rglob("*.md"):
        assert re.search(r"^Applies to: .+", p.read_text(), re.M), p


def test_an_unknown_service_escalates_without_calling_the_llm(index):
    from copilot.kb.evaluate import novel_case
    class NoLLM:
        def with_structured_output(self, schema): raise AssertionError("LLM called")
    case = novel_case({"id": "x", "service": "session-cache", "host": "cache-01",
                       "titles": ["cache-01 redis: connection refused on cache-01:6379 (READONLY replica)"]})
    g = diagnose(case, index, NoLLM())
    assert not g.grounded and "no runbook or postmortem" in g.diagnosis.summary


def test_decoy_documents_are_never_offered_to_the_llm(index):
    from copilot.kb.evaluate import novel_case
    llm = DraftLLMAny()
    case = novel_case({"id": "k8s", "service": "storefront", "host": "web-01",
                       "titles": ["web-01 storefront: pod checkout-7d9f OOMKilled, container memory limit reached"]})
    diagnose(case, index, llm)
    assert "k8s-pod-oomkilled" not in llm.prompt and "cloud-sql" not in llm.prompt


class DraftLLMAny:
    """Records the prompt; answers with an ungrounded draft."""
    def with_structured_output(self, schema):
        outer = self
        class W:
            def invoke(self, prompt):
                outer.prompt = prompt
                return schema(summary="not covered", confidence=0.1, root_cause=None, steps=[])
        return W()
