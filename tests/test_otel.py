"""OpenTelemetry input: every simulator scenario, as OTLP, through the reader and correlation. The same path as live
(simulator -> OTLP JSON -> reader -> signals -> cases), minus the Collector process, whose file format FileSink writes."""
import json

import pytest

from copilot.config import observed_edges, reset_observed
from copilot.correlation import Correlator
from copilot.intake import from_log_record
from copilot.otel import OTelReader
from copilot.signals import IncidentSignal, Rejected, Severity, SignalType
from sim import otlp
from sim.scenarios import SCENARIOS, timeline
from sim.world import TICK_NS, World

T0 = 1_768_464_000 * 10**9          # 2026-01-15 08:00 UTC: fixed, in the past


def simulate(name: str, path, minutes: int = 12):
    sink, world = otlp.FileSink(path), World(seed=7)
    total, changes = timeline(name, minutes)
    for t in range(total):
        for change in changes.get(t, []):
            change(world)
        sink.send(world.tick(t, T0 + t * TICK_NS).documents())


def run(name, tmp_path):
    reset_observed()
    path = tmp_path / "telemetry.jsonl"
    path.unlink(missing_ok=True)
    simulate(name, path)
    items = OTelReader(path).poll()
    signals = sorted((i for i in items if isinstance(i, IncidentSignal)), key=lambda s: s.timestamp)
    corr = Correlator(clock=lambda: signals[-1].timestamp if signals else None)
    for s in signals:
        corr.ingest(s)
    return items, signals, corr


# scenario -> (services of each expected case, root of each), or [] for no incident
EXPECT = {
    # Capacity / saturation probe (tests/capacity_eval): nothing is broken, something is full.
    "pool_exhaustion": [({"orders-api", "storefront"}, "orders-api")],
    "pool_exhaustion_db_noisy": [({"orders-api", "storefront", "orders-db"}, "orders-api")],  # saturation beats the graph
    "storefront_saturated": [({"storefront"}, "storefront")],
    "batch_saturated": [({"reports-batch"}, "reports-batch")],
    "db_connections_full": [({"orders-db", "orders-api", "storefront"}, "orders-db")],
    "cpu_vs_capacity": [({"orders-api"}, "orders-api")],   # the CPU alert fires alone; no checkout failures
    "healthy": [],
    "vm_stopped": [({"storefront"}, "storefront")],
    "process_crash": [({"storefront"}, "storefront")],
    "cpu_runaway": [({"reports-batch"}, "reports-batch")],
    "lookalike_cpu": [({"storefront"}, "storefront"), ({"reports-batch"}, "reports-batch")],
    "flapping": [({"reports-batch"}, "reports-batch")],
    "blip": [({"storefront"}, "storefront")],            # without actions it looks like cpu_runaway
    "disk_full": [({"orders-db", "orders-api", "storefront"}, "orders-db")],
    "db_down": [({"orders-db", "orders-api", "storefront"}, "orders-db")],
    "bad_release": [({"storefront"}, "storefront")],
    "firewall_blocked": [({"storefront"}, "storefront")],
    "payfast_outage": [({"payments-provider", "storefront"}, "payments-provider")],
    "guest_agent_noise": [({"storefront"}, "storefront")],
    "scheduled_stop": [({"reports-batch"}, "reports-batch")],   # the monitoring alert still fires; diagnosis says expected
    "novel": [({"session-cache"}, "session-cache")],
}


def test_every_scenario_has_an_expectation():
    assert set(EXPECT) == set(SCENARIOS)


@pytest.mark.parametrize("name", list(EXPECT))
def test_scenario_becomes_the_expected_incidents(name, tmp_path):
    items, signals, corr = run(name, tmp_path)
    assert not [i for i in items if isinstance(i, Rejected)], "simulator output the reader rejected"
    got = sorted(((set(c.services), c.root_service) for c in corr.open_cases()), key=lambda x: sorted(x[0]))
    want = sorted(EXPECT[name], key=lambda x: sorted(x[0]))
    assert got == want, f"{SCENARIOS[name][0]}\n  got {got}"


