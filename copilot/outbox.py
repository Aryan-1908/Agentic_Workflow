"""Tickets and notifications (spec: "update the ticket, notify"; "full audit trail ... attached to the incident").
Local stand-ins for now (decided 25 Sep): a Markdown ticket per case and a notification log. Jira / Slack can
replace them later by implementing the same two methods."""
import json, pathlib
from datetime import datetime, timezone

RUNS = pathlib.Path("runs")


class LocalTickets:
    """runs/tickets/<case_id>.md, rewritten from the workflow state at every step."""

    def __init__(self, root: pathlib.Path = RUNS / "tickets"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def update(self, case_id: str, state: dict) -> pathlib.Path:
        path = self.root / f"{case_id}.md"
        rec, route, d = state.get("recommendation") or {}, state.get("route") or {}, (state.get("diagnosis") or {}).get("diagnosis") or {}
        lines = [f"# Incident {case_id}", "",
                 f"**Status:** {state.get('status', 'open')}  ", f"**Services:** {', '.join(state.get('services', []))}  ",
                 f"**Lane:** {route.get('lane', '-')}: {route.get('reason', '')}  ",
                 f"**Trace:** runs/trace/{case_id}.jsonl (every step, with timings and errors)", "", "## Diagnosis",
                 f"{(state.get('diagnosis') or {}).get('status', '-')}: {d.get('summary', '')}"]
        if d.get("root_cause"):
            lines.append(f"- cause: {d['root_cause']['text']} ({', '.join(d['root_cause']['citations'])})")
        for r in (state.get("diagnosis") or {}).get("removed", []):
            lines.append(f"- removed ({r['where']}): {r['claim']} ({r['problem']})")
        if rec:
            lines += ["", "## Recommendation",
                      f"- action: `{rec['action']}` on {rec.get('target') or '-'} ({rec.get('service') or '-'})",
                      f"- blast radius: {rec['blast_radius']} ({rec['blast_reason']})",
                      f"- confidence: {rec['confidence']}", f"- rollback: {rec['rollback']}",
                      f"- why: {rec['rationale']} ({', '.join(rec['citations'])})"]
        lines += ["", "## Timeline"] + [f"- {e['at']} **{e['step']}**: {e['detail']}" for e in state.get("trace", [])]
        path.write_text("\n".join(lines) + "\n")
        return path


class LocalNotifier:
    """runs/notifications.jsonl: one line per message, with who it's for."""

    def __init__(self, path: pathlib.Path = RUNS / "notifications.jsonl"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, to: str, case_id: str, message: str):
        with open(self.path, "a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "to": to, "case_id": case_id, "message": message}) + "\n")
