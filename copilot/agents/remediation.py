"""Action Execution agent + the remediation agents it dispatches to, one per action family (decided 29 Sep).

    ExecutionAgent.run(recommendation)
      1. Safety Reviewer (code): allowlist, grounding, parameters, bounds, lane/approval, data services, circuit breaker
      2. the family's remediation agent checks its preconditions against the incident's own telemetry
      3. sends the action to the environment (copilot/cloud.py) and verifies from the telemetry (copilot/agents/verify.py)
      4. resolved -> done; failed / not applied -> escalate; ambiguous -> ask the LLM once (wait / escalate), no LLM -> escalate

Remediation agents are code. The only LLM call is (4) when recovery is ambiguous (cost rule: "LLM only when stuck")."""
from pydantic import BaseModel, Field

from .. import resilience
from ..actions import SPECS
from ..otel import OTelReader
from .safety import SafetyReviewer
from .verify import Verifier


def _evidence(case, host: str | None, *metrics_or_titles: str) -> bool:
    """Does the case contain a signal on this host whose metric or title mentions one of the words?"""
    for s in case.signals:
        if host and s.resource.get("name") != host:
            continue
        text = f"{s.metric or ''} {s.title}".lower()
        if any(w.lower() in text for w in metrics_or_titles):
            return True
    return False


class RemediationAgent:
    family = ""

    def precheck(self, action: str, params: dict, case) -> str | None:
        """None if the incident's telemetry supports doing this; otherwise why not."""
        return None


class ComputeAgent(RemediationAgent):
    family = "compute"

    def precheck(self, action, params, case):
        vm = params.get("vm")
        if action == "vm.start" and not _evidence(case, vm, "event/vm.stopped", "VM not reporting"):
            return f"no evidence in the incident that {vm} is stopped"
        if action == "vm.resize" and not _evidence(case, vm, "cpu", "memory"):
            return f"no evidence of a capacity problem on {vm}"
        return None


class ServiceAgent(RemediationAgent):
    family = "service"

    def precheck(self, action, params, case):
        vm = params.get("vm")
        if _evidence(case, vm, "event/vm.stopped") and not _evidence(case, vm, "event/vm.started"):
            return f"{vm} is stopped: restarting a service on it can't work (the VM needs vm.start)"
        if action == "logs.rotate" and not _evidence(case, vm, "disk", "filesystem", "No space left"):
            return f"no evidence that {vm}'s disk is filling up"
        if action == "service.restart" and not _evidence(case, vm, "log/", "alert/", "anomaly", "cpu", "trace/"):
            return f"no error or alert on {vm} to restart for"
        return None


class DeploymentAgent(RemediationAgent):
    family = "deployment"

    def precheck(self, action, params, case):
        if action == "mig.rollback" and not _evidence(case, None, "event/deploy.rollout"):
            return "no release rollout in the incident: nothing to roll back"
        return None


class NetworkAgent(RemediationAgent):
    family = "network"

    def precheck(self, action, params, case):
        if action == "firewall.restore" and not _evidence(case, None, "event/firewall.rule_deleted"):
            return "no firewall rule deletion in the incident"
        return None


AGENTS = {a.family: a for a in (ComputeAgent(), ServiceAgent(), DeploymentAgent(), NetworkAgent())}


class ExecutionResult(BaseModel):
    executed: bool
    result: str                  # resolved | failed | not_applied | blocked | ambiguous
    detail: str
    escalate: bool = False
    agent: str | None = None
    request_id: str | None = None
    action: str = ""
    target: str | None = None
    sent: bool = False           # an action actually went to the environment (the circuit breaker counts these)
    llm_decision: str | None = None
    steps: list[str] = Field(default_factory=list)


class _Decision(BaseModel):
    decision: str = Field(description="wait (give it more time) or escalate")
    reason: str


