"""Diagnosis / RAG (spec M4): "retrieves grounded context from the organization's knowledge base ... to propose a
likely root cause, with groundedness evaluation so it never fabricates a cause or a step that doesn't exist."

    diagnose(case)  ->  retrieve runbook/postmortem sections (hybrid search)  ->  LLM drafts a Diagnosis with citations
                    ->  check_groundedness()  ->  GroundedDiagnosis  (the only thing shown to a person or passed on)

Groundedness rules, applied to the root cause and to every step:
  1. it cites at least one section
  2. every cited section is one that was actually retrieved for this case (no invented or outside citations)
  3. a step's action is a registered action type (copilot/actions.py)
  4. a step's action appears in a cited section: the runbook must contain that step, e.g. `vm.start`
  5. a judge (LLM) confirms the cited text supports the claim for this incident (catches a real step applied to
     the wrong situation, or a cause the text doesn't say)
Anything failing a rule is removed and listed with the reason. Without a grounded root cause the diagnosis is
"insufficient knowledge", which means escalate; the copilot never fills the gap itself."""
import json, re

from pydantic import BaseModel, Field

from . import resilience
from .actions import ACTIONS
from .config import services
from .correlation import UNKNOWN
from .signals import Severity

DRAFT_PROMPT = """You diagnose an incident for the operations team, using ONLY the knowledge-base sections below.

Incident:
{incident}

Similar past incidents (from memory):
{history}

Knowledge-base sections (cite them by their id in square brackets):
{sections}

Write the likely root cause and the remediation steps.
- The root cause names which documented situation this incident is (cite the section whose symptoms match it), even
  if the deeper reason is not known (e.g. "the storefront process crashed; why it was killed is not known"). Leave it
  empty only if no section matches the incident.
- Every claim must cite the section ids it comes from, in its citations field (not in the text). Cite only ids listed above.
- A step's action must be one of: {actions}. Use an action only if a cited section names it for this situation.
- If the sections don't explain this incident, say so in the summary, set confidence low and give the single step
  action "escalate" citing nothing but the closest section. Do not guess."""

JUDGE_PROMPT = """Does the SOURCE support the CLAIM for this incident?

Incident: {incident}
CLAIM: {claim}
SOURCE:
{source}

supported = true only if the source states it or it follows directly. If the source says this applies to a different
situation (another service, another kind of disk, another technology), or says the opposite, supported = false."""


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list, description="knowledge-base section ids")


class Step(BaseModel):
    text: str
    action: str = Field(description="registered action type")
    citations: list[str] = Field(default_factory=list)


class Diagnosis(BaseModel):
    summary: str
    root_cause: Claim | None
    steps: list[Step] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class Issue(BaseModel):
    where: str          # "root cause" or "step 2"
    claim: str
    problem: str


class GroundedDiagnosis(BaseModel):
    diagnosis: Diagnosis            # only the parts that passed
    removed: list[Issue]            # what was taken out, and why
    grounded: bool                  # a grounded root cause survived
    sources: list[str]              # the section ids retrieved for this case

    @property
    def status(self) -> str:
        return "grounded" if self.grounded else "insufficient knowledge: escalate"


class Verdict(BaseModel):
    supported: bool
    reason: str


def check_groundedness(d: Diagnosis, retrieved: dict[str, dict], judge=None, incident: str = "",
                       progress=None) -> GroundedDiagnosis:
    """retrieved: chunk_id -> chunk (with "text") for the sections that were retrieved for this case."""
    removed: list[Issue] = []
    total, done = (1 if d.root_cause else 0) + len(d.steps), [0]

    def problems(claim_text: str, citations: list[str], action: str | None) -> str | None:
        if not citations:
            return "no citation"
        unknown = [c for c in citations if c not in retrieved]
        if unknown:
            return f"cites sections that were not retrieved: {', '.join(unknown)}"
        if action is not None:
            if action not in ACTIONS:
                return f"unknown action type {action!r}"
            if action != "none" and not any(f"`{action}`" in retrieved[c]["text"] for c in citations):
                return f"no cited section contains the step `{action}`"
        if judge is not None:
            done[0] += 1
            if progress:
                progress(f"judge: checking claim {done[0]}/{total}")
            source = "\n\n".join(f"[{c}]\n{retrieved[c]['text']}" for c in citations)
            prompt = JUDGE_PROMPT.format(incident=incident, claim=claim_text, source=source)
            v = resilience.call("judge", lambda: judge.with_structured_output(Verdict).invoke(prompt))
            if not v.supported:
                return f"judge: not supported by the cited text ({v.reason})"
        return None

    root = d.root_cause
    if root is not None and (p := problems(root.text, root.citations, None)):
        removed.append(Issue(where="root cause", claim=root.text, problem=p))
        root = None
    steps = []
    for i, s in enumerate(d.steps, 1):
        if p := problems(f"{s.text} (action {s.action})", s.citations, s.action):
            removed.append(Issue(where=f"step {i}", claim=s.text, problem=p))
        else:
            steps.append(s)
    kept = d.model_copy(update={"root_cause": root, "steps": steps,
                                "confidence": d.confidence if root is not None else min(d.confidence, 0.2)})
    return GroundedDiagnosis(diagnosis=kept, removed=removed, grounded=root is not None, sources=list(retrieved))


