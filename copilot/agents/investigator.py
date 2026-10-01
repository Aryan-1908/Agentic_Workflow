"""Investigator agent (spec M6 step 1: "one that investigates"): turns a correlated case into an Investigation,
the structured handoff to the Recommender.

    evidence   what the case is (services, likely origin, each distinct signal once)
    history    similar past incidents from memory, with how they ended
    diagnosis  a grounded diagnosis: reused from a known incident (no LLM), or drafted by Gemini from the knowledge
               base and checked (copilot/diagnosis.py); out of LLM budget -> ungrounded, so it escalates"""
from pydantic import BaseModel, Field

from ..diagnosis import GroundedDiagnosis, diagnose_or_reuse, incident_text


class Investigation(BaseModel):
    case_id: str
    evidence: str
    history: list[dict] = Field(default_factory=list)     # similar past cases: id, when, similarity, outcomes
    diagnosis: GroundedDiagnosis
    reused_from: str | None = None                        # past case whose checked diagnosis was reused (0 LLM calls)


class Investigator:
    def __init__(self, index, llm, judge=None, memory=None, progress=None):
        self.index, self.llm, self.judge, self.memory, self.progress = index, llm, judge, memory, progress

    def investigate(self, case) -> Investigation:
        history = self.memory.similar_cases(case) if self.memory is not None else []
        g = diagnose_or_reuse(case, self.index, self.llm, judge=self.judge, memory=self.memory, progress=self.progress)
        reused = g.diagnosis.summary[len("[same as "):].split("]")[0] if g.diagnosis.summary.startswith("[same as ") else None
        return Investigation(case_id=case.case_id, evidence=incident_text(case), diagnosis=g, reused_from=reused,
                             history=[{k: h[k] for k in ("case_id", "first_seen", "score", "outcomes")} for h in history])
