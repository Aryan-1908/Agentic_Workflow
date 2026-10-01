"""Where approved actions are sent: the environment's control API. For the POC that's the simulator: requests go into
runs/sim/actions.jsonl, the simulator (running in --live mode) applies them on its next minute, and confirms through
telemetry with a `remediation.applied` event carrying the request id, like a real cloud's audit log. A real cloud
later = another class with the same apply()."""
import json, pathlib, uuid
from datetime import datetime, timezone

INBOX = pathlib.Path("runs") / "sim" / "actions.jsonl"


class SimCloud:
    def __init__(self, inbox: str | pathlib.Path = INBOX):
        self.inbox = pathlib.Path(inbox)

    def apply(self, action: str, params: dict, actor: str = "copilot-executor") -> str:
        """Send one allowlisted action; returns the request id to look for in the telemetry."""
        request_id = uuid.uuid4().hex[:12]
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with open(self.inbox, "a") as f:
            f.write(json.dumps({"id": request_id, "action": action, "params": params, "actor": actor,
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
        return request_id
