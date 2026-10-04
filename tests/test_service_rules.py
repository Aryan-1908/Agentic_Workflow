"""Service-level rules: the same action means different things on different services.

The existing suite tests incident *shapes* (a VM stopped, a disk filled, a cascade). This file tests
the other axis — what a service IS — because the same symptom and the same action are safe on one
service and unsafe on another:

    disk full on a web server      -> rotate the logs
    disk full on a database        -> escalate; its files are data
    restart a stateless service    -> fine
    restart a database             -> drops every in-flight transaction
    anything on a third party      -> not ours to act on, at any confidence

config/services.toml already carries the three facts that decide this — tier, data, external — and
these tests pin that each one actually changes behaviour.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from copilot.agents.safety import DATA_UNSAFE, SafetyReviewer  # noqa: E402
from copilot.config import services  # noqa: E402
from copilot.correlation import Case  # noqa: E402
from copilot.routing import LEVELS, Recommendation, RoutingPolicy, blast_radius, route  # noqa: E402
from copilot.signals import IncidentSignal, Severity, SignalType  # noqa: E402

POLICY = RoutingPolicy(auto_max_blast="low", auto_min_confidence=0.8, escalate_below_confidence=0.4)

OWN = ["storefront", "orders-api", "orders-db", "reports-batch"]
DATA_SERVICES = [n for n, s in services().items() if getattr(s, "data", False)]
EXTERNAL = [n for n, s in services().items() if getattr(s, "external", False)]
TIER1 = [n for n, s in services().items() if s.tier == 1 and not getattr(s, "external", False)]
TIER3 = [n for n, s in services().items() if s.tier == 3]


def rec(action, service, target="x-01", confidence=0.95, target_by="rules"):
    blast, why = blast_radius(action, service)
    return Recommendation(action=action, target=target, service=service, blast_radius=blast,
                          blast_reason=why, confidence=confidence, rollback="none",
                          rationale="test", citations=["runbooks/x.md#r"], target_by=target_by,
                          params={"vm": target})


def _case(service, host="x-01"):
    sig = IncidentSignal(source="alert", service=service, severity=Severity.error,
                         timestamp=__import__("datetime").datetime.now(
                             __import__("datetime").timezone.utc),
                         signal_type=SignalType.alert, signal_id="s1", title="test",
                         resource={"name": host})
    now = sig.timestamp
    return Case(case_id="c1", signals=[sig], opened_at=now, last_placed_at=now)


# ---- tier changes the lane, not the diagnosis ---------------------------------------------------

@pytest.mark.parametrize("service", TIER1)
def test_a_customer_facing_service_never_auto_executes_a_restart(service):
    """Tier 1 is customer-facing: restarting one is never unattended under this policy."""
    assert route(rec("service.restart", service), True, POLICY).lane != "auto"


def test_the_same_action_is_riskier_on_a_tier_1_service_than_a_tier_3_one():
    """The only difference is the service's importance, so the blast radius must reflect it."""
    hot = blast_radius("service.restart", "storefront")[0]      # tier 1
    cold = blast_radius("service.restart", "reports-batch")[0]  # tier 3
    assert LEVELS.index(hot) > LEVELS.index(cold)


# ---- data services ------------------------------------------------------------------------------

@pytest.mark.parametrize("action", DATA_UNSAFE)
@pytest.mark.parametrize("service", DATA_SERVICES)
def test_interrupting_a_data_service_is_never_allowed(action, service):
    """Every action that interrupts something running is refused on a service holding data.

    Being right about the service does not make the action safe: a database can be correctly
    identified as the problem and still must not be restarted, because the restart is what loses
    the data.
    """
    v = SafetyReviewer(POLICY).review(rec(action, service, target="db-01"), "approval",
                                      grounded=True, case=_case(service, "db-01"),
                                      approved_by="someone", grounded_actions={action})
    assert not v.allowed and "holds data" in v.reason


@pytest.mark.parametrize("service", DATA_SERVICES)
def test_starting_a_stopped_data_service_is_still_allowed(service):
    """vm.start is not an interruption: there is nothing in flight to lose, and it is the right fix
    when a database VM is down. The guard is on what the action does, not on which service it is."""
    v = SafetyReviewer(POLICY).review(rec("vm.start", service, target="db-01"), "approval",
                                      grounded=True, case=_case(service, "db-01"),
                                      approved_by="someone", grounded_actions={"vm.start"})
    assert v.allowed, v.reason


# ---- third-party services -----------------------------------------------------------------------

@pytest.mark.parametrize("service", EXTERNAL)
@pytest.mark.parametrize("action", ["vm.start", "service.restart", "vm.reset", "mig.rollback"])
def test_nothing_executes_against_a_third_party(service, action):
    """A provider's infrastructure is not ours to act on, at any confidence.

    The spec's use case U9 is "external provider outage -> escalate (not ours to fix)". The service
    map already marks these external; this pins that the marking actually blocks execution rather
    than only describing the service.
    """
    v = SafetyReviewer(POLICY).review(rec(action, service), "approval", grounded=True,
                                      case=_case(service), approved_by="someone",
                                      grounded_actions={action})
    assert not v.allowed, f"{action} on the third party {service} was allowed: {v.reason}"


@pytest.mark.parametrize("service", EXTERNAL)
def test_a_third_party_incident_is_escalated_not_actioned(service):
    assert route(rec("escalate", service), True, POLICY).lane == "escalate"


# ---- unknown services ---------------------------------------------------------------------------

def test_an_unknown_service_is_treated_as_more_dangerous_not_less():
    """Nothing is known about what depends on it, so the blast radius goes up, not down."""
    known = blast_radius("service.restart", "reports-batch")[0]
    unknown = blast_radius("service.restart", "service-we-have-never-seen")[0]
    assert LEVELS.index(unknown) > LEVELS.index(known)


# ---- an action nobody registered ----------------------------------------------------------------

def test_an_unregistered_action_is_refused_on_every_service():
    for service in OWN:
        v = SafetyReviewer(POLICY).review(rec("rm -rf /", service), "approval", grounded=True,
                                          case=_case(service), approved_by="someone",
                                          grounded_actions={"rm -rf /"})
        assert not v.allowed and "allowlist" in v.reason
