"""M6 step 2: Safety Reviewer, remediation agents, execution against the simulator, verification from telemetry.
The simulator runs in-process: each verification poll advances it one minute, applying the actions sent to it."""
import pathlib, sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from copilot import usage
from copilot.agents.remediation import ExecutionAgent
from copilot.agents.safety import SafetyReviewer
from copilot.agents.verify import Verifier
from copilot.config import reset_observed
from copilot.correlation import Correlator
from copilot.diagnosis import Diagnosis, GroundedDiagnosis
from copilot.memory import Memory
from copilot.otel import OTelReader
from copilot.outbox import LocalNotifier, LocalTickets
from copilot.routing import RoutingPolicy, recommend
from copilot.signals import IncidentSignal
from copilot.workflow import Workflow
from sim import otlp
from sim.scenarios import timeline
from sim.world import TICK_NS, World


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PATH", tmp_path / "usage.json")
    reset_observed()


class Env:
    """A running simulated shop: scenario up to `minutes` into the incident; tick() = one more minute."""
    def __init__(self, tmp, scenario, minutes=8):
        import time
        self.path, self.world, self.pending = tmp / "telemetry.jsonl", World(seed=7, run=scenario), []
        self.sink = otlp.FileSink(self.path)
        total, changes = timeline(scenario, minutes)
        self.t0, self.t = time.time_ns() - (total + 30) * TICK_NS, 0       # in the past, room for 30 more minutes
        for _ in range(total):
            for change in changes.get(self.t, []):
                change(self.world)
            self.tick()

    def apply(self, action, params, actor="copilot"):          # the simulated control API
        rid = f"req{len(self.pending)}{self.t}"
        self.pending.append((rid, action, params, actor))
        return rid

    def tick(self):
        for rid, action, params, actor in self.pending:
            self.world.apply(rid, action, params, actor)
        self.pending = []
        self.sink.send(self.world.tick(self.t, self.t0 + self.t * TICK_NS).documents())
        self.t += 1

    def case(self):
        sigs = sorted((s for s in OTelReader(self.path).poll() if isinstance(s, IncidentSignal)), key=lambda s: s.timestamp)
        corr = Correlator(clock=lambda: sigs[-1].timestamp)
        for s in sigs:
            corr.ingest(s)
        return max(corr.open_cases(), key=lambda c: len(c.signals))


def grounded(action, text, cite="runbooks/x.md#remediation"):
    d = Diagnosis(summary="s", confidence=0.9, root_cause={"text": "c", "citations": [cite]},
                  steps=[{"text": text, "action": action, "citations": [cite]}])
    return GroundedDiagnosis(diagnosis=d, removed=[], grounded=True, sources=[cite])


def executor(env, memory=None, polls=12, llm=None):
    return ExecutionAgent(env, str(env.path), SafetyReviewer(RoutingPolicy(), memory),
                          Verifier(timeout=1e9, quiet_polls=3, wait=env.tick, max_polls=polls), llm=llm)


def run(env, action, text, lane, approved_by=None, memory=None, params=None, polls=12):
    case = env.case()
    g = grounded(action, text)
    rec = recommend(case, g)
    if params is not None:
        rec = rec.model_copy(update={"params": params})
    return case, executor(env, memory, polls).run(rec, case, lane, True, {action}, approved_by)


# ---- the fix works ----------------------------------------------------------------------------------------

def test_auto_restart_of_a_runaway_process_is_executed_and_verified(tmp_path):
    env = Env(tmp_path, "cpu_runaway")
    case, r = run(env, "service.restart", "Restart the stuck report service on batch-01.", "auto")
    assert (r.result, r.agent, r.executed, r.escalate) == ("resolved", "service", True, False), r.detail
    assert env.world.cpu_override.get("batch-01") is None               # the simulated shop really changed


def test_approved_vm_start_brings_the_whole_cascade_back(tmp_path):
    env = Env(tmp_path, "db_down")
    case, r = run(env, "vm.start", "Start the database VM db-01.", "approval", approved_by="aryan")
    assert r.result == "resolved" and env.world.running["db-01"], r.detail
    assert "alert(s) closed" in r.detail


