"""Cost rule (29 Sep): Gemini only for what's new; repeats reuse a checked diagnosis; a daily budget stops calls."""
import pathlib, sys

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_memory import burst, run   # noqa: E402

from copilot import usage
from copilot.diagnosis import Diagnosis, GroundedDiagnosis, diagnose_or_reuse, to_record
from copilot.memory import Memory
from copilot.routing import RoutingPolicy, recommend, route


@pytest.fixture(autouse=True)
def usage_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PATH", tmp_path / "usage.json")
    monkeypatch.setenv("COPILOT_LLM_DAILY_BUDGET", "3")


class CountingLLM:
    """Stands in for Gemini; fails the test if the cost rule says it shouldn't have been called."""
    def __init__(self): self.calls = 0
    def with_structured_output(self, schema):
        self.calls += 1
        raise AssertionError("the LLM was called")


def grounded_record():
    cite = "runbooks/guest-agent-errors.md#cause"
    d = Diagnosis(summary="Known guest agent noise.", confidence=0.9, root_cause={"text": "stale users", "citations": [cite]},
                  steps=[{"text": "No action.", "action": "none", "citations": [cite]}])
    return to_record(GroundedDiagnosis(diagnosis=d, removed=[], grounded=True, sources=[cite]))


def test_a_known_incident_reuses_the_checked_diagnosis_without_calling_the_llm(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    past = m.save_case(run(burst("mon", 0), m).open_cases()[0])
    m.record(past, "diagnosis", grounded_record())
    m.record(past, "outcome", {"action": "none", "result": "closed: no action needed"})
    today = run(burst("tue", 1440), m).open_cases()[0]
    llm, steps = CountingLLM(), []
    g = diagnose_or_reuse(today, index=None, llm=llm, memory=m, progress=steps.append)
    assert llm.calls == 0 and g.grounded and g.diagnosis.summary.startswith(f"[same as {past}]")
    assert past in steps[0] and "no Gemini call" in steps[0]


def test_after_a_rejection_the_llm_thinks_again(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    past = m.save_case(run(burst("mon", 0), m).open_cases()[0])
    m.record(past, "diagnosis", grounded_record())
    m.record(past, "outcome", {"action": "none", "result": "closed: rejected by aryan"})
    today = run(burst("tue", 1440), m).open_cases()[0]
    with pytest.raises(AssertionError, match="LLM was called"):
        diagnose_or_reuse(today, index=_Index(), llm=CountingLLM(), memory=m)


class _Index:
    def documents_for(self, query, docs=3, reranker=None):
        return [{"chunk_id": "runbooks/guest-agent-errors.md#cause", "path": "runbooks/guest-agent-errors.md", "text": "x"}]


def test_every_chat_call_is_counted_and_the_budget_stops_the_fourth():
    llm = FakeListChatModel(responses=["ok"] * 5, callbacks=[usage.CountingCallback("diagnosis")])
    for _ in range(3):
        llm.invoke("hi")
    assert usage.today() == {"diagnosis": 3} and usage.spent() == 3
    with pytest.raises(usage.BudgetExceeded):
        llm.invoke("one too many")
    assert usage.spent() == 3                          # the refused call wasn't counted or made


def test_embeddings_are_counted_but_not_budgeted():
    class E:
        def embed_query(self, t): return [1.0]
        def embed_documents(self, ts): return [[1.0]] * len(ts)
    e = usage.CountedEmbeddings(E())
    e.embed_documents(["a", "b", "c", "d"])
    e.embed_query("q")
    assert usage.today()["embeddings"] == 5 and usage.spent() == 0


def test_out_of_budget_escalates_with_the_rule_based_information(tmp_path):
    for _ in range(3):
        usage.add("diagnosis")
    m = Memory(tmp_path / "m.sqlite")
    case = run(burst("x", 0), m).open_cases()[0]

    class BudgetedLLM:
        def with_structured_output(self, schema):
            outer = self
            class R:
                def invoke(self, prompt): usage.add("diagnosis")      # what the callback does on a real model
            return R()
    g = diagnose_or_reuse(case, index=_Index(), llm=BudgetedLLM(), memory=m)
    assert not g.grounded and "budget" in g.diagnosis.summary
    assert route(recommend(case, g), g.grounded, RoutingPolicy()).lane == "escalate"


def test_reused_summaries_do_not_stack_prefixes(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    rec = grounded_record()
    rec["diagnosis"]["summary"] = "[same as case-a] [same as case-b] Known guest agent noise."
    past = m.save_case(run(burst("mon", 0), m).open_cases()[0])
    m.record(past, "diagnosis", rec)
    m.record(past, "outcome", {"action": "none", "result": "no action needed"})
    g = diagnose_or_reuse(run(burst("tue", 1440), m).open_cases()[0], index=None, llm=CountingLLM(), memory=m)
    assert g.diagnosis.summary == f"[same as {past}] Known guest agent noise."
