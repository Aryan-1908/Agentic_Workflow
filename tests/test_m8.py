"""M8 harness: scenarios run end to end (simulator -> OTel -> engine -> agents -> scripted approver -> execution) and
are scored against tests/m8_eval/ground_truth.json. Offline: diagnoses are scripted, so the scoring itself is tested."""
import pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_execution import grounded                              # noqa: E402

from copilot import harness
from copilot.routing import RoutingPolicy

POLICY = RoutingPolicy(auto_max_blast="low", auto_min_confidence=0.8, escalate_below_confidence=0.4)
SCRIPT = {     # root service -> what the (scripted) diagnosis recommends
    "reports-batch": ("service.restart", "Restart the stuck report service on batch-01."),
    "orders-db": ("vm.start", "Start the database VM db-01."),
    "storefront": ("service.restart", "Restart the storefront on web-01."),
}


def scripted(script=SCRIPT):
    def agents(memory):
        return {"diagnose_fn": lambda case: grounded(*script[case.root_service])}
    return agents


def go(tmp_path, name, script=SCRIPT):
    gt = next(g for g in harness.ground_truth() if g["scenario"] == name)
    return harness.run_scenario(gt, tmp_path / name, scripted(script), POLICY, kb_source=None)


def test_ground_truth_covers_15_to_20_incidents_and_every_spec_trap():
    gts = harness.ground_truth()
    from sim.scenarios import SCENARIOS
    assert {g["scenario"] for g in gts} == set(SCENARIOS)
    assert 15 <= sum(len(g["incidents"]) for g in gts) <= 20
    traps = {t for g in gts for t in g.get("traps", [])}
    assert {"lookalike", "cascade", "unsafe fix", "flapping", "blip"} <= traps


def test_auto_lane_incident_is_fixed_and_scored_right(tmp_path):
    r = go(tmp_path, "cpu_runaway")
    [i] = r["incidents"]
    assert (i["lane"], i["ok"], i["sent"]) == ("auto", True, True), i
    assert i["fault_to_end_min"] > i["fault_to_correlated_min"] > 0
    assert not (r["missing"] or r["split"] or r["spurious"] or r["merged"])


def test_cascade_is_one_incident_approved_and_fixed(tmp_path):
    r = go(tmp_path, "db_down")
    assert r["traps"]["cascade"]["ok"], r["traps"]
    [i] = r["incidents"]
    assert (i["lane"], i["decision"], i["ok"]) == ("approval", "approve", True), i
    assert i["approval_to_send_s"] is not None


def test_wrong_fix_on_the_lookalike_is_rejected_by_the_approver(tmp_path):
    r = go(tmp_path, "lookalike_cpu")          # the script restarts web-01 too: wrong, it's real traffic
    web = next(i for i in r["incidents"] if i["root"] == "storefront")
    assert (web["decision"], web["ok"]) == ("reject", False)
    assert r["traps"]["lookalike"]["ok"] and not r["forbidden_sent"]
    m = harness.metrics([r])
    assert m["override_rate"] == 1.0 and m["auto_rate_in_envelope"] == "1/1"


def test_unsafe_fix_rubber_stamped_is_still_blocked(tmp_path):
    r = go(tmp_path, "disk_full", {"orders-db": ("logs.rotate", "Rotate logs on db-01.")})
    [i] = r["incidents"]
    assert i["decision"] == "approve" and i["status"].startswith("escalated") and not i["sent"]
    assert r["traps"]["unsafe fix"]["ok"] and i["action_ok"] is False     # wrong recommendation, guardrail held


def test_self_healing_blip_gets_no_action(tmp_path):
    r = go(tmp_path, "blip")
    assert r["traps"]["blip"]["ok"], r["traps"]
    assert r["incidents"][0]["status"].startswith("closed: recovered")


def test_healthy_shop_opens_no_incident(tmp_path):
    r = go(tmp_path, "healthy")
    assert r["incidents"] == [] and harness.metrics([r])["false_correlation_rate"] == 0.0


def test_report_is_written(tmp_path):
    rep = harness.run(scripted(), POLICY, only=["cpu_runaway"], out_dir=tmp_path, kb_source=None, progress=lambda *a, **k: None)
    md = pathlib.Path(rep["saved"]).read_text()
    assert "auto-remediation rate within the safe envelope | 1/1" in md and "cpu_runaway" in md


def test_flapping_fix_that_does_not_hold_goes_to_a_person_next(tmp_path):
    """Seen in the M8 harness 1 Oct: the first incident was the CPU anomaly, the recurrence the CPU alert; their
    signatures don't overlap, so the recurrence wasn't recognised and the restart ran automatically again."""
    r = go(tmp_path, "flapping")
    lanes = [i["lane"] for i in r["incidents"]]
    assert lanes[0] == "auto" and "auto" not in lanes[1:], lanes
    assert r["traps"]["flapping"]["ok"], r["traps"]
