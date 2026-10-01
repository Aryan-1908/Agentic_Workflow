"""M5: routing lanes, the durable approval pause, and the audit trail (tickets, notifications, memory)."""
import json, pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_memory import burst, run   # noqa: E402

from copilot.diagnosis import Diagnosis, GroundedDiagnosis
from copilot.memory import Memory
from copilot.outbox import LocalNotifier, LocalTickets
from copilot.routing import Recommendation, RoutingPolicy, blast_radius, route
from copilot.workflow import Workflow

P = RoutingPolicy()


def rec(action="vm.start", blast="low", conf=0.9, **kw):
    fields = dict(action=action, target="web-01", service="storefront", blast_radius=blast, blast_reason="t",
                  confidence=conf, rollback="-", rationale="-", citations=["runbooks/vm-stopped.md#remediation"])
    return Recommendation(**{**fields, **kw})


# ---- routing ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("r, grounded, lane", [
    (rec(), True, "auto"),                                      # low blast, high confidence
    (rec(blast="medium"), True, "approval"),                   # elevated blast radius
    (rec(conf=0.6), True, "approval"),                         # ambiguous
    (rec(conf=0.3), True, "escalate"),                         # low confidence
    (rec(blast="critical"), True, "escalate"),                 # safety-critical
    (rec(action="escalate", blast="none"), True, "escalate"),  # the runbook says escalate
    (rec(action="none", blast="none"), True, "none"),          # nothing to do
    (rec(), False, "escalate"),                                # no grounded root cause
], ids=["auto", "blast", "ambiguous", "low-conf", "critical", "runbook-escalate", "no-action", "ungrounded"])
def test_lanes(r, grounded, lane):
    assert route(r, grounded, P).lane == lane


def test_blocked_actions_always_need_a_person():
    assert route(rec(action="vm.start"), True, RoutingPolicy(block_actions=["vm.start"])).lane == "approval"


def test_blast_radius_grows_with_service_tier():
    assert blast_radius("vm.start", "reports-batch")[0] == "low"     # tier 3
    assert blast_radius("vm.start", "orders-db")[0] == "medium"      # tier 1
    assert blast_radius("mig.rollback", "storefront")[0] == "high"   # scope 2, tier 1
    assert blast_radius("vm.start", "nobody-knows")[0] == "medium"   # unknown service: +1
    assert blast_radius("shell.exec", "storefront")[0] == "critical" # not a registered action


# ---- workflow -----------------------------------------------------------------------------------------

def grounded(action, conf=0.9, ok=True):
    cite = {"vm.start": "runbooks/vm-stopped.md#remediation", "none": "runbooks/guest-agent-errors.md#remediation",
            "escalate": "runbooks/third-party-outage.md#remediation"}[action]
    d = Diagnosis(summary=f"diagnosis for {action}", confidence=conf,
                  root_cause={"text": "cause", "citations": [cite]} if ok else None,
                  steps=[{"text": f"do {action}", "action": action, "citations": [cite]}])
    return GroundedDiagnosis(diagnosis=d, removed=[], grounded=ok, sources=[cite])


