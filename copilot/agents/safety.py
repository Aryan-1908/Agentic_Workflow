"""Safety / Guardrail Reviewer agent (spec: "hard-blocks any auto-executed action above a configured blast-radius
threshold without human sign-off, blocks fabricated remediation steps, and enforces an allowlist of executable action
types"). Code, not a prompt: nothing the LLM writes can argue past it. It runs right before execution, every time."""
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from ..actions import SPECS
from ..config import services
from ..routing import LEVELS, Recommendation, RoutingPolicy

BREAKER_LIMIT, BREAKER_WINDOW = 3, timedelta(hours=1)     # at most 3 attempts per action+target per hour

# Actions that INTERRUPT a running service. On a service holding data (services.toml: data = true)
# each drops in-flight transactions, so none may run unattended whatever the diagnosis says.
# vm.start is deliberately absent: it starts something already stopped, so there is nothing in
# flight to lose, and it is the correct fix when a database VM is down (scenario db_down).
DATA_UNSAFE = ("logs.rotate", "vm.reset", "service.restart")


class Verdict(BaseModel):
    allowed: bool
    reason: str


class SafetyReviewer:
    def __init__(self, policy: RoutingPolicy | None = None, memory=None):
        self.policy, self.memory = policy or RoutingPolicy(), memory

    def review(self, rec: Recommendation, lane: str, grounded: bool, case, approved_by: str | None = None,
               grounded_actions: set[str] | None = None) -> Verdict:
        """grounded_actions: the actions of the diagnosis steps that passed the groundedness check (each one exists
        in a cited runbook section)."""
        def no(why):
            return Verdict(allowed=False, reason=why)

        spec = SPECS.get(rec.action)
        if spec is None or spec.family in ("none", "escalation"):
            return no(f"{rec.action!r} is not an executable action on the allowlist")
        if not grounded:
            return no("the diagnosis is not grounded")
        if rec.action not in (grounded_actions or set()):
            return no(f"`{rec.action}` is not one of the grounded diagnosis steps (fabricated step)")
        missing = [p for p in spec.params if not rec.params.get(p)]
        if missing:
            return no(f"missing parameter(s) {', '.join(missing)}: not found in the case's telemetry")
        for p, top in spec.max_value.items():
            try:
                if not 0 < float(rec.params[p]) <= top:
                    return no(f"{p}={rec.params[p]} is out of bounds (0 < {p} <= {top})")
            except (TypeError, ValueError):
                return no(f"{p}={rec.params[p]!r} is not a number")
        resources = {s.resource.get("name") for s in case.signals}
        if spec.target_param == "vm" and rec.params["vm"] not in resources:
            return no(f"VM {rec.params['vm']} is not part of this incident")
        svc = services().get(rec.service or "")
        # A third party's infrastructure is not ours to act on, at any confidence: we have no
        # credentials for it and no right to use them. The service map already marks these external
        # (config/services.toml [external.*]) and the spec's U9 says an external outage is
        # "escalate (not ours to fix)", but nothing enforced it — vm.reset on the payments provider
        # was allowed. Escalation and "none" are fine: they change nothing on their side.
        if svc is not None and getattr(svc, "external", False) and spec.family not in ("none", "escalation"):
            return no(f"{rec.service} is a third-party service: {rec.action} is not ours to run (escalate)")
        # Anything that interrupts a service holding data drops its in-flight transactions, so it is
        # never automatic. service.restart was missing here: a saturated database (connection slots
        # full, CPU and disk normal) is diagnosed correctly and then restarted, which is the one
        # action that loses data. The guard is on what the action DOES, not on which runbook found it.
        if rec.action in DATA_UNSAFE and svc is not None and svc.data:
            return no(f"{rec.action} on {rec.service} is never automatic: it holds data (runbook: escalate)")
        if lane == "auto":
            if LEVELS.index(rec.blast_radius) > LEVELS.index(self.policy.auto_max_blast):
                return no(f"blast radius {rec.blast_radius} is above the auto limit {self.policy.auto_max_blast} "
                          "and nobody approved it")
            if rec.target_by != "rules":
                return no("the target was chosen by the LLM and nobody approved it")
        elif lane == "approval" and not approved_by:
            return no("the action needs approval and has none")
        elif lane not in ("auto", "approval"):
            return no(f"lane {lane} never executes")
        if self.memory is not None:
            since = datetime.now(timezone.utc) - BREAKER_WINDOW
            n = self.memory.recent_executions(rec.action, rec.target, since)
            if n >= BREAKER_LIMIT:
                return no(f"circuit breaker: {rec.action} on {rec.target} already tried {n} times in the last hour")
        return Verdict(allowed=True, reason="allowed")
