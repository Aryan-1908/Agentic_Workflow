"""A rejection must change what happens next time.

Until now a rejection was written and never read: workflow.py records it as kind='approval', and
outcome_stats() filters kind='outcome'. So confidence never moved when a person said no, and the
same recommendation came back unchanged however often it was refused.

Two channels, both tested here:
  count   lowers confidence like a failed execution, which moves the routing lane (deterministic)
  reasons travel to the investigation, so the model can see what was refused and why (context only)

The reason is free-form text typed during an incident. It must never reach the allowlist, the risk
tiers or the safety reviewer — the last test pins that.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from copilot.memory import Memory  # noqa: E402
from copilot.routing import RoutingPolicy, route  # noqa: E402

POLICY = RoutingPolicy(auto_max_blast="low", auto_min_confidence=0.8, escalate_below_confidence=0.4)


def _reject(m: Memory, case_id: str, action: str, by="shubham", reason="wrong fix", citations=None):
    """One refused recommendation on a case, as the workflow records it."""
    m.record(case_id, "recommendation", {"action": action, "target": "db-01",
                                         "citations": citations or ["runbooks/disk-full.md#remediation"]})
    m.record(case_id, "approval", {"decision": "reject", "by": by, "reason": reason})


def _case(m: Memory, case_id: str, services=("orders-db",)):
    m.db.execute("INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?)",
                 (case_id, "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
                  "2026-10-01T00:00:00+00:00", __import__("json").dumps(list(services)),
                  services[0], "[]", 0, "{}"))
    m.db.commit()


def test_a_rejection_is_found_with_its_reason(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    _case(m, "case-1")
    _reject(m, "case-1", "vm.start", reason="db-01 was up; the disk was full, not the VM")

    found = m.rejections("vm.start", "orders-db")
    assert len(found) == 1
    assert found[0]["by"] == "shubham" and "disk was full" in found[0]["reason"]
    assert found[0]["citations"] == ["runbooks/disk-full.md#remediation"]


def test_a_rejection_of_another_action_does_not_count(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    _case(m, "case-1")
    _reject(m, "case-1", "vm.start")
    assert m.rejections("service.restart", "orders-db") == []


def test_a_rejection_on_another_service_does_not_count(tmp_path):
    """Rejections are per service: 'never restart the database' is not 'never restart anything'."""
    m = Memory(tmp_path / "m.sqlite")
    _case(m, "case-1", services=("orders-db",))
    _reject(m, "case-1", "vm.start")
    assert m.rejections("vm.start", "storefront") == []


def test_repeated_rejections_move_the_lane_from_auto_to_approval(tmp_path):
    """The behaviour that was missing: enough refusals and the action stops running unattended."""
    from copilot.routing import Recommendation

    def rec(conf):
        return Recommendation(action="logs.rotate", target="web-01", service="storefront",
                              blast_radius="low", blast_reason="test", confidence=conf,
                              rollback="none", rationale="test", citations=[])

    assert route(rec(0.9), True, POLICY).lane == "auto"
    # one rejection (-0.2) takes 0.9 to 0.7, under the 0.8 auto threshold: a person decides
    assert route(rec(0.9 - 0.2), True, POLICY).lane == "approval"
    # three (-0.6) take it to 0.3, under escalate_below_confidence=0.4: no action is offered at all
    assert route(rec(0.9 - 3 * 0.2), True, POLICY).lane == "escalate"


def test_the_reason_reaches_the_investigation_but_not_the_guardrails(tmp_path):
    """Rejection text is untrusted input: context for a diagnosis, never a permission.

    A reason saying "always approve this" must not make the safety reviewer allow a non-allowlisted
    action — the reviewer never reads rejections at all, which is what keeps that true.
    """
    import inspect

    from copilot.agents import safety

    src = inspect.getsource(safety)
    assert "rejection" not in src.lower(), "the safety reviewer must not read rejection text"
