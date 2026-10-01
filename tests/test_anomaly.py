import random
from datetime import datetime, timedelta, timezone

from copilot.anomaly import detect
from copilot.intake import intake
from copilot.signals import IncidentSignal, SignalType

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
CPU = "compute.googleapis.com/instance/cpu/utilization"
RES = {"type": "gce_instance", "name": "web-01"}


def series(values):
    return [(T0 + timedelta(minutes=i), v) for i, v in enumerate(values)]


def normal(n, mean=0.30, spread=0.02, seed=1):
    rnd = random.Random(seed)
    return [mean + rnd.uniform(-spread, spread) for _ in range(n)]


def test_sustained_spike_is_an_anomaly_and_becomes_a_signal():
    payload = detect(series(normal(60) + [0.95, 0.97, 0.96]), CPU, RES)
    assert payload and payload["zscore"] > 3 and payload["expected"] < 0.35
    [sig] = intake(payload, "anomaly")
    assert isinstance(sig, IncidentSignal) and sig.signal_type == SignalType.anomaly and sig.service == "storefront"


def test_single_blip_is_ignored():
    assert detect(series(normal(60) + [0.30, 0.95, 0.31]), CPU, RES) is None


def test_drop_is_detected_too():
    assert detect(series(normal(60, mean=500, spread=10) + [20, 15, 18]), "requests", RES) is not None


def test_mixed_directions_are_not_one_anomaly():
    assert detect(series(normal(60) + [0.95, 0.01, 0.97]), CPU, RES) is None


def test_not_enough_history():
    assert detect(series(normal(10) + [0.95, 0.97, 0.96]), CPU, RES) is None


def test_flat_baseline_needs_a_real_move():
    flat = [0.20] * 60
    assert detect(series(flat + [0.2005, 0.2006, 0.2004]), CPU, RES) is None     # noise on a flat line
    assert detect(series(flat + [0.60, 0.62, 0.61]), CPU, RES) is not None


def test_same_episode_gets_the_same_id():
    base = normal(60)
    a = detect(series(base + [0.95, 0.97, 0.96]), CPU, RES)
    b = detect(series(base + [0.95, 0.97, 0.96]), CPU, RES)
    assert a["id"] == b["id"]
