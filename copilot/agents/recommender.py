"""Recommender agent (spec: Remediation Recommendation Agent, "proposes a concrete action (restart, scale, rollback,
config change, or 'escalate only') with a stated blast radius, confidence, and rollback plan").

Code first (copilot/routing.py: the grounded step's action, its target, blast radius, confidence adjusted by past
outcomes, rollback from the runbook). Gemini is asked only when stuck: the action needs a target and the code can't
tell which resource (e.g. a case with two independent origins). Even then it may only pick one of the case's own
resources, or none; anything else is ignored and the case escalates."""
from pydantic import BaseModel, Field

from .. import resilience
from ..actions import params_for
from ..routing import Recommendation, blast_radius, recommend

PICK_PROMPT = """An incident's grounded diagnosis recommends this step:
  action: {action}
  step: {step}

Diagnosis: {summary}
Evidence:
{evidence}

Which ONE of these resources does the step apply to? {candidates}
Answer with exactly one of them, or null if the evidence doesn't make it clear. Do not guess."""


class _Pick(BaseModel):
    resource: str | None = Field(description="one of the listed resources, or null")


class Recommender:
    def __init__(self, llm=None, memory=None, progress=None):
        self.llm, self.memory, self.progress = llm, memory, progress

    def recommend(self, case, investigation) -> Recommendation:
        g = investigation.diagnosis
        rec = recommend(case, g, self.memory)
        if rec.target is None and rec.action not in ("none", "escalate") and g.grounded and self.llm is not None:
            rec = self._pick_target(case, investigation, rec)
        return rec

    def _pick_target(self, case, inv, rec: Recommendation) -> Recommendation:
        owner = {s.resource["name"]: s.service for s in case.signals if s.resource.get("name")}
        if not owner:
            return rec
        if self.progress:
            self.progress(f"recommender: unclear which resource {rec.action} applies to; asking Gemini to choose "
                          f"among {len(owner)} of the case's resources")
        step = next((s for s in inv.diagnosis.diagnosis.steps if s.action == rec.action), None)
        prompt = PICK_PROMPT.format(action=rec.action, step=step.text if step else rec.rationale,
                                    summary=inv.diagnosis.diagnosis.summary, evidence=inv.evidence,
                                    candidates=", ".join(sorted(owner)))
        try:
            answer = resilience.call("recommender target", lambda: self.llm.with_structured_output(_Pick).invoke(prompt))
        except Exception:                 # after retries (or out of budget): no target, so the case escalates
            return rec
        picked = answer.resource if answer and answer.resource in owner else None      # only the case's own resources
        if picked is None:
            return rec
        blast, why = blast_radius(rec.action, owner[picked])
        return rec.model_copy(update={"target": picked, "service": owner[picked], "blast_radius": blast,
                                      "blast_reason": why, "target_by": "llm",
                                      "params": params_for(rec.action, case, picked, owner[picked])})