def test_dependencies_are_learned_from_traces(tmp_path):
    run("healthy", tmp_path)
    assert observed_edges() == {"storefront": {"orders-api", "payments-provider"}, "orders-api": {"orders-db"}}


def test_info_events_are_context_that_joins_the_incident(tmp_path):
    _, _, corr = run("bad_release", tmp_path)
    [case] = corr.open_cases()
    assert any(s.metric == "event/deploy.rollout" for s in case.signals)     # the rollout explains the errors
    _, _, corr = run("scheduled_stop", tmp_path)
    [case] = corr.open_cases()
    assert any(s.metric == "event/vm.stopped_by_schedule" for s in case.signals)


def test_alerts_open_and_close(tmp_path):
    reset_observed()
    path = tmp_path / "t.jsonl"
    sink, world = otlp.FileSink(path), World()
    world.stop_vm("web-01")
    for t in range(3):
        sink.send(world.tick(t, T0 + t * TICK_NS).documents())
    world.start_vm("web-01", agent_noise=False)
    for t in range(3, 7):
        sink.send(world.tick(t, T0 + t * TICK_NS).documents())
    alerts = [s for s in OTelReader(path).poll() if isinstance(s, IncidentSignal) and s.signal_type == SignalType.alert]
    uptime = [s for s in alerts if s.title.startswith("storefront uptime")]
    assert [s.state for s in uptime] == ["open", "closed"] and uptime[0].signal_id.rsplit(":", 1)[0] == uptime[1].signal_id.rsplit(":", 1)[0]


def test_anomaly_detector_sees_the_runaway_cpu(tmp_path):
    _, signals, _ = run("cpu_runaway", tmp_path)
    assert any(s.signal_type == SignalType.anomaly and s.service == "reports-batch" for s in signals)


def test_reader_returns_only_new_data_and_skips_partial_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    simulate("db_down", path, minutes=2)
    r = OTelReader(path)
    first = r.poll()
    assert first and r.poll() == []
    with open(path, "a") as f:
        f.write('{"resourceLogs": [')                  # the Collector is mid-write
    assert r.poll() == []


def test_unknown_event_names_are_rejected_not_dropped():
    rec = otlp.log_record(T0, otlp.WARN, "x", "u1", **{"event.name": "vm.teleported"})
    from copilot.intake import safely
    [r] = safely("otlp.log", rec, lambda: from_log_record({"service.name": "storefront"}, "infra", rec))
    assert isinstance(r, Rejected) and "vm.teleported" in r.reason


def test_log_severity_follows_otel_severity_numbers():
    res = {"service.name": "storefront", "host.name": "web-01"}
    assert from_log_record(res, "app", otlp.log_record(T0, otlp.INFO, "fine", "a")) is None
    assert from_log_record(res, "app", otlp.log_record(T0, otlp.ERROR, "bad", "b")).severity == Severity.error
    assert from_log_record(res, "app", otlp.log_record(T0, otlp.FATAL, "worse", "c")).severity == Severity.critical


def test_two_runs_of_the_same_scenario_never_share_ids(tmp_path):
    """Seen 29 Sep: a second db_down run came back as 'duplicate', because the simulator reused ids across runs."""
    ids = []
    for run, start in (("r1", T0), ("r2", T0 + 86_400 * 10**9)):     # the same scenario, the next day
        path = tmp_path / f"{run}.jsonl"
        sink, world = otlp.FileSink(path), World(seed=7, run=run)
        world.stop_vm("db-01")
        for t in range(3):
            sink.send(world.tick(t, start + t * TICK_NS).documents())
        ids.append({s.signal_id for s in OTelReader(path).poll() if isinstance(s, IncidentSignal)})
    assert ids[0] and ids[1] and not ids[0] & ids[1]