# ---- the Safety Reviewer blocks ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario, action, text, lane, approved, params, why", [
    ("db_down", "vm.start", "Start the database VM db-01.", "approval", None, None, "needs approval"),
    ("db_down", "vm.start", "Start the database VM db-01.", "auto", None, None, "above the auto limit"),
    ("db_down", "shell.exec", "Run a command on db-01.", "approval", "aryan", None, "allowlist"),
    ("disk_full", "logs.rotate", "Rotate logs on db-01.", "approval", "aryan", None, "holds data"),
    ("db_down", "vm.start", "Start web-77.", "approval", "aryan", {"vm": "web-77"}, "not part of this incident"),
    ("cpu_runaway", "vm.resize", "Resize batch-01.", "approval", "aryan", {"vm": "batch-01", "size": 8}, "out of bounds"),
    ("firewall_blocked", "firewall.restore", "Restore the rule.", "approval", "aryan", {}, "missing parameter"),
])
def test_safety_reviewer_blocks(tmp_path, scenario, action, text, lane, approved, params, why):
    env = Env(tmp_path, scenario)
    _, r = run(env, action, text, lane, approved, params=params)
    assert (r.result, r.executed, r.escalate) == ("blocked", False, True) and why in r.detail, r.detail
    assert env.pending == [] and not r.sent                             # nothing reached the environment


def test_a_step_that_is_not_in_the_grounded_diagnosis_is_blocked(tmp_path):
    env = Env(tmp_path, "db_down")
    case = env.case()
    rec = recommend(case, grounded("vm.start", "Start the database VM db-01."))
    r = executor(env).run(rec, case, "approval", True, grounded_actions={"escalate"}, approved_by="aryan")
    assert r.result == "blocked" and "fabricated" in r.detail


def test_circuit_breaker_after_three_attempts(tmp_path):
    env = Env(tmp_path, "db_down")
    m = Memory(tmp_path / "m.sqlite")
    for _ in range(3):
        m.record("case-x", "execution", {"action": "vm.start", "target": "db-01", "sent": True})
    _, r = run(env, "vm.start", "Start the database VM db-01.", "approval", "aryan", memory=m)
    assert r.result == "blocked" and "circuit breaker" in r.detail


# ---- remediation agents' preconditions ---------------------------------------------------------------------

def test_compute_agent_wont_start_a_vm_the_incident_doesnt_show_stopped(tmp_path):
    env = Env(tmp_path, "cpu_runaway")
    _, r = run(env, "vm.start", "Start batch-01.", "approval", "aryan", params={"vm": "batch-01"})
    assert r.result == "blocked" and r.agent == "compute" and "no evidence" in r.detail


def test_service_agent_wont_restart_on_a_stopped_vm(tmp_path):
    """A restart of a stopped database is refused — now at the safety reviewer, before the service agent.

    orders-db holds data, and service.restart interrupts a running service, so the reviewer blocks it
    for every data service whatever its state (see DATA_UNSAFE). The service agent's own "start the VM
    first" check still stands for non-data services; this case no longer reaches it.
    """
    env = Env(tmp_path, "db_down")
    _, r = run(env, "service.restart", "Restart PostgreSQL on db-01.", "approval", "aryan", params={"vm": "db-01"})
    assert r.result == "blocked" and not r.sent
    assert "holds data" in r.detail or "vm.start" in r.detail


# ---- verification says no ----------------------------------------------------------------------------------

def test_the_wrong_fix_is_detected_and_escalated(tmp_path):
    env = Env(tmp_path, "db_down")
    _, r = run(env, "service.restart", "Restart the storefront on web-01.", "approval", "aryan",
               params={"vm": "web-01"}, polls=8)
    assert (r.result, r.escalate) == ("failed", True) and "still open" in r.detail


def test_the_environment_refusing_is_a_failure(tmp_path):
    env = Env(tmp_path, "firewall_blocked")
    _, r = run(env, "firewall.restore", "Restore the rule.", "approval", "aryan", params={"rule": "allow-something-else"})
    assert r.result == "failed" and "refused" in r.detail and env.world.firewall_ok is False


def test_no_confirmation_from_the_environment_escalates(tmp_path):
    env = Env(tmp_path, "db_down")
    env.apply = lambda *a, **k: "lost-request"                        # the request never arrives
    _, r = run(env, "vm.start", "Start the database VM db-01.", "approval", "aryan", polls=4)
    assert (r.result, r.escalate) == ("not_applied", True)


# ---- in the workflow ---------------------------------------------------------------------------------------

def test_workflow_approval_then_execution_then_verified_close(tmp_path):
    env = Env(tmp_path, "db_down")
    m = Memory(tmp_path / "m.sqlite")
    case = env.case()
    cid = m.save_case(case)
    notes = tmp_path / "n.jsonl"
    w = Workflow(m, LocalTickets(tmp_path / "t"), LocalNotifier(notes), path=tmp_path / "wf.sqlite",
                 execution="simulated", diagnose_fn=lambda c: grounded("vm.start", "Start the database VM db-01."),
                 executor=executor(env, m))
    st = w.start(cid, case)
    assert st["waiting"] and st["card"]["action"] == "vm.start" and st["card"]["target"] == "db-01"
    done = w.decide(cid, "approve", by="aryan", reason="db-01 is stopped")
    assert done["status"] == "closed: resolved by vm.start (verified from telemetry)", done["status"]
    assert m.outcome_stats("vm.start", "orders-db") == {"resolved": 1}
    ex = [e for e in m.events(cid) if e["kind"] == "execution"][0]
    assert ex["sent"] and ex["agent"] == "compute"


