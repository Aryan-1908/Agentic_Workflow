"""Signal Correlation (spec M2): turns a stream of IncidentSignals into incident cases.

Clear-cut decisions are rules, so they are predictable and testable:
  1. duplicate    the exact signal id was already seen (repeat polls, re-deliveries)            -> dropped
  2. closing      an alert's "closed" signal                                                     -> marks the open one closed
  3. repeat       same condition (source, service, metric/title, resource) already in a case     -> counted, not re-added
  4. related      same service, or one depends on the other, and within WINDOW of the case       -> merged (with the reason)
                  a signal related to several open cases joins them into one (a shared upstream cause)
                  exception: a local symptom (CPU/memory/disk) on a service *downstream* of the case is not merged
                  through the dependency: an upstream outage explains downstream errors, not downstream CPU
  5. info         an info-level event (VM started, scheduled stop) never opens a case: it joins a related case,
                  or is kept as context and pulled into a related case that opens within WINDOW (spec: dedupe noise)
  6. otherwise    a new case
Uncertain signals (unknown service, or related but between WINDOW and LATE_WINDOW) go to the Correlation Agent,
an LLM with the spec's tools. It may only merge into a case that exists; without an agent they open their own
case flagged needs_review, because a wrong merge is worse than a missed one."""
import json, uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel, Field

from . import resilience
from .config import dependency_path, services
from .signals import IncidentSignal, Severity

WINDOW = timedelta(minutes=15)
LATE_WINDOW = timedelta(minutes=45)
UNKNOWN = "unknown"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def condition_key(s: IncidentSignal) -> str:
    """Two signals with the same key describe the same condition (a re-fired alert, a status that got worse)."""
    where = s.resource.get("name") or s.resource.get("host") or ""
    return "|".join([s.source, s.service, s.metric or s.title, where])


