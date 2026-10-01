"""The copilot's main loop, one pass at a time (used by `copilot watch`, the tests, and the M8 evaluation):

    read new OTel signals -> correlate -> remember -> once an incident has settled, run its workflow
    -> when an incident is over (fixed, or nothing to do) close it, so a recurrence is a new incident

Incidents handed to people (escalated, rejected, waiting for approval) stay open: their new signals join them and no
new workflow starts, so a still-broken service doesn't trigger a workflow per signal."""
from datetime import datetime, timedelta, timezone

from . import trace
from .signals import IncidentSignal
from .workflow import SETTLE, settled

OVER = ("closed: resolved", "closed: no action needed", "closed: recovered")     # statuses that end an incident
RECURRENCE_WINDOW = timedelta(minutes=30)                    # back within this after a fix = the fix didn't hold


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(self, reader, memory, corr=None, flow=None, out=print, settle=SETTLE, record=None,
                 seen_before=None, clock=_now):
        self.reader, self.memory, self.corr, self.flow = reader, memory, corr, flow
        self.out, self.settle, self.record, self.seen_before, self.clock = out, settle, record, seen_before, clock
        self.seen: set[str] = set()
        self.told: set[str] = set()                 # cases already announced as incidents
        self.waiting: dict[str, bool] = {}          # first signal of each incident waiting to settle
        self.open_flows: dict[str, str] = {}        # stored case id -> a signal id of the live case (to follow joins)
        self.status_seen: dict[str, list[str]] = {}

    def step(self, final: bool = False) -> list[dict]:
        """One pass. final=True: don't wait for incidents to settle (all data has been read). Returns the workflow
        states that finished in this pass."""
        finished = []
        try:
            items = self.reader.poll()
        except Exception as e:                      # a flaky source must not stop the copilot
            self.out(f"  source failed: {e}")
            items = []
        for item in items:
            key = getattr(item, "signal_id", None)
            if key in self.seen:
                continue
            self.seen.add(key)
            if self.record:
                self.record(item)
            self.out(_show(item))
            if not isinstance(item, IncidentSignal):
                continue
            if item.signal_type.value == "status":
                self.status_seen.setdefault(item.service, []).append(item.title)
            if self.corr is None:
                continue
            d = self.corr.ingest(item)
            case = self.corr.case_of(item.signal_id)
            cid = self.memory.save_case(case) if case else None
            where = (f"{cid} [{', '.join(case.services)}] root={case.root_service or '?'}" if case else "(no incident)")
            self.out(f"          -> {d.action} {where} ({d.by}: {d.reason})")
            if cid and d.action not in ("repeat", "duplicate"):
                trace.event("correlate", "info", f"{d.action}: {item.title[:120]} ({d.reason})", case_id=cid)
            if case and case.state == "open" and cid not in self.told and \
                    any(x.severity.value != "info" for x in case.signals):
                self.told.add(cid)
                if self.seen_before:
                    self.out(self.seen_before(self.memory, case))
                if self.flow:
                    self.waiting[item.signal_id] = True
                    self.out(f"          workflow: waiting for the incident to settle "
                             f"(no new signal for {int(self.settle.total_seconds())} s) before diagnosing")
        for sig_id in list(self.waiting):
            case = self.corr.case_of(sig_id)
            if case is None or case.state != "open":
                self.waiting.pop(sig_id)
            elif final or settled(case, self.corr.clock(), quiet=self.settle):
                self.waiting.pop(sig_id)
                cid = self.memory.save_case(case)
                self.out(f"\n  [{cid}] settled with {len(case.signals)} signal(s) on {', '.join(case.services)}")
                self.open_flows[cid] = sig_id
                self._note_recurrence(cid, case)
                finished += self._run(cid, case)
        finished += self._check_decided()
        return finished

    def _note_recurrence(self, cid, case):
        """A resource we fixed recently is in trouble again: that fix didn't hold. Record it as failed, so confidence
        in it drops (the next attempt goes to a person) and the next diagnosis is fresh, not reused (seen live 1 Oct:
        in `flapping`, confidence rose to 1.0 while the problem kept returning). Matched on the fixed resource, not on
        the signals: the M8 harness showed the first occurrence seen by the anomaly detector and the recurrence by
        the alert, with nothing in common."""
        now = datetime.now(timezone.utc)
        here = {s.resource.get("name") for s in case.signals if s.severity.value != "info"} | {case.root_service}
        for fix in self.memory.recent_fixes(now - RECURRENCE_WINDOW):
            if fix["case_id"] == cid or fix["target"] not in here:
                continue
            if not fix["failed"]:
                mins = int((now - datetime.fromisoformat(fix["at"])).total_seconds() // 60)
                self.memory.record(fix["case_id"], "outcome", {
                    "action": fix["action"], "result": "failed", "note": f"came back {mins} min after the fix (as {cid})"})
                trace.event("lifecycle", "failed", f"recurrence of {fix['case_id']} {mins} min after its fix: "
                            "that fix is now recorded as not holding", case_id=cid)
                self.out(f"          memory: {fix['target']} is in trouble again {mins} min after {fix['case_id']} "
                         f"was fixed by {fix['action']}: that fix is recorded as not holding")
            return

    def _run(self, cid, case) -> list[dict]:
        try:
            st = self.flow.start(cid, case)
        except Exception as e:
            self.out(f"          workflow failed: {type(e).__name__}: {str(e)[:150]}")
            return []
        for t in st.get("trace", []):
            if t["step"] != "start":
                self.out(f"          {t['step']}: {t['detail']}")
        if st.get("waiting"):
            self.out(f"          -> AWAITING APPROVAL: python -m copilot approve {cid} --by <name>   (or reject … --reason)")
            return []
        return self._maybe_close(cid, st)

    def _check_decided(self) -> list[dict]:
        """Approvals happen in another process (`copilot approve`): notice when those workflows finish."""
        out = []
        for cid in list(self.open_flows):
            st = self.flow.state(cid)
            if not st.get("waiting") and st.get("status", "").startswith(("closed", "escalated")):
                out += self._maybe_close(cid, st)
        return out

    def _maybe_close(self, cid, st) -> list[dict]:
        sig_id = self.open_flows.pop(cid, None)
        status = st.get("status", "")
        if status.startswith(OVER) and sig_id:
            live = self.corr.case_of(sig_id)
            if live is not None and live.state == "open":
                at = self.clock()
                self.corr.resolve(live.case_id, at)
                self.memory.save_case(live)
                self.memory.mark_closed(cid)
                trace.event("lifecycle", "ok", f"incident closed ({status}); a recurrence opens a new incident", case_id=cid)
        return [{"case_id": cid, **st}]


def _show(item) -> str:
    if isinstance(item, IncidentSignal):
        return (f"{item.timestamp:%H:%M:%S}  {item.severity.value:<8} {item.signal_type.value:<7} "
                f"{item.service:<18} {item.state:<6} {item.title}")
    return f"REJECTED ({item.source_hint}): {item.reason}"
