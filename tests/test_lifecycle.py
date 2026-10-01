"""M7: incident lifecycle through the whole engine (read OTel -> correlate -> workflow -> execute -> verify -> close),
against the in-process simulator. A resolved incident closes, a recurrence is a new incident, and a flapping target
stops being remediated after the circuit breaker's limit."""
import json, pathlib, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_execution import Env, executor, grounded              # noqa: E402

from copilot.correlation import Correlator
from copilot.engine import Engine
from copilot.memory import Memory
from copilot.otel import OTelReader
from copilot.outbox import LocalNotifier, LocalTickets
from copilot.workflow import Workflow
from sim.world import TICK_NS


def build(tmp_path, scenario, diagnosis, policy=None):
    env = Env(tmp_path, scenario)
    sim_now = lambda: datetime.fromtimestamp((env.t0 + env.t * TICK_NS) / 1e9, tz=timezone.utc)
    m = Memory(tmp_path / "m.sqlite")
    notes = tmp_path / "n.jsonl"
    flow = Workflow(m, LocalTickets(tmp_path / "t"), LocalNotifier(notes), path=tmp_path / "wf.sqlite",
                    execution="simulated", diagnose_fn=lambda c: diagnosis, executor=executor(env, m), policy=policy)
    eng = Engine(OTelReader(env.path), m, corr=Correlator(clock=sim_now), flow=flow, out=lambda *_: None,
                 settle=timedelta(0), clock=sim_now)
    return env, m, eng, notes


def test_a_fix_that_does_not_hold_sends_the_next_attempt_to_a_person(tmp_path):
    env, m, eng, notes = build(tmp_path, "flapping",
                               grounded("service.restart", "Restart the stuck report service on batch-01."))
    results = []
    for _ in range(60):
        results += eng.step()
        waiting = [cid for cid in eng.flow.cases() if eng.flow.state(cid)["waiting"]]
        if waiting:
            break
        env.tick()
    assert [r["status"] for r in results] == ["closed: resolved by service.restart (verified from telemetry)"]
    first = results[0]["case_id"]
    assert [e["result"] for e in m.events(first) if e["kind"] == "outcome"] == ["resolved", "failed"]
    card = eng.flow.state(waiting[0])["card"]
    assert card["action"] == "service.restart" and card["confidence"] < 0.8     # lower confidence: a person decides


def test_with_auto_kept_on_a_flapping_target_is_still_stopped_by_the_circuit_breaker(tmp_path):
    from copilot.routing import RoutingPolicy
    permissive = RoutingPolicy(auto_min_confidence=0.0, escalate_below_confidence=0.0)
    env, m, eng, notes = build(tmp_path, "flapping",
                               grounded("service.restart", "Restart the stuck report service on batch-01."), permissive)
    results = []
    for _ in range(120):
        results += eng.step()
        if any("circuit breaker" in json.dumps(r.get("execution", {})) for r in results):
            break
        env.tick()
    statuses = [r["status"] for r in results]
    resolved = [s for s in statuses if s.startswith("closed: resolved")]
    assert len(resolved) == 3, statuses                                   # fixed three times, each verified
    assert len({r["case_id"] for r in results}) == len(results)          # every recurrence was a new incident
    last = results[-1]
    assert last["status"].startswith("escalated") and "circuit breaker" in last["execution"]["detail"]
    on_call = [json.loads(l) for l in notes.read_text().splitlines() if json.loads(l)["to"] == "on-call"]
    assert "circuit breaker" in on_call[-1]["message"]
    sent = sum(e.get("sent", False) for r in results for e in m.events(r["case_id"]) if e["kind"] == "execution")
    assert sent == 3                                                       # the fourth attempt never reached the shop


def test_a_resolved_incident_closes_and_late_signals_dont_reopen_it(tmp_path):
    env, m, eng, notes = build(tmp_path, "cpu_runaway",
                               grounded("service.restart", "Restart the stuck report service on batch-01."))
    results = []
    for _ in range(40):
        results += eng.step()
        env.tick()
    assert [r["status"] for r in results] == ["closed: resolved by service.restart (verified from telemetry)"]
    [case] = [c for c in eng.corr.cases.values() if c.state == "resolved"]
    assert case.resolved_at is not None
    assert not eng.corr.open_cases()                                      # nothing reopened afterwards
    assert m.db.execute("SELECT all_clear FROM cases WHERE case_id=?", (results[0]["case_id"],)).fetchone()[0] == 1