class Case(BaseModel):
    case_id: str
    state: str = "open"                              # open | merged (into another case) | resolved
    resolved_at: datetime | None = None              # signal time the incident was resolved (fixed / no action)
    signals: list[IncidentSignal] = Field(default_factory=list)
    repeats: dict[str, int] = Field(default_factory=dict)       # condition_key -> extra occurrences
    closed: list[str] = Field(default_factory=list)             # ids of open signals whose alert has closed
    rationale: list[str] = Field(default_factory=list)          # why each signal is here
    needs_review: bool = False
    opened_at: datetime                              # processing time: first signal received
    last_placed_at: datetime                         # processing time: latest signal placed in this case

    @property
    def services(self) -> list[str]:
        return sorted({s.service for s in self.signals})

    @property
    def first_seen(self) -> datetime:
        return min(s.timestamp for s in self.signals)

    @property
    def last_seen(self) -> datetime:
        return max(s.timestamp for s in self.signals)

    @property
    def root_service(self) -> str | None:
        """The service to diagnose first. A hint for diagnosis, not a diagnosis.

        Normally the most upstream service in the case: one the others depend on and that depends on
        none of them. That is right for "something broke" — an outage explains its downstream errors.

        It is wrong for "something is full". When a service runs out of workers or connections, the
        requests pile up against a dependency that is busy but perfectly healthy, and that dependency
        reports slow queries of its own. Both land in the case, the healthy one is upstream, and the
        graph blames it (seen in pool_exhaustion_db_noisy: orders-api is out of workers, orders-db is
        named the root and restarted).

        So evidence wins over topology: a service reporting its OWN resource exhaustion is the
        saturated one, and no dependency of it can be more responsible. Only when exactly one service
        says this does it override the graph — two would be ambiguous, and ambiguity belongs with a
        person, not with a tie-break rule.
        """
        svcs = [s for s in self.services if s != UNKNOWN]
        saturated = [s for s in svcs if self._is_saturated(s)]
        if len(saturated) == 1:
            return saturated[0]
        roots = [s for s in svcs if not any(o != s and dependency_path(s, o) for o in svcs)]
        if len(roots) == 1:
            return roots[0]
        return None

    #: Phrases where a service reports ITS OWN capacity being exhausted. Deliberately narrow.
    #: Excluded on purpose:
    #:   "connection refused", "timeout"  — what a saturated service's victim reports
    #:   "N of M connections in use"      — pressure arriving from elsewhere, not own exhaustion
    #:   "slow query", "duration: ...ms"  — being busy is not being full
    _SATURATION = ("pool exhausted", "pool saturated", "no worker available", "workers busy",
                   "connection is not available", "remaining connection slots",
                   "queue depth", "thread pool", "threads busy", "too many connections")

    def _is_saturated(self, service: str) -> bool:
        """Does this service report running out of a resource of its own?

        Matched on signal text, because saturation has no metric yet: the telemetry carries cpu,
        disk and process state, nothing for pool/worker/queue depth. When those metrics exist this
        should read them instead — text matching is the stand-in, not the design.

        A service only counts as saturated when it reports the exhaustion from its OWN resource,
        which is why a signal merely quoting a dependency's error ("database error: FATAL:
        remaining connection slots ...") does not: the phrase is there, but it is prefixed by the
        dependency's own error. Without this, in db_connections_full both orders-db (really full)
        and orders-api (quoting it) match, the count is two, and the rule gives up.
        """
        for s in self.signals:
            if s.service != service or s.severity == Severity.info:
                continue
            text = f"{s.title} {s.raw.get('body', '')}".lower()
            if self._relays_someone_else(text, service):
                continue
            if any(m in text for m in self._SATURATION):
                return True
        return False

    def _relays_someone_else(self, text: str, service: str) -> bool:
        """Is this signal quoting another service's failure rather than reporting its own?

        Two ways it happens, and both would otherwise make the relaying service look saturated:
          "database error: FATAL: remaining connection slots ..."   a prefix naming the failure's origin
          "checkout failed: orders-api 503: no worker available"    another service named before the phrase

        Without this, in pool_exhaustion_db_noisy the storefront matches on orders-api's quoted 503
        and in db_connections_full orders-api matches on the database's quoted error — two services
        look saturated, the count is no longer one, and the rule falls back to the graph.
        """
        if "database error" in text or "upstream" in text:
            return True
        others = [o for o in self.services if o != service and o != UNKNOWN]
        first = min((text.index(m) for m in self._SATURATION if m in text), default=None)
        return first is not None and any(
            0 <= text.find(o.lower()) < first for o in others if o.lower() in text)

    @property
    def all_clear(self) -> bool:
        alerts = [s for s in self.signals if s.signal_type.value == "alert"]
        return bool(alerts) and all(s.signal_id in self.closed for s in alerts)

    def summary(self) -> dict:
        return {"case_id": self.case_id, "services": self.services, "root_service": self.root_service,
                "first_seen": self.first_seen.isoformat(), "last_seen": self.last_seen.isoformat(),
                "signals": [f"{s.severity.value} {s.service}: {s.title}" for s in self.signals],
                "needs_review": self.needs_review}


class Decision(BaseModel):
    action: str                  # duplicate | closing | repeat | merged | new_case | cases_joined
    case_id: str | None
    reason: str
    by: str = "rules"            # rules | agent


def _gap(sig: IncidentSignal, case: Case) -> timedelta:
    """Distance from the signal to the case's time span (0 if inside it)."""
    if case.first_seen <= sig.timestamp <= case.last_seen:
        return timedelta(0)
    return min(abs(sig.timestamp - case.first_seen), abs(sig.timestamp - case.last_seen))


LOCAL_WORDS = ("cpu", "memory", "disk", "utilization", "saturat", "load average", "swap")


def is_local(sig: IncidentSignal) -> bool:
    """A resource-saturation symptom (CPU, memory, disk) describes the machine it's on. Unlike downtime, errors
    or latency, it does not travel downstream, so a dependency alone doesn't explain it."""
    text = f"{sig.metric or ''} {sig.title}".lower()
    return any(w in text for w in LOCAL_WORDS)


def topology_reason(service: str, case: Case) -> tuple[str, str] | None:
    """(reason, direction of the signal relative to the case: same | upstream | downstream), or None."""
    if service == UNKNOWN:
        return None
    if service in case.services:
        return f"same service ({service})", "same"
    for other in case.services:
        if path := dependency_path(other, service):      # the case's service needs the signal's: signal is upstream
            return f"{' -> '.join(path)} (depends on)", "upstream"
    for other in case.services:
        if path := dependency_path(service, other):
            return f"{' -> '.join(path)} (depends on)", "downstream"
    return None


