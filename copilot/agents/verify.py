"""Verification (spec: "verifies post-action signal state"): after an action is sent, read the telemetry and decide
whether the incident actually recovered. Evidence, in order:
  1. the environment confirmed the action: a `remediation.applied` event with our request id (and result ok)
  2. the case's open alerts close
  3. no new errors from the case's services for a while
Outcomes: resolved | failed | not_applied (the environment never confirmed) | ambiguous (partly recovered)."""
import time
from dataclasses import dataclass, field

from ..signals import IncidentSignal, Severity, SignalType


@dataclass
class Verification:
    outcome: str
    detail: str
    applied: bool = False
    closed_alerts: list = field(default_factory=list)
    open_alerts: list = field(default_factory=list)
    errors_after: int = 0


class Verifier:
    def __init__(self, timeout: float = 60, poll: float = 2, quiet_polls: int = 3, wait=None, max_polls: int | None = None):
        """quiet_polls: consecutive polls without new errors needed to call it resolved.
        wait(): what happens between polls (default: sleep `poll` seconds; tests advance the simulator instead)."""
        self.timeout, self.poll, self.quiet_polls = timeout, poll, quiet_polls
        self.wait = wait or (lambda: time.sleep(self.poll))
        self.max_polls = max_polls or 10_000

    def verify(self, reader, request_id: str, case) -> Verification:
        open_alerts = {s.signal_id.rsplit(":", 1)[0] for s in case.signals
                       if s.signal_type == SignalType.alert and s.state == "open" and s.signal_id not in case.closed}
        services = set(case.services)
        applied, closed, quiet, errors_after, errors_before_fix = False, set(), 0, 0, 0
        deadline = time.monotonic() + self.timeout
        polls = 0
        while True:
            new = [s for s in reader.poll() if isinstance(s, IncidentSignal)]
            polls += 1
            for s in new:
                a = s.raw.get("attributes", {}) if isinstance(s.raw, dict) else {}
                if s.metric == "event/remediation.applied" and a.get("request.id") == request_id:
                    if a.get("result") != "ok":
                        return Verification("failed", f"the environment refused it: {a.get('result')}")
                    applied = True
            if applied:
                errs = [s for s in new if s.service in services and s.severity in (Severity.error, Severity.critical)
                        and s.signal_type != SignalType.alert]
                closed |= {s.signal_id.rsplit(":", 1)[0] for s in new if s.state == "closed"} & open_alerts
                errors_after += len(errs)
                quiet = 0 if errs else quiet + 1
                if closed == open_alerts and quiet >= self.quiet_polls:
                    return Verification("resolved", f"{len(closed)} alert(s) closed, no new errors for {quiet} checks",
                                        True, sorted(closed), [], errors_after)
            if time.monotonic() >= deadline or polls >= self.max_polls:
                break
            self.wait()
        still = sorted(open_alerts - closed)
        if not applied:
            return Verification("not_applied", "the environment never confirmed the action (is the simulator running live?)")
        if closed or quiet > 0:
            return Verification("ambiguous", f"partly recovered: {len(closed)}/{len(open_alerts)} alert(s) closed, "
                                f"{errors_after} error(s) since the action", True, sorted(closed), still, errors_after)
        return Verification("failed", f"no recovery: {len(still)} alert(s) still open, {errors_after} error(s) since the action",
                            True, [], still, errors_after)