def per_service_evidence(case) -> str:
    """What each service in the case looks like, one block per service.

    A flat list of signals makes the model compare lines; a per-service block makes it compare
    *services*, which is the question being asked — which one is the cause and which are affected.
    Each block carries that service's own measurements (cpu, disk, any gauge that arrived), its own
    errors, and whether it only relays someone else's failure.

    "relaying" is the distinction that matters: storefront logging
    "checkout failed: orders-api 503: no worker available" is a victim, not a cause, and without
    saying so the model sees an error on storefront and can blame it.
    """
    blocks = []
    for svc in case.services:
        mine = [s for s in case.signals if s.service == svc]
        if not mine:
            continue
        measured = {}
        for s in mine:
            if s.metric and s.value is not None:
                measured[s.metric] = s.value          # last value wins; they arrive in order
        own, relayed = [], []
        for s in mine:
            if s.severity == Severity.info:
                continue
            text = f"{s.title} {s.raw.get('body', '')}".lower()
            others = [o for o in case.services if o != svc and o != UNKNOWN]
            is_relay = ("database error" in text or "upstream" in text
                        or any(o.lower() in text for o in others))
            (relayed if is_relay else own).append(s.title)

        # Counts and measurements only — the signal text is already listed once above, and repeating
        # it here would make the same condition appear twice in the prompt.
        bits = []
        if measured:
            bits.append(", ".join(f"{k}={v}" for k, v in sorted(measured.items())))
        if own:
            bits.append(f"{len(set(own))} error(s) of its own")
        if relayed:
            bits.append(f"{len(set(relayed))} relaying another service's failure")
        if not own and not relayed:
            bits.append("no errors of its own")
        blocks.append(f"- {svc}: " + "; ".join(bits))
    return "\n".join(blocks)


def incident_text(case) -> str:
    """What the case is about, for retrieval and prompts.

    Keeps the flat signal list (retrieval matches against it) and adds the per-service breakdown, so
    the model can tell a saturated service from one that is merely downstream of it.
    """
    lines = [f"services: {', '.join(case.services)}; likely origin: {case.root_service or 'unknown'}"]
    seen = set()
    for s in case.signals:
        key = s.metric or s.title
        if key in seen:
            continue
        seen.add(key)
        tag = "context" if s.severity == Severity.info else s.severity.value
        lines.append(f"- [{tag}] {s.title}")
    by_service = per_service_evidence(case)
    if by_service:
        lines += ["", "per service (own measurements and errors, vs failures relayed from elsewhere):", by_service]
    return "\n".join(lines)


_APPLIES = re.compile(r"^Applies to:\s*(.+?)\.?\s*$", re.M)


def applies_to(path: str, root=None) -> set[str] | str:
    """A knowledge-base document's scope, from its "Applies to:" line: "all" (every service in the service map) or a
    set of service names (empty: it applies to none of ours, e.g. the Kubernetes or Cloud SQL decoys)."""
    from .kb.index import KB_DIR
    try:
        m = _APPLIES.search(((root or KB_DIR) / path).read_text())
    except OSError:
        return set()
    if not m:
        return set()
    text = m.group(1).strip()
    if text.lower().startswith("all services"):
        return "all"
    return {t.strip() for t in text.split(",") if t.strip() in services()}


def doc_applies(path: str, case_services, root=None) -> bool:
    scope = applies_to(path, root)
    known = [s for s in case_services if s in services()]
    return bool(known) if scope == "all" else bool(scope & set(case_services))