class Correlator:
    def __init__(self, agent: "CorrelationAgent | None" = None, clock: Callable[[], datetime] = utcnow,
                 window: timedelta = WINDOW, late_window: timedelta = LATE_WINDOW):
        self.agent, self.clock, self.window, self.late_window = agent, clock, window, late_window
        self.cases: dict[str, Case] = {}
        self._where: dict[str, str] = {}        # signal_id -> case_id
        self._conditions: dict[str, str] = {}   # condition_key -> case_id
        self.log: list[Decision] = []
        self.context: list[IncidentSignal] = []   # info signals that matched no case (yet)

    # ---- tools (also exposed to the agent) ----------------------------------------------------------
    def case_of(self, signal_id: str) -> Case | None:
        """The case a signal is in now (follows joins); None for context-only signals."""
        cid = self._where.get(signal_id)
        return self.cases[cid] if cid else None

    def adopt(self, case: Case):
        """Take over a case from memory (after a restart): its signals and conditions point at it again."""
        self.cases[case.case_id] = case
        for sig in case.signals:
            self._where[sig.signal_id] = case.case_id
            self._conditions[condition_key(sig)] = case.case_id

    def open_cases(self) -> list[Case]:
        return [c for c in self.cases.values() if c.state == "open"]

    def resolve(self, case_id: str, at: datetime):
        """The incident is over (fixed, or nothing to do). A recurrence after `at` opens a new incident; signals that
        arrive later but describe the time before `at` still belong here."""
        case = self.cases[case_id]
        case.state, case.resolved_at = "resolved", at

    def open_case(self, sig: IncidentSignal, reason: str, needs_review: bool = False) -> Case:
        now = self.clock()
        case = Case(case_id=f"case-{uuid.uuid4().hex[:8]}", opened_at=now, last_placed_at=now, needs_review=needs_review)
        self.cases[case.case_id] = case
        self._add(case, sig, reason)
        if sig.severity != Severity.info:          # e.g. the VM start just before its errors
            for ctx in [c for c in self.context if abs(c.timestamp - sig.timestamp) <= self.window]:
                if rel := topology_reason(ctx.service, case):
                    self.context.remove(ctx)
                    self._add(case, ctx, f"context: {rel[0]}")
        return case

    def merge_into_case(self, case_id: str, sig: IncidentSignal, reason: str) -> Case:
        case = self.cases[case_id]
        if case.state != "open":
            raise ValueError(f"{case_id} is not open")
        self._add(case, sig, reason)
        return case

    def join_cases(self, target: Case, others: list[Case], reason: str):
        for o in others:
            for s in o.signals:
                self._where[s.signal_id] = target.case_id
            for k, cid in self._conditions.items():
                if cid == o.case_id:
                    self._conditions[k] = target.case_id
            target.signals += o.signals
            target.rationale += o.rationale + [f"joined {o.case_id}: {reason}"]
            target.closed += o.closed
            for k, n in o.repeats.items():
                target.repeats[k] = target.repeats.get(k, 0) + n
            target.opened_at = min(target.opened_at, o.opened_at)
            target.needs_review = target.needs_review or o.needs_review
            o.state = "merged"
        target.last_placed_at = self.clock()

    def _add(self, case: Case, sig: IncidentSignal, reason: str):
        case.signals.append(sig)
        case.rationale.append(f"{sig.signal_id}: {reason}")
        case.last_placed_at = self.clock()
        self._where[sig.signal_id] = case.case_id
        self._conditions[condition_key(sig)] = case.case_id

    # ---- the decision -----------------------------------------------------------------------------------
    def ingest(self, sig: IncidentSignal) -> Decision:
        d = self._decide(sig)
        self.log.append(d)
        return d

    def _decide(self, sig: IncidentSignal) -> Decision:
        if sig.signal_id in self._where:
            if self._where[sig.signal_id] is None:
                return Decision(action="duplicate", case_id=None, reason="signal id already seen (context)")
            return Decision(action="duplicate", case_id=self._where[sig.signal_id], reason="signal id already seen")

        if sig.state == "closed":
            open_id = sig.signal_id.rsplit(":", 1)[0] + ":open"
            if open_id in self._where:
                case = self.cases[self._where[open_id]]
                case.closed.append(open_id)
                self._where[sig.signal_id] = case.case_id
                case.rationale.append(f"{open_id}: alert closed")
                return Decision(action="closing", case_id=case.case_id, reason=f"closes {open_id}")
            # a close for something we never saw open: nothing to act on, but keep it visible
            case = self.open_case(sig, "closing signal without a matching open alert", needs_review=True)
            return Decision(action="new_case", case_id=case.case_id, reason="orphan closing signal")

        key = condition_key(sig)
        if key in self._conditions:
            case = self.cases[self._conditions[key]]
            if case.state == "resolved" and case.resolved_at and sig.timestamp <= case.resolved_at:
                case.repeats[key] = case.repeats.get(key, 0) + 1
                self._where[sig.signal_id] = case.case_id
                return Decision(action="late", case_id=case.case_id, reason="repeat from before the incident was resolved")
            if case.state == "open" and _gap(sig, case) <= self.window:
                case.repeats[key] = case.repeats.get(key, 0) + 1
                self._where[sig.signal_id] = case.case_id
                return Decision(action="repeat", case_id=case.case_id, reason="same condition already in the case")

        for case in self.cases.values():        # late signals describing the time before a resolved incident ended
            if case.state == "resolved" and case.resolved_at and sig.timestamp <= case.resolved_at \
                    and topology_reason(sig.service, case):
                self._add(case, sig, "arrived after the incident was resolved, but describes the time before")
                return Decision(action="late", case_id=case.case_id, reason="belongs to the already-resolved incident")

        strong, late, local = [], [], []
        for case in self.open_cases():
            rel = topology_reason(sig.service, case)
            if not rel:
                continue
            why, direction = rel
            gap = _gap(sig, case)
            mins = int(gap.total_seconds() // 60)
            if direction == "downstream" and is_local(sig):
                if gap <= self.window:
                    local.append((case, f"{why}, but {sig.title!r} is local to {sig.service}"))
            elif gap <= self.window:
                strong.append((case, f"{why}, {mins} min apart"))
            elif gap <= self.late_window:
                late.append((case, f"{why}, but {mins} min apart"))

        if len(strong) == 1:
            case, why = strong[0]
            self.merge_into_case(case.case_id, sig, why)
            return Decision(action="merged", case_id=case.case_id, reason=why)
        if len(strong) > 1:
            target, others = strong[0][0], [c for c, _ in strong[1:]]
            why = "; ".join(w for _, w in strong)
            self.merge_into_case(target.case_id, sig, strong[0][1])
            self.join_cases(target, others, f"{sig.service} links them: {why}")
            return Decision(action="cases_joined", case_id=target.case_id, reason=why)

        if sig.severity == Severity.info:
            self.context.append(sig)
            self._where[sig.signal_id] = None
            return Decision(action="context", case_id=None, reason="info event, no related incident: kept as context")

        uncertain = sig.service == UNKNOWN or bool(late)
        if (uncertain or local) and self.agent:
            return self.agent.decide(self, sig, hints=[w for _, w in late + local])
        if uncertain:
            case = self.open_case(sig, "uncertain: " + ("unknown service" if sig.service == UNKNOWN
                                                        else "; ".join(w for _, w in late)), needs_review=True)
            return Decision(action="new_case", case_id=case.case_id, reason="uncertain, kept separate for review")
        if local:
            why = "; ".join(w for _, w in local)
            case = self.open_case(sig, f"local symptom, a dependency alone doesn't explain it: {why}")
            return Decision(action="new_case", case_id=case.case_id, reason="local symptom on a downstream service")
        case = self.open_case(sig, "no related open case")
        return Decision(action="new_case", case_id=case.case_id, reason="no related open case")

    # ---- metrics ----------------------------------------------------------------------------------------
    def time_to_correlate(self) -> dict[str, float]:
        """Per open case: seconds from its first signal being received to its latest signal being placed."""
        return {c.case_id: (c.last_placed_at - c.opened_at).total_seconds() for c in self.open_cases()}


# ---- the agent, for uncertain signals only ---------------------------------------------------------------

AGENT_PROMPT = """You correlate monitoring signals into incidents for Acme Shop. A new signal could not be placed
by the rules. Decide whether it belongs to one of the open cases or is a separate incident.
Use the tools to look at the open cases, the service dependency graph and external status pages.
Only merge when the evidence (shared service, dependency, timing, matching resource or symptom) supports it;
if unsure, open a new case. Finish by calling exactly one of merge_into_case or open_new_case, with a short reason."""


class CorrelationAgent:
    """Tool-using LLM for the uncertain signals. The LLM chooses; the tools enforce what is allowed."""

    def __init__(self, llm, status_lookup: Callable[[str], list[str]] | None = None, max_steps: int = 6):
        self.llm, self.status_lookup, self.max_steps = llm, status_lookup or (lambda p: []), max_steps

    def decide(self, corr: Correlator, sig: IncidentSignal, hints: list[str]) -> Decision:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        from langchain_core.tools import tool

        outcome: dict = {}

        @tool
        def list_open_cases() -> str:
            """The open incident cases: services, time span and signal titles."""
            return json.dumps([c.summary() for c in corr.open_cases()])

        @tool
        def dependency_lookup(service_a: str, service_b: str) -> str:
            """Whether one service depends on the other in Acme Shop's dependency graph."""
            p = dependency_path(service_a, service_b) or dependency_path(service_b, service_a)
            return " -> ".join(p) + " (depends on)" if p else "no dependency between them"

        @tool
        def status_feed_lookup(provider: str) -> str:
            """Current non-operational components on an external provider's status page."""
            if provider not in services() or not services()[provider].external:
                return f"{provider} is not a known external provider"
            return json.dumps(self.status_lookup(provider)) or "[]"

        @tool
        def merge_into_case(case_id: str, reason: str) -> str:
            """Put the new signal into an existing open case."""
            if case_id not in corr.cases or corr.cases[case_id].state != "open":
                return f"refused: {case_id} is not an open case"
            outcome.update(action="merged", case_id=case_id, reason=reason)
            return "ok"

        @tool
        def open_new_case(reason: str) -> str:
            """Treat the new signal as a separate incident."""
            outcome.update(action="new_case", case_id=None, reason=reason)
            return "ok"

        tools = {t.name: t for t in (list_open_cases, dependency_lookup, status_feed_lookup, merge_into_case, open_new_case)}
        llm = self.llm.bind_tools(list(tools.values()))
        signal = sig.model_dump(mode="json", exclude={"raw"})
        msgs = [SystemMessage(AGENT_PROMPT),
                HumanMessage(f"New signal:\n{json.dumps(signal)}\nRule hints: {hints or 'none'}")]
        for _ in range(self.max_steps):
            try:
                ai = resilience.call("correlation agent", lambda: llm.invoke(msgs))
            except Exception as e:          # after retries (or out of budget): no merge, a person reviews it
                outcome.clear()
                case = corr.open_case(sig, f"agent failed ({type(e).__name__}): kept separate for review", needs_review=True)
                return Decision(action="new_case", case_id=case.case_id, reason=f"correlation agent failed: {e}", by="agent")
            msgs.append(ai)
            if not getattr(ai, "tool_calls", None):
                break
            for call in ai.tool_calls:
                result = tools[call["name"]].invoke(call["args"]) if call["name"] in tools else "unknown tool"
                msgs.append(ToolMessage(result, tool_call_id=call["id"]))
            if outcome:
                break

        if outcome.get("action") == "merged":
            corr.merge_into_case(outcome["case_id"], sig, f"agent: {outcome['reason']}")
            return Decision(action="merged", case_id=outcome["case_id"], reason=outcome["reason"], by="agent")
        reason = outcome.get("reason") or "agent reached no decision"
        case = corr.open_case(sig, f"agent: {reason}", needs_review=not outcome)
        return Decision(action="new_case", case_id=case.case_id, reason=reason, by="agent")
