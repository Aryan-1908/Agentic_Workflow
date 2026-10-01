"""M7: every step traced; failed calls retried, then escalated cleanly with the error attached."""
import json, pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_execution import Env, executor, grounded              # noqa: E402
from test_memory import burst, run                               # noqa: E402

from copilot import resilience, trace, usage
from copilot.memory import Memory
from copilot.outbox import LocalNotifier, LocalTickets
from copilot.routing import recommend
from copilot.workflow import Workflow


def steps(case_id):
    return [(e["step"], e["status"]) for e in trace.read(case_id)]


# ---- the retry helper -------------------------------------------------------------------------------------

def test_a_call_that_fails_twice_succeeds_on_the_third_attempt_and_the_retries_are_traced():
    resilience.reset_faults("llm=2")
    with trace.for_case("case-r1"):
        assert resilience.call("diagnosis draft", lambda: "ok") == "ok"
    assert steps("case-r1") == [("diagnosis draft", "retry"), ("diagnosis draft", "retry"), ("diagnosis draft", "ok")]


def test_after_the_last_attempt_the_error_is_raised_and_traced():
    calls = []
    with trace.for_case("case-r2"), pytest.raises(TimeoutError):
        resilience.call("judge", lambda: calls.append(1) or (_ for _ in ()).throw(TimeoutError("slow")))
    assert len(calls) == 3 and steps("case-r2")[-1] == ("judge", "error")


def test_a_used_up_budget_is_not_retried():
    calls = []
    def f():
        calls.append(1)
        raise usage.BudgetExceeded("budget used up")
    with pytest.raises(usage.BudgetExceeded):
        resilience.call("diagnosis draft", f)
    assert len(calls) == 1


# ---- clean failure in the workflow ---------------------------------------------------------------------------

def _workflow(tmp_path, m, diagnose_fn, ex=None):
    return Workflow(m, LocalTickets(tmp_path / "t"), LocalNotifier(tmp_path / "n.jsonl"), path=tmp_path / "wf.sqlite",
                    diagnose_fn=diagnose_fn, execution="simulated" if ex else "off", executor=ex)


def test_an_investigation_that_keeps_failing_escalates_with_the_error_attached(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    cid = m.save_case(run(burst("x", 0), m).open_cases()[0])

    def always_failing(case):
        return resilience.call("diagnosis draft", lambda: (_ for _ in ()).throw(ConnectionError("Gemini unreachable")))
    st = _workflow(tmp_path, m, always_failing).start(cid, m.get(cid))
    assert st["status"] == "escalated to on-call"
    note = json.loads((tmp_path / "n.jsonl").read_text().splitlines()[-1])
    assert note["to"] == "on-call" and "investigation failed after retries: ConnectionError: Gemini unreachable" in note["message"]
    assert [s for s in steps(cid) if s[0] == "diagnosis draft"] == [("diagnosis draft", "retry")] * 2 + [("diagnosis draft", "error")]


def test_a_send_that_fails_once_is_retried_and_still_verified(tmp_path):
    resilience.reset_faults("cloud=1")
    env = Env(tmp_path, "db_down")
    case = env.case()
    with trace.for_case("case-r3"):
        r = executor(env).run(recommend(case, grounded("vm.start", "Start db-01.")), case, "approval", True,
                              {"vm.start"}, approved_by="aryan")
    assert r.result == "resolved" and ("send action", "retry") in steps("case-r3")


def test_a_send_that_keeps_failing_escalates_and_nothing_hangs(tmp_path):
    resilience.reset_faults("cloud=3")
    env = Env(tmp_path, "db_down")
    case = env.case()
    r = executor(env).run(recommend(case, grounded("vm.start", "Start db-01.")), case, "approval", True,
                          {"vm.start"}, approved_by="aryan")
    assert (r.result, r.escalate, r.sent) == ("failed", True, False) and "after 3 attempts" in r.detail


# ---- every step in the trace ---------------------------------------------------------------------------------

def test_every_step_of_a_case_is_in_its_trace(tmp_path):
    env = Env(tmp_path, "db_down")
    m = Memory(tmp_path / "m.sqlite")
    case = env.case()
    cid = m.save_case(case)
    w = _workflow(tmp_path, m, lambda c: grounded("vm.start", "Start the database VM db-01."), ex=executor(env, m))
    w.start(cid, case)
    w.decide(cid, "approve", by="aryan", reason="ok")
    names = [s for s, _ in steps(cid)]
    for expected in ("start", "investigate", "recommend", "route", "approval", "execute", "close"):
        assert expected in names, f"{expected} missing from the trace: {names}"
    assert ("approval", "waiting") in steps(cid)
    assert all(e.get("ms") is not None for e in trace.read(cid) if e["step"] in ("investigate", "execute"))
    assert f"runs/trace/{cid}.jsonl" in (tmp_path / "t" / f"{cid}.md").read_text()
