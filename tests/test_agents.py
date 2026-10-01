"""M6 step 1: the Investigator and Recommender agents, and the structured handoff between them."""
import pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_memory import WordEmbedder, burst, run          # noqa: E402
from test_diagnosis import DraftLLM                       # noqa: E402
from test_workflow import _case, _step, DB_DOWN           # noqa: E402

from copilot import usage
from copilot.agents import Investigation, Investigator, Recommender
from copilot.diagnosis import Diagnosis, GroundedDiagnosis, to_record
from copilot.kb.index import KnowledgeIndex
from copilot.memory import Memory
from copilot.outbox import LocalNotifier, LocalTickets
from copilot.routing import RoutingPolicy, route
from copilot.workflow import Workflow


@pytest.fixture(autouse=True)
def usage_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PATH", tmp_path / "usage.json")


def grounded(step_text, action="vm.start", cite="runbooks/vm-stopped.md#remediation"):
    d = Diagnosis(summary="db-01 stopped", confidence=0.9, root_cause={"text": "VM stopped", "citations": [cite]},
                  steps=[{"text": step_text, "action": action, "citations": [cite]}])
    return GroundedDiagnosis(diagnosis=d, removed=[], grounded=True, sources=[cite])


class PickLLM:
    """Scripted recommender LLM: answers with the given resource and counts calls."""
    def __init__(self, answer): self.answer, self.calls = answer, 0
    def with_structured_output(self, schema):
        outer = self
        class R:
            def invoke(self, prompt):
                outer.calls += 1
                outer.prompt = prompt
                return schema(resource=outer.answer)
        return R()


PAYFAST_AND_DB = DB_DOWN + [{"id": "p", "minute": 1, "service": "payments-provider", "title": "PayFast outage",
                             "resource": "Card Authorization API"}]


# ---- Investigator -------------------------------------------------------------------------------------

def test_investigator_new_incident_drafts_with_the_llm_and_brings_history(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    idx = KnowledgeIndex(m.db, embedder=WordEmbedder()); idx.rebuild()
    run(burst("mon", 0, service="reports-batch", vm="batch-01"), m)          # an unrelated past case
    case = run(burst("tue", 1440), m).open_cases()[0]
    inv = Investigator(idx, DraftLLM(), memory=m).investigate(case)
    assert inv.diagnosis.grounded and inv.reused_from is None
    assert "GCEGuestAgent" in inv.evidence and inv.history == []            # nothing similar on storefront


def test_investigator_reuses_a_known_incident(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    past = m.save_case(run(burst("mon", 0), m).open_cases()[0])
    m.record(past, "diagnosis", to_record(grounded("No action.", "none", "runbooks/guest-agent-errors.md#cause")))
    m.record(past, "outcome", {"action": "none", "result": "closed: no action needed"})
    case = run(burst("tue", 1440), m).open_cases()[0]

    class NoLLM:
        def with_structured_output(self, s): raise AssertionError("LLM called for a known incident")
    inv = Investigator(None, NoLLM(), memory=m).investigate(case)
    assert inv.reused_from == past and inv.history[0]["case_id"] == past and inv.history[0]["score"] == 1.0


# ---- Recommender ---------------------------------------------------------------------------------------

def test_recommender_uses_no_llm_when_the_target_is_clear():
    llm = PickLLM("orders-01")
    case = _case(DB_DOWN)
    rec = Recommender(llm).recommend(case, Investigation(case_id="c", evidence="", diagnosis=grounded("Start the database VM db-01.")))
    assert (rec.target, rec.target_by, llm.calls) == ("db-01", "rules", 0)


def test_recommender_asks_the_llm_only_when_stuck_and_a_person_confirms_its_choice():
    llm = PickLLM("db-01")
    case = _case(PAYFAST_AND_DB)                                           # two origins: no root service
    inv = Investigation(case_id="c", evidence="db-01 stopped; PayFast outage", diagnosis=grounded("Start the stopped VM."))
    rec = Recommender(llm).recommend(case, inv)
    assert (rec.target, rec.service, rec.target_by, llm.calls) == ("db-01", "orders-db", "llm", 1)
    assert "db-01" in llm.prompt and "orders-01" in llm.prompt           # it chose among the case's own resources
    r = route(rec.model_copy(update={"blast_radius": "low"}), True, RoutingPolicy())
    assert r.lane == "approval" and "chosen by the LLM" in r.reason        # never auto, even at low blast radius


@pytest.mark.parametrize("answer", ["web-99", None, "db-01; also restart orders-01"])
def test_an_llm_answer_outside_the_case_resources_is_ignored_and_escalates(answer):
    case = _case(PAYFAST_AND_DB)
    inv = Investigation(case_id="c", evidence="", diagnosis=grounded("Start the stopped VM."))
    rec = Recommender(PickLLM(answer)).recommend(case, inv)
    assert rec.target is None and route(rec, True, RoutingPolicy()).lane == "escalate"


def test_no_llm_for_escalate_or_no_action():
    llm = PickLLM("db-01")
    case = _case(PAYFAST_AND_DB)
    for action in ("none", "escalate"):
        Recommender(llm).recommend(case, Investigation(case_id="c", evidence="", diagnosis=grounded("x", action)))
    assert llm.calls == 0


# ---- handoff in the workflow ---------------------------------------------------------------------------

def test_the_workflow_passes_the_investigation_to_the_recommender(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    cid = m.save_case(run(burst("x", 0), m).open_cases()[0])
    case = m.get(cid)
    seen = {}

    class StubInvestigator:
        def investigate(self, c):
            return Investigation(case_id=cid, evidence="EVIDENCE", diagnosis=grounded("No action.", "none",
                                                                                     "runbooks/guest-agent-errors.md#cause"))

    class SpyRecommender(Recommender):
        def recommend(self, c, inv):
            seen["inv"] = inv
            return super().recommend(c, inv)

    w = Workflow(m, LocalTickets(tmp_path / "t"), LocalNotifier(tmp_path / "n.jsonl"), path=tmp_path / "wf.sqlite",
                 investigator=StubInvestigator(), recommender=SpyRecommender())
    st = w.start(cid, case)
    assert isinstance(seen["inv"], Investigation) and seen["inv"].evidence == "EVIDENCE"
    assert st["investigation"]["evidence"] == "EVIDENCE" and st["status"] == "closed: no action needed"
    assert [t["step"] for t in st["trace"]][:3] == ["start", "investigate", "recommend"]