class ExecutionAgent:
    def __init__(self, cloud, telemetry: str, safety: SafetyReviewer, verifier: Verifier | None = None, llm=None,
                 progress=None):
        self.cloud, self.telemetry, self.safety = cloud, telemetry, safety
        self.verifier, self.llm = verifier or Verifier(), llm
        self.say = progress or (lambda msg: None)

    def run(self, rec, case, lane: str, grounded: bool, grounded_actions: set[str],
            approved_by: str | None = None) -> ExecutionResult:
        base = {"action": rec.action, "target": rec.target}
        alerts = [s for s in case.signals if s.signal_type.value == "alert" and s.state == "open"]
        if alerts and case.all_clear:          # spec trap "self-healing blip": nothing left to fix
            return ExecutionResult(executed=False, result="recovered", escalate=False,
                                   detail="every alert closed before acting: it recovered by itself, no action taken", **base)
        verdict = self.safety.review(rec, lane, grounded, case, approved_by, grounded_actions)
        if not verdict.allowed:
            return ExecutionResult(executed=False, result="blocked", detail=f"safety reviewer: {verdict.reason}",
                                   escalate=True, **base)
        spec = SPECS[rec.action]
        agent = AGENTS[spec.family]
        why = agent.precheck(rec.action, rec.params, case)
        if why:
            return ExecutionResult(executed=False, result="blocked", detail=f"{agent.family} agent: {why}",
                                   escalate=True, agent=agent.family, **base)
        reader = OTelReader(self.telemetry, from_start=False)        # only what happens from now on
        self.say(f"{agent.family} agent: sending {rec.action} {rec.params}")
        actor = f"copilot/{agent.family}-agent" + (f" (approved by {approved_by})" if approved_by else "")
        try:
            rid = resilience.call("send action", lambda: self.cloud.apply(rec.action, rec.params, actor=actor), kind="cloud")
        except Exception as e:
            return ExecutionResult(executed=False, result="failed", escalate=True, agent=agent.family,
                                   detail=f"could not send {rec.action} after {resilience.ATTEMPTS} attempts: "
                                          f"{type(e).__name__}: {e}", **base)
        self.say(f"{agent.family} agent: verifying from the telemetry")
        v = self.verifier.verify(reader, rid, case)
        common = dict(executed=True, agent=agent.family, request_id=rid, sent=True, **base)
        if v.outcome == "ambiguous" and self.llm is not None:
            d = self._decide(rec, v)
            if d.decision == "wait":
                self.say(f"{agent.family} agent: LLM says wait ({d.reason}); verifying once more")
                v2 = self._verify_more(reader, rid, case, v)
                if v2.outcome == "resolved":
                    return ExecutionResult(result="resolved", detail=v2.detail, llm_decision=f"wait: {d.reason}", **common)
                v = v2
            return ExecutionResult(result=v.outcome, detail=v.detail, escalate=True,
                                   llm_decision=f"{d.decision}: {d.reason}", **common)
        return ExecutionResult(result=v.outcome, detail=v.detail, escalate=v.outcome != "resolved", **common)

    def _verify_more(self, reader, rid, case, first):
        """Second look after an ambiguous result: the action is already confirmed, so only recovery is checked."""
        from .verify import Verification
        remaining = set(first.open_alerts)
        quiet = 0
        # a fresh window, and at least long enough to see the required quiet checks
        window = max(self.verifier.max_polls, 2 * self.verifier.quiet_polls)
        for _ in range(min(window, max(1, int(self.verifier.timeout / max(self.verifier.poll, 0.001))))):
            self.verifier.wait()
            new = reader.poll()
            remaining -= {s.signal_id.rsplit(":", 1)[0] for s in new if getattr(s, "state", "") == "closed"}
            errs = [s for s in new if getattr(s, "service", None) in case.services
                    and getattr(s, "severity", None) is not None and s.severity.value in ("error", "critical")
                    and s.signal_type.value != "alert"]
            quiet = 0 if errs else quiet + 1
            if not remaining and quiet >= self.verifier.quiet_polls:
                return Verification("resolved", "recovered after waiting", True)
        why = f"{len(remaining)} alert(s) still open" if remaining else "errors still arriving"
        return Verification("failed", f"still not recovered after waiting: {why}", True, [], sorted(remaining))

    def _decide(self, rec, v) -> _Decision:
        self.say("execution: recovery is ambiguous; asking Gemini whether to wait or escalate")
        prompt = (f"An automatic remediation was applied: {rec.action} on {rec.target}.\n"
                  f"Verification so far: {v.detail}. Alerts still open: {v.open_alerts or 'none'}.\n"
                  "Should we wait longer for recovery, or escalate to on-call now? Choose wait only if the evidence "
                  "shows recovery in progress.")
        try:
            d = resilience.call("remediation decision", lambda: self.llm.with_structured_output(_Decision).invoke(prompt))
            return d if d.decision in ("wait", "escalate") else _Decision(decision="escalate", reason="invalid answer")
        except Exception as e:          # budget used up, timeout: be safe
            return _Decision(decision="escalate", reason=f"no LLM decision ({type(e).__name__})")