def test_quiet_is_not_recovered_while_an_alert_stays_open(tmp_path):
    """Unreachable storefront: no requests means no errors, but the uptime alert is still open. Restarting the
    service is the wrong fix; silence must not be mistaken for recovery."""
    env = Env(tmp_path, "firewall_blocked")
    _, r = run(env, "service.restart", "Restart the storefront on web-01.", "approval", "aryan",
               params={"vm": "web-01"}, polls=8)
    assert r.result != "resolved" and r.escalate and env.world.firewall_ok is False, r.detail


def test_execution_verifies_against_the_latest_case_not_the_snapshot_from_the_start(tmp_path):
    """Seen live 29 Sep: the workflow started on the first signal; by approval time three alerts had joined the case,
    but verification used the old snapshot (0 alerts) and called it resolved without checking them."""
    env = Env(tmp_path, "db_down")
    m = Memory(tmp_path / "m.sqlite")
    full = env.case()
    first_only = full.model_copy(update={"signals": [x for x in full.signals if x.metric == "event/vm.stopped"]})
    cid = m.save_case(first_only)
    w = Workflow(m, LocalTickets(tmp_path / "t"), LocalNotifier(tmp_path / "n.jsonl"), path=tmp_path / "wf.sqlite",
                 execution="simulated", diagnose_fn=lambda c: grounded("vm.start", "Start the database VM db-01."),
                 executor=executor(env, m))
    w.start(cid, first_only)
    m.save_case(full.model_copy(update={"case_id": cid}))              # watch kept adding signals meanwhile
    done = w.decide(cid, "approve", by="aryan")
    ex = done["execution"]
    assert ex["result"] == "resolved" and not ex["detail"].startswith("0 alert"), ex["detail"]


class DecideLLM:
    """Scripted remediation LLM for ambiguous recovery: answers with a decision, or fails."""
    def __init__(self, decision=None, fail=False): self.decision, self.fail, self.calls = decision, fail, 0
    def with_structured_output(self, schema):
        outer = self
        class R:
            def invoke(self, prompt):
                outer.calls += 1
                if outer.fail:
                    raise TimeoutError("LLM timed out")
                return schema(decision=outer.decision, reason="alerts are closing one by one")
        return R()


def _ambiguous_run(tmp_path, llm):
    """vm.start on db-01 with a verification window too short for all alerts to close: partial recovery."""
    env = Env(tmp_path, "db_down")
    case = env.case()
    rec = recommend(case, grounded("vm.start", "Start the database VM db-01."))
    ex = ExecutionAgent(env, str(env.path), SafetyReviewer(RoutingPolicy()),
                        Verifier(timeout=1e9, quiet_polls=3, wait=env.tick, max_polls=2), llm=llm)
    return ex.run(rec, case, "approval", True, {"vm.start"}, approved_by="aryan")


def test_ambiguous_recovery_asks_the_llm_and_waits_when_told():
    import tempfile
    llm = DecideLLM("wait")
    r = _ambiguous_run(pathlib.Path(tempfile.mkdtemp()), llm)
    assert llm.calls == 1 and r.result == "resolved" and r.llm_decision.startswith("wait"), r.detail


def test_ambiguous_recovery_without_a_usable_llm_escalates(tmp_path):
    r = _ambiguous_run(tmp_path, DecideLLM(fail=True))
    assert r.escalate and r.result == "ambiguous" and "no LLM decision" in r.llm_decision


def test_the_llm_is_not_asked_when_verification_is_clear(tmp_path):
    env = Env(tmp_path, "db_down")
    case = env.case()
    llm = DecideLLM("wait")
    rec = recommend(case, grounded("vm.start", "Start the database VM db-01."))
    r = executor(env, llm=llm).run(rec, case, "approval", True, {"vm.start"}, approved_by="aryan")
    assert r.result == "resolved" and llm.calls == 0


def test_an_incident_that_recovered_by_itself_is_not_acted_on(tmp_path):
    """Spec trap 'self-healing blip': by the time the action would run, every alert has closed."""
    env = Env(tmp_path, "blip", minutes=10)
    case = env.case()
    assert case.all_clear                                                # alerts fired and closed again
    _, r = run(env, "service.restart", "Restart the storefront on web-01.", "approval", "aryan", params={"vm": "web-01"})
    assert (r.result, r.executed, r.escalate) == ("recovered", False, False) and env.pending == []