def diagnose(case, index, llm, judge=None, memory=None, docs: int = 3, reranker=None, progress=None) -> GroundedDiagnosis:
    say = progress or (lambda msg: None)
    incident = incident_text(case)
    query = " ".join(s.title for s in case.signals if s.severity != Severity.info) or incident
    say("searching the knowledge base")
    hits = resilience.call("knowledge-base search", lambda: index.documents_for(query, docs=docs * 3, reranker=reranker),
                           kind="embeddings")                    # whole runbooks/postmortems that match
    # Only documents that apply to this incident's services can ground it (seen live 29 Sep: a generic runbook was
    # stretched onto an unknown Redis service). None applies -> escalate, without calling the LLM (relevance floor).
    root = getattr(index, "root", None)
    paths = [p for p in dict.fromkeys(h["path"] for h in hits) if doc_applies(p, case.services, root)][:docs]
    hits = [h for h in hits if h["path"] in paths]
    if not hits:
        say(f"no runbook or postmortem applies to {', '.join(case.services)}: escalating without an LLM call")
        return GroundedDiagnosis(diagnosis=Diagnosis(
            summary=f"no runbook or postmortem in the knowledge base applies to {', '.join(case.services)}",
            root_cause=None, steps=[], confidence=0.0), removed=[], grounded=False, sources=[])
    retrieved = {h["chunk_id"]: h for h in hits}
    history = "none"
    if memory is not None:
        past = memory.similar_cases(case)
        if past:
            history = "\n".join(f"- {p['first_seen'][:16]} {', '.join(p['services'])} ({p['score']} similar); outcome: "
                                + ("; ".join(f"{o.get('action')}: {o.get('result')}" for o in p["outcomes"]) or "none recorded")
                                for p in past)
    sections = "\n\n".join(f"[{h['chunk_id']}]\n{h['text']}" for h in hits)
    say(f"drafting the diagnosis from {len({h['path'] for h in hits})} document(s)")
    prompt = DRAFT_PROMPT.format(incident=incident, history=history, sections=sections or "(none found)",
                                 actions=", ".join(ACTIONS))
    draft = resilience.call("diagnosis draft", lambda: llm.with_structured_output(Diagnosis).invoke(prompt))
    return check_groundedness(draft, retrieved, judge=judge, incident=incident, progress=progress)


_INLINE_CITE = re.compile(r"\s*\[[\w./-]+#[\w-]+\]")


def _clean(text: str) -> str:
    """Drop section ids the model also wrote into the text; they're shown once, from the citations field."""
    return _INLINE_CITE.sub("", text).strip()


def reuse_known(case, memory) -> GroundedDiagnosis | None:
    """A repeat of a known incident (same signature as a past case) reuses that case's checked diagnosis: no LLM call.
    Spec: "recurring issues get faster, more confident recommendations". Not reused if the last time was rejected or
    failed (something is different: think again), or if that diagnosis wasn't grounded."""
    for past in memory.similar_cases(case, k=3, min_score=1.0):
        outcome = " ".join(str(o.get("result", "")) for o in past["outcomes"])
        if "rejected" in outcome or "failed" in outcome:
            return None
        rec = next((e for e in reversed(memory.events(past["case_id"])) if e["kind"] == "diagnosis"), None)
        if rec and rec.get("grounded"):
            g = GroundedDiagnosis.model_validate({k: v for k, v in rec.items() if k not in ("kind", "at", "status")})
            original = re.sub(r"^(\[same as [\w-]+\] )+", "", g.diagnosis.summary)     # don't stack prefixes
            g.diagnosis.summary = f"[same as {past['case_id']}] {original}"
            return g
    return None


def diagnose_or_reuse(case, index, llm, judge=None, memory=None, progress=None, **kw) -> GroundedDiagnosis:
    """The cost rule: reuse a known incident's diagnosis; call the LLM only for what's new; out of budget -> escalate."""
    from .usage import BudgetExceeded
    say = progress or (lambda msg: None)
    if memory is not None and (g := reuse_known(case, memory)):
        past = re.match(r"\[same as ([\w-]+)\]", g.diagnosis.summary)
        say(f"known incident: reusing the checked diagnosis of {past.group(1) if past else 'a past case'} (no Gemini call)")
        return g
    try:
        return diagnose(case, index, llm, judge=judge, memory=memory, progress=progress, **kw)
    except BudgetExceeded as e:
        say(str(e))
        return GroundedDiagnosis(diagnosis=Diagnosis(summary=f"{e}; escalated with the rule-based information",
                                                     root_cause=None, steps=[], confidence=0.0),
                                 removed=[], grounded=False, sources=[])


def render(g: GroundedDiagnosis, indent: str = "          ") -> str:
    d = g.diagnosis
    out = [f"{indent}diagnosis ({g.status}, confidence {d.confidence:.1f}): {_clean(d.summary)}"]
    if d.root_cause:
        out.append(f"{indent}  cause: {_clean(d.root_cause.text)}  [{', '.join(d.root_cause.citations)}]")
    for i, s in enumerate(d.steps, 1):
        out.append(f"{indent}  step {i}: {s.action} - {_clean(s.text)}  [{', '.join(s.citations)}]")
    for r in g.removed:
        out.append(f"{indent}  removed ({r.where}): {r.claim[:80]} -> {r.problem}")
    return "\n".join(out)


def to_record(g: GroundedDiagnosis) -> dict:
    return json.loads(g.model_dump_json()) | {"status": g.status}
