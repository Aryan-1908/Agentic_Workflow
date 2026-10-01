"""The action allowlist (spec guardrail: "the Execution Agent can only invoke pre-registered remediation types, never
arbitrary commands"). Every action has a family (which remediation agent runs it), required parameters, a rollback
(or none), and is the only thing the executor will ever send to the environment."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionSpec:
    name: str
    family: str                      # compute | service | deployment | network | escalation | none
    description: str
    params: tuple[str, ...] = ()     # required parameter names
    rollback: str | None = None      # action that undoes it, if any
    target_param: str | None = None  # which parameter names the resource acted on
    max_value: dict = field(default_factory=dict)   # numeric bounds, e.g. {"size": 10}


SPECS = {s.name: s for s in [
    ActionSpec("vm.start", "compute", "start a stopped VM", ("vm",), target_param="vm"),
    ActionSpec("vm.reset", "compute", "hard-reset a hung VM", ("vm",), target_param="vm"),
    ActionSpec("vm.resize", "compute", "change a VM's machine type (restarts it)", ("vm", "size"), target_param="vm",
               max_value={"size": 2}),     # at most double the current capacity
    ActionSpec("service.restart", "service", "restart the application service on a VM", ("vm",), target_param="vm"),
    ActionSpec("logs.rotate", "service", "rotate and compress logs on a VM's boot disk", ("vm",), target_param="vm"),
    ActionSpec("mig.recreate_instance", "deployment", "replace one unhealthy instance in a managed instance group",
               ("vm",), target_param="vm"),
    ActionSpec("mig.resize", "deployment", "change the size of a managed instance group", ("service", "size"),
               target_param="service", max_value={"size": 10}),
    ActionSpec("mig.rollback", "deployment", "roll a managed instance group back to its previous instance template",
               ("service",), target_param="service"),
    ActionSpec("firewall.restore", "network", "re-create a firewall rule exactly as it was", ("rule",),
               target_param="rule"),
    ActionSpec("escalate", "escalation", "hand the incident to on-call with the diagnosis"),
    ActionSpec("none", "none", "no action needed"),
]}

ACTIONS = {name: s.description for name, s in SPECS.items()}      # name -> description (for prompts and checks)


def params_for(action: str, case, target: str | None, service: str | None) -> dict:
    """The action's parameters, taken from the case's own telemetry, never invented. Missing -> left out (the Safety
    Reviewer then blocks the action)."""
    spec = SPECS.get(action)
    if spec is None or not spec.params:
        return {}
    if action == "firewall.restore":      # the rule named in the firewall.rule_deleted event
        rule = next((s.raw.get("attributes", {}).get("rule") for s in case.signals
                     if s.metric == "event/firewall.rule_deleted" and s.raw.get("attributes", {}).get("rule")), None)
        return {"rule": rule} if rule else {}
    out = {}
    if "size" in spec.params:             # the runbook's bound: at most double the current capacity
        out["size"] = 2
    if "vm" in spec.params and target:
        out["vm"] = target
    if "service" in spec.params and service:
        out["service"] = service
    return out
