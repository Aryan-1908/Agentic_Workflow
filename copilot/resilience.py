"""Retries with backoff for every external call (spec M7: "make failed calls retry a couple of times before giving up
cleanly"): Gemini, embeddings, the environment's control API. Each retry and the final failure go into the case's
trace; after the last attempt the error is raised to the caller, which turns it into an escalation (never a hang, never
a silent drop). A used-up LLM budget is not retried.

Fault injection, for demos and tests:  COPILOT_FAULTS="llm=2,cloud=1" makes the next 2 LLM calls and the next cloud
call fail on purpose (kinds: llm, embeddings, cloud)."""
import os, time

from . import trace
from .usage import BudgetExceeded

ATTEMPTS = 3
BACKOFF = float(os.getenv("COPILOT_RETRY_BACKOFF", "1.0"))    # seconds before the 2nd attempt; doubles each time
_injected: dict[str, int] | None = None


class InjectedFault(RuntimeError):
    pass


def _faults() -> dict[str, int]:
    global _injected
    if _injected is None:
        _injected = {}
        for part in filter(None, os.getenv("COPILOT_FAULTS", "").split(",")):
            kind, _, n = part.partition("=")
            _injected[kind.strip()] = int(n or 1)
    return _injected


def reset_faults(spec: str | None = None):
    """Re-read COPILOT_FAULTS (or use `spec`); used by tests."""
    global _injected
    _injected = None
    if spec is not None:
        os.environ["COPILOT_FAULTS"] = spec


def call(what: str, fn, kind: str = "llm", attempts: int = ATTEMPTS, sleep=time.sleep):
    """fn() with retries. `what` names the call in the trace (e.g. "diagnosis draft")."""
    for attempt in range(1, attempts + 1):
        try:
            faults = _faults()
            if faults.get(kind, 0) > 0:
                faults[kind] -= 1
                raise InjectedFault(f"injected {kind} failure (COPILOT_FAULTS)")
            result = fn()
            if attempt > 1:
                trace.event(what, "ok", f"succeeded on attempt {attempt}/{attempts}", attempt=attempt)
            return result
        except BudgetExceeded:
            raise
        except Exception as e:
            last = attempt == attempts
            trace.event(what, "error" if last else "retry", f"attempt {attempt}/{attempts}: {type(e).__name__}: {e}",
                        attempt=attempt)
            if last:
                raise
            sleep(BACKOFF * 2 ** (attempt - 1))
