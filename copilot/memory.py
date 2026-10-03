"""Persistent case memory (spec M3): "per-incident and per-service history (past correlated incidents, past
remediations and their outcomes) so recurring issues get faster, more confident recommendations over time."

SQLite, one file (default runs/memory.sqlite). Holds:
  cases        every correlated case, as the full Case (signals, rationale, repeats), with its services and signature
  case_events  what happened to a case: recommendation, approval, execution, outcome, note (filled from M5/M6 on)

A case is identified across restarts by its signals: saving a case whose signals are already stored updates that
stored case instead of adding a new one, so replaying the same history (watch --since) never duplicates memory."""
import json, pathlib, sqlite3
from datetime import datetime, timedelta

from .correlation import LATE_WINDOW, Case
from .signals import Severity

DEFAULT_PATH = pathlib.Path("runs") / "memory.sqlite"
LATE = LATE_WINDOW      # how long after its last signal an open case can still grow
EVENT_KINDS = ("diagnosis", "recommendation", "approval", "execution", "outcome", "note")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, opened_at TEXT, first_seen TEXT, last_seen TEXT,
  services TEXT, root_service TEXT, signature TEXT, all_clear INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS case_signals (signal_id TEXT PRIMARY KEY, case_id TEXT);
CREATE TABLE IF NOT EXISTS case_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, kind TEXT, at TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_case ON case_events(case_id);
"""


def signature(case: Case) -> list[str]:
    """What kind of incident this is, independent of when it happened: service + source + what was measured.
    Info-level context (a VM start) is left out, so it doesn't make unrelated cases look alike."""
    return sorted({f"{s.service}|{s.source}|{s.metric or s.title}" for s in case.signals if s.severity != Severity.info})


def similarity(a: list[str], b: list[str]) -> float:
    """Jaccard overlap of two signatures, 0..1."""
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


