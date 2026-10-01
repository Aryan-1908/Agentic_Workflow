"""Recommendation + conditional routing (spec M5): "recommend (action + blast radius + confidence + rollback plan)
-> conditional routing: low blast-radius, high confidence -> auto-execute; elevated blast-radius / ambiguous ->
human-approval interrupt; safety-critical / low confidence -> hard-block auto-execution, escalate to on-call."

Routing is code, not a prompt, so the guardrail can't be talked around. Thresholds come from the client config
([routing] in clients/<name>.toml). M5 derives the recommendation from the grounded diagnosis; M6 replaces that with
the Remediation Recommendation Agent, which feeds the same route()."""
from dataclasses import dataclass, field

from pydantic import BaseModel

from .actions import params_for
from .config import services

LEVELS = ["none", "low", "medium", "high", "critical"]

# How much one run of each action can disturb, before the service's importance is added.
ACTION_SCOPE = {"none": 0, "escalate": 0,
                "vm.start": 1, "service.restart": 1, "logs.rotate": 1, "mig.recreate_instance": 1,
                "vm.reset": 2, "vm.resize": 2, "mig.resize": 2, "mig.rollback": 2, "firewall.restore": 2}


@dataclass
class RoutingPolicy:
    auto_max_blast: str = "low"               # highest blast radius that may run without a person
    auto_min_confidence: float = 0.8          # below this, a person approves even low-risk actions
    escalate_below_confidence: float = 0.4    # below this, no action is offered at all
    block_actions: list[str] = field(default_factory=list)   # never auto-run these, whatever the score

    @classmethod
    def from_config(cls, raw: dict | None):
        return cls(**(raw or {}))


class Recommendation(BaseModel):
    action: str
    target: str | None              # the VM / group the action applies to
    service: str | None
    blast_radius: str               # none | low | medium | high | critical
    blast_reason: str
    confidence: float
    rollback: str
    rationale: str
    citations: list[str]
    target_by: str = "rules"        # rules | llm (the recommender's LLM chose the target: a person must confirm)
    params: dict = {}               # the action's parameters, from the case's telemetry (copilot/actions.py)


class Route(BaseModel):
    lane: str                       # auto | approval | escalate | none
    reason: str


def blast_radius(action: str, service: str | None) -> tuple[str, str]:
    scope = ACTION_SCOPE.get(action, 4)
    if scope == 0:
        return "none", f"{action}: changes nothing"
    svc = services().get(service or "")
    tier = svc.tier if svc else None
    level = scope + (1 if tier == 1 else 0) + (1 if action not in ACTION_SCOPE else 0)
    why = f"{action} (scope {scope})" + (f" on a tier {tier} service" if tier else " on an unknown service")
    if svc is None:
        level += 1                  # we don't know what depends on it
    return LEVELS[min(level, 4)], why


def pick_target(case, step) -> tuple[str | None, str | None]:
    """What the action applies to. Never guessed: the resource the step names (if it names exactly one of the case's
    resources), else the resource of the case's root service. Unclear -> (None, root or None), and routing escalates.
    Seen 29 Sep: a merged case with no clear root proposed vm.start on a healthy VM (the first service in the list)."""
    owner = {s.resource["name"]: s.service for s in case.signals if s.resource.get("name")}
    named = [n for n in owner if step and n in step.text]
    if len(named) == 1:
        return named[0], owner[named[0]]
    if len(named) > 1 or not case.root_service:
        return None, case.root_service
    mine = sorted({n for n, svc in owner.items() if svc == case.root_service})
    return (mine[0], case.root_service) if len(mine) == 1 else (None, case.root_service)


def recommend(case, grounded, memory=None) -> Recommendation:
    """From the grounded diagnosis: its first remediation step, sized and scored."""
    d = grounded.diagnosis
    step = next((s for s in d.steps if s.action not in ("none",)), None) or (d.steps[0] if d.steps else None)
    action = step.action if step else "escalate"
    target, service = pick_target(case, step)
    params = params_for(action, case, target, service)
    if action == "firewall.restore":          # it acts on the rule, not on a VM
        target = params.get("rule")
    blast, why = blast_radius(action, service)
    confidence = d.confidence if grounded.grounded else min(d.confidence, 0.2)
    if memory is not None and action not in ("none", "escalate"):
        stats = memory.outcome_stats(action, service)
        confidence = max(0.0, min(1.0, confidence + 0.05 * stats.get("resolved", 0) - 0.2 * stats.get("failed", 0)))
    rollback = next((f"see {c.split('#')[0]}#rollback" for c in (step.citations if step else [])
                     if c.startswith("runbooks/")), "none documented")
    return Recommendation(action=action, target=target, service=service, blast_radius=blast, blast_reason=why,
                          confidence=round(confidence, 2), rollback=rollback,
                          rationale=step.text if step else "no grounded remediation step",
                          citations=step.citations if step else [], params=params)


def route(rec: Recommendation, grounded: bool, policy: RoutingPolicy) -> Route:
    if not grounded:
        return Route(lane="escalate", reason="no grounded root cause: insufficient knowledge")
    if rec.action == "escalate":
        return Route(lane="escalate", reason="the runbook says to escalate")
    if rec.confidence < policy.escalate_below_confidence:
        return Route(lane="escalate", reason=f"confidence {rec.confidence} below {policy.escalate_below_confidence}")
    if rec.target is None and rec.action not in ("none", "escalate"):
        return Route(lane="escalate", reason=f"{rec.action}: unclear which resource it applies to")
    if rec.blast_radius == "critical":
        return Route(lane="escalate", reason=f"safety-critical blast radius ({rec.blast_reason})")
    if rec.action == "none":
        return Route(lane="none", reason="grounded diagnosis: no action needed")
    if rec.action in policy.block_actions:
        return Route(lane="approval", reason=f"{rec.action} always needs a person (client policy)")
    if rec.target_by == "llm":
        return Route(lane="approval", reason=f"the target {rec.target} was chosen by the LLM: a person confirms it")
    if LEVELS.index(rec.blast_radius) > LEVELS.index(policy.auto_max_blast):
        return Route(lane="approval", reason=f"blast radius {rec.blast_radius} above auto limit {policy.auto_max_blast} ({rec.blast_reason})")
    if rec.confidence < policy.auto_min_confidence:
        return Route(lane="approval", reason=f"confidence {rec.confidence} below auto limit {policy.auto_min_confidence}")
    return Route(lane="auto", reason=f"blast radius {rec.blast_radius}, confidence {rec.confidence}")