@pytest.fixture
def env(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    return {"memory": m, "tickets": LocalTickets(tmp_path / "tickets"), "notifier": LocalNotifier(tmp_path / "n.jsonl"),
            "path": tmp_path / "wf.sqlite", "tmp": tmp_path}


def wf(env, diagnosis=None, **kw):
    return Workflow(env["memory"], env["tickets"], env["notifier"], path=env["path"],
                    diagnose_fn=(lambda case: diagnosis) if diagnosis else None, **kw)


def case_on(env, service, vm):
    c = run(burst("x", 0, service=service, vm=vm), env["memory"]).open_cases()[0]
    return env["memory"].save_case(c), c


def notes(env):
    return [json.loads(l) for l in (env["tmp"] / "n.jsonl").read_text().splitlines()]


def test_no_action_lane_closes_with_ticket_and_notification(env):
    cid, case = case_on(env, "storefront", "web-01")
    s = wf(env, grounded("none")).start(cid, case)
    assert s["route"]["lane"] == "none" and s["status"] == "closed: no action needed"
    assert "closed: no action needed" in (env["tmp"] / "tickets" / f"{cid}.md").read_text()
    assert notes(env)[-1]["to"] == "team"
    kinds = [e["kind"] for e in env["memory"].events(cid)]
    assert kinds == ["diagnosis", "recommendation", "outcome"]


def test_auto_lane_records_but_never_executes_in_an_observe_only_project(env):
    cid, case = case_on(env, "reports-batch", "batch-01")          # tier 3: vm.start is low blast
    s = wf(env, grounded("vm.start"), execution="off").start(cid, case)
    assert s["route"]["lane"] == "auto"
    assert s["execution"] == {"executed": False, "result": "not executed: observe-only project", "action": "vm.start"}


def test_approval_pause_survives_a_restart_and_records_who_when_why(env):
    cid, case = case_on(env, "storefront", "web-01")               # tier 1: vm.start is medium blast
    first = wf(env, grounded("vm.start"))
    s = first.start(cid, case)
    assert s["waiting"] and s["status"] == "awaiting approval"
    assert s["card"]["action"] == "vm.start" and s["card"]["blast_radius"] == "medium"
    assert notes(env)[-1]["to"] == "approvers"
    assert "awaiting approval" in (env["tmp"] / "tickets" / f"{cid}.md").read_text()
    first.conn.close()
    del first                                                        # the process dies

    later = wf(env)                                                  # new process, no LLM needed to approve
    assert cid in later.cases() and later.state(cid)["waiting"]
    done = later.decide(cid, "approve", by="aryan", reason="checked the VM")
    assert done["status"].startswith("closed: not executed") and not done["waiting"]
    d = done["decision"]
    assert (d["decision"], d["by"], d["reason"]) == ("approve", "aryan", "checked the VM") and d["at"]
    approval = [e for e in env["memory"].events(cid) if e["kind"] == "approval"][0]
    assert approval["by"] == "aryan" and approval["at"]
    ticket = (env["tmp"] / "tickets" / f"{cid}.md").read_text()
    assert "approve by aryan: checked the VM" in ticket and "## Timeline" in ticket


def test_reject_needs_a_reason_and_closes_without_executing(env):
    cid, case = case_on(env, "storefront", "web-01")
    w = wf(env, grounded("vm.start"))
    w.start(cid, case)
    with pytest.raises(ValueError, match="reason"):
        w.decide(cid, "reject", by="aryan")
    with pytest.raises(ValueError, match="approver"):
        w.decide(cid, "approve", by=" ")
    s = w.decide(cid, "reject", by="aryan", reason="VM is being migrated")
    assert s["status"] == "closed: rejected by aryan" and "execution" not in s
    with pytest.raises(ValueError, match="not waiting"):
        w.decide(cid, "approve", by="aryan")


def test_ungrounded_diagnosis_is_escalated_to_on_call(env):
    cid, case = case_on(env, "storefront", "web-01")
    s = wf(env, grounded("vm.start", ok=False)).start(cid, case)
    assert s["route"]["lane"] == "escalate" and s["status"] == "escalated to on-call"
    assert notes(env)[-1]["to"] == "on-call" and "insufficient knowledge" in notes(env)[-1]["message"]


def test_starting_a_case_twice_does_not_rerun_it(env):
    cid, case = case_on(env, "storefront", "web-01")
    calls = []
    w = Workflow(env["memory"], env["tickets"], env["notifier"], path=env["path"],
                 diagnose_fn=lambda c: calls.append(1) or grounded("none"))
    w.start(cid, case)
    w.start(cid, case)                                               # e.g. watch --since replays the same history
    assert len(calls) == 1


def test_listing_cases_on_a_fresh_machine(env):
    assert wf(env).cases() == []


def test_an_interrupted_workflow_resumes_on_the_next_start(env):
    """Seen live 28 Sep: Ctrl+C during the diagnosis left the case 'running' forever."""
    cid, case = case_on(env, "storefront", "web-01")
    calls = []

    def flaky(c):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt          # or an LLM error: the node never finished
        return grounded("none")

    w = Workflow(env["memory"], env["tickets"], env["notifier"], path=env["path"], diagnose_fn=flaky)
    with pytest.raises(KeyboardInterrupt):
        w.start(cid, case)
    assert w.state(cid)["status"] == "running"
    s = w.start(cid, case)                   # next watch
    assert s["status"] == "closed: no action needed" and len(calls) == 2
    assert [t["step"] for t in s["trace"]].count("start") == 1


def test_unfinished_workflows_are_listed_and_resumed_without_seeing_the_case_again(env):
    """Seen live 29 Sep: the case's signal had left the --since window, so it was never started again."""
    cid, case = case_on(env, "storefront", "web-01")
    calls = []

    def flaky(c):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt          # the process was stopped mid-step (a failed call now escalates cleanly)
        return grounded("none")

    w = Workflow(env["memory"], env["tickets"], env["notifier"], path=env["path"], diagnose_fn=flaky)
    with pytest.raises(KeyboardInterrupt):
        w.start(cid, case)
    later = Workflow(env["memory"], env["tickets"], env["notifier"], path=env["path"], diagnose_fn=flaky)
    assert later.unfinished() == [cid]
    assert later.resume(cid)["status"] == "closed: no action needed" and later.unfinished() == []


def test_llm_calls_are_bounded(monkeypatch):
    import copilot.llm as llm
    seen = {}
    monkeypatch.setattr(llm, "init_chat_model", lambda name, **kw: seen.update(kw))
    llm.get_llm("diagnosis")
    assert seen["timeout"] == 60 and seen["max_retries"] == 0      # retried (visibly) by copilot/resilience.py


def test_diagnosis_reports_progress(tmp_path):
    from test_diagnosis import DraftLLM
    from test_memory import WordEmbedder
    from copilot.diagnosis import diagnose
    from copilot.kb.index import KnowledgeIndex
    m = Memory(tmp_path / "m.sqlite")
    idx = KnowledgeIndex(m.db, embedder=WordEmbedder()); idx.rebuild()
    case = run(burst("a", 0), m).open_cases()[0]
    steps = []
    diagnose(case, idx, DraftLLM(), judge=None, progress=steps.append)
    assert steps[0] == "searching the knowledge base" and steps[1].startswith("drafting the diagnosis")


def test_dotenv_loads_keys_without_overriding_the_environment(tmp_path, monkeypatch):
    from copilot.__main__ import load_dotenv
    (tmp_path / ".env").write_text('# comment\nexport GOOGLE_API_KEY="from-file"\nCOPILOT_X=1\n')
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("COPILOT_X", "from-env")
    load_dotenv(tmp_path / ".env")
    import os
    assert os.environ["GOOGLE_API_KEY"] == "from-file" and os.environ["COPILOT_X"] == "from-env"
    monkeypatch.delenv("GOOGLE_API_KEY")


# ---- action target (seen 29 Sep: vm.start proposed on a healthy VM of a merged case) ----------------------

def _case(signals, root_hint=None):
    from copilot.correlation import Case
    from datetime import datetime, timezone
    from test_correlation import make_signal
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    return Case(case_id="c", signals=[make_signal(s) for s in signals], opened_at=now, last_placed_at=now)


def _step(text, action="vm.start"):
    from copilot.diagnosis import Step
    return Step(text=text, action=action, citations=["runbooks/vm-stopped.md#remediation"])


DB_DOWN = [{"id": "d", "minute": 0, "service": "orders-db", "title": "db-01: VM stopped", "resource": "db-01"},
           {"id": "a", "minute": 1, "service": "orders-api", "title": "db errors", "resource": "orders-01"}]


def test_target_is_the_resource_the_step_names():
    from copilot.routing import pick_target
    assert pick_target(_case(DB_DOWN), _step("Start the database VM db-01 after approval.")) == ("db-01", "orders-db")


def test_target_falls_back_to_the_root_service_resource():
    from copilot.routing import pick_target
    assert pick_target(_case(DB_DOWN), _step("Start the database VM.")) == ("db-01", "orders-db")


def test_no_clear_target_means_escalate_never_a_guess():
    from copilot.routing import pick_target
    payfast_and_db = DB_DOWN + [{"id": "p", "minute": 1, "service": "payments-provider", "title": "PayFast outage",
                                 "resource": "Card Authorization API"}]
    case = _case(payfast_and_db)
    assert case.root_service is None                                   # two independent origins
    target, _ = pick_target(case, _step("Start the stopped VM."))
    assert target is None
    assert route(rec(action="vm.start", target=None), True, P).lane == "escalate"
    assert pick_target(case, _step("Start orders-01 or db-01."))[0] is None      # names two: ambiguous


def test_an_incident_is_diagnosed_only_once_it_has_settled():
    from datetime import datetime, timedelta, timezone
    from copilot.workflow import settled
    c = _case(DB_DOWN)
    t0 = datetime(2026, 1, 15, tzinfo=timezone.utc)
    c = c.model_copy(update={"opened_at": t0, "last_placed_at": t0 + timedelta(seconds=30)})
    assert not settled(c, t0 + timedelta(seconds=40))          # a signal joined 10 s ago: still growing
    assert settled(c, t0 + timedelta(seconds=51))              # quiet for 21 s
    busy = c.model_copy(update={"last_placed_at": t0 + timedelta(seconds=119)})
    assert settled(busy, t0 + timedelta(seconds=121))          # never waits more than 2 minutes