class Memory:
    def __init__(self, path: str | pathlib.Path = DEFAULT_PATH):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- writing ----------------------------------------------------------------------------------------
    def save_case(self, case: Case) -> str:
        """Store or update a case. Returns the stored case id (the earlier id if these signals were seen before)."""
        ids = [s.signal_id for s in case.signals]
        known = {r["case_id"] for r in self.db.execute(
            f"SELECT case_id FROM case_signals WHERE signal_id IN ({','.join('?' * len(ids))})", ids)}
        stored_id = sorted(known)[0] if known else case.case_id     # stable across restarts and replays
        data = case.model_copy(update={"case_id": stored_id})
        with self.db:
            for other in known - {stored_id}:                         # the live case joined two stored ones
                self.db.execute("UPDATE case_events SET case_id=? WHERE case_id=?", (stored_id, other))
                self.db.execute("DELETE FROM cases WHERE case_id=?", (other,))
            self.db.execute(
                "INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?)",
                (stored_id, case.opened_at.isoformat(), case.first_seen.isoformat(), case.last_seen.isoformat(),
                 json.dumps(case.services), case.root_service, json.dumps(signature(case)),
                 int(case.all_clear or case.state == "resolved"),
                 data.model_dump_json()))
            self.db.executemany("INSERT OR REPLACE INTO case_signals VALUES (?,?)", [(i, stored_id) for i in ids])
        return stored_id

    def record(self, case_id: str, kind: str, data: dict, at: datetime | None = None):
        """Something that happened to a case, e.g. record(cid, "outcome", {"action": "vm.start", "result": "resolved"})."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {EVENT_KINDS}")
        with self.db:
            self.db.execute("INSERT INTO case_events (case_id, kind, at, data) VALUES (?,?,?,?)",
                            (case_id, kind, (at or datetime.now().astimezone()).isoformat(), json.dumps(data)))

    # ---- reading ----------------------------------------------------------------------------------------
    def get(self, case_id: str) -> Case | None:
        row = self.db.execute("SELECT data FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return Case.model_validate_json(row["data"]) if row else None

    def events(self, case_id: str) -> list[dict]:
        return [{"kind": r["kind"], "at": r["at"], **json.loads(r["data"])} for r in self.db.execute(
            "SELECT kind, at, data FROM case_events WHERE case_id=? ORDER BY id", (case_id,))]

    def history(self, service: str, limit: int = 20) -> list[dict]:
        """Past cases involving a service, newest first."""
        rows = self.db.execute("SELECT * FROM cases WHERE services LIKE ? ORDER BY first_seen DESC LIMIT ?",
                               (f'%"{service}"%', limit))
        return [self._summary(r) for r in rows]

    def similar_cases(self, case: Case, k: int = 3, min_score: float = 0.5) -> list[dict]:
        """Earlier cases that look like this one (same services and signal signature), best first.
        The case itself (matched by its signals) is excluded, so a replayed case never matches its own record."""
        ids = [s.signal_id for s in case.signals]
        own = {r["case_id"] for r in self.db.execute(
            f"SELECT case_id FROM case_signals WHERE signal_id IN ({','.join('?' * len(ids))})", ids)}
        sig = signature(case)
        scored = []
        for r in self.db.execute("SELECT * FROM cases WHERE first_seen < ?", (case.first_seen.isoformat(),)):
            if r["case_id"] in own:
                continue
            score = similarity(sig, json.loads(r["signature"]))
            if score >= min_score:
                scored.append({**self._summary(r), "score": round(score, 2)})
        scored.sort(key=lambda x: x["first_seen"], reverse=True)     # most recent first among equal scores
        return sorted(scored, key=lambda x: -x["score"])[:k]

    def recent_fixes(self, since: datetime) -> list[dict]:
        """Verified fixes since `since`, newest first: case, action, the resource it was applied to, and whether a
        later recurrence already marked it failed. For recognising "the thing we just fixed broke again"."""
        out = []
        for r in self.db.execute("SELECT case_id, at, data FROM case_events WHERE kind='execution' AND at >= ? "
                                 "ORDER BY id DESC", (since.isoformat(),)):
            d = json.loads(r["data"])
            if d.get("result") != "resolved":
                continue
            results = [json.loads(o["data"]).get("result") for o in self.db.execute(
                "SELECT data FROM case_events WHERE case_id=? AND kind='outcome'", (r["case_id"],))]
            out.append({"case_id": r["case_id"], "at": r["at"], "action": d.get("action"), "target": d.get("target"),
                        "failed": "failed" in results})
        return out

    def outcome_stats(self, action: str, service: str | None = None) -> dict[str, int]:
        """How an action turned out before, e.g. {"resolved": 4, "failed": 1}. Feeds recommendation confidence (M6)."""
        out: dict[str, int] = {}
        for r in self.db.execute("SELECT e.data, c.services FROM case_events e JOIN cases c USING (case_id) "
                                 "WHERE e.kind='outcome'"):
            d = json.loads(r["data"])
            if d.get("action") == action and (service is None or service in json.loads(r["services"])):
                out[d.get("result", "unknown")] = out.get(d.get("result", "unknown"), 0) + 1
        return out

    def rejections(self, action: str, service: str | None = None, limit: int = 3) -> list[dict]:
        """Times a person rejected this action, newest first: [{"by", "reason", "at", "citations"}].

        The decision is on the case's `approval` event and the action on its `recommendation` event,
        so the two are joined here. Until now a rejection was written and never read back:
        outcome_stats() filters kind='outcome', and a rejection is kind='approval'. So confidence
        never moved when a person said no, and the same recommendation came back unchanged.

        Two uses, both deliberate:
          - a count, to lower confidence (routing.py) — deterministic, auditable
          - the reasons, shown to the model so it does not repeat a refused action (investigator)
        The reason is free-form text typed during an incident. It is context for a diagnosis, never
        input to the allowlist, the risk tiers or the safety reviewer.
        """
        out = []
        for r in self.db.execute(
                "SELECT a.case_id, a.at, a.data, c.services FROM case_events a JOIN cases c USING (case_id) "
                "WHERE a.kind='approval' ORDER BY a.id DESC"):
            d = json.loads(r["data"])
            if d.get("decision") != "reject":
                continue
            if service is not None and service not in json.loads(r["services"]):
                continue
            rec = next((e for e in self.events(r["case_id"]) if e["kind"] == "recommendation"), None)
            if rec and rec.get("action") == action:
                out.append({"by": d.get("by"), "reason": d.get("reason"), "at": r["at"],
                            "citations": rec.get("citations", [])})
            if len(out) >= limit:
                break
        return out

    def recent_executions(self, action: str, target: str, since: datetime) -> int:
        """How many times an action was sent to a target since `since` (for the circuit breaker)."""
        n = 0
        for r in self.db.execute("SELECT at, data FROM case_events WHERE kind='execution' AND at >= ?", (since.isoformat(),)):
            d = json.loads(r["data"])
            n += d.get("action") == action and d.get("target") == target and d.get("sent", False)
        return n

    def mark_closed(self, case_id: str):
        """The incident is over: never resumed as open after a restart."""
        with self.db:
            self.db.execute("UPDATE cases SET all_clear=1 WHERE case_id=?", (case_id,))

    def recent_open(self, since: datetime) -> list[Case]:
        """Cases still open and active since `since`: reloaded into the correlator after a restart."""
        rows = self.db.execute("SELECT data FROM cases WHERE all_clear=0 AND last_seen >= ?", (since.isoformat(),))
        return [Case.model_validate_json(r["data"]) for r in rows]

    def _summary(self, r) -> dict:
        outcomes = [e for e in self.events(r["case_id"]) if e["kind"] == "outcome"]
        return {"case_id": r["case_id"], "first_seen": r["first_seen"], "services": json.loads(r["services"]),
                "root_service": r["root_service"], "signature": json.loads(r["signature"]),
                "outcomes": outcomes}


def restore(corr, memory: Memory, window: timedelta):
    """Put recently active open cases back into a fresh Correlator, so an incident that was in progress when the
    copilot stopped keeps growing in the same case instead of splitting in two."""
    for case in memory.recent_open(corr.clock() - window):
        corr.adopt(case)
