"""Per-incident trace (spec M7: "logging for every step the system takes"). One JSON line per step in
runs/trace/<case_id>.jsonl; the case id is the trace id. Steps come from correlation, every workflow node, the agents'
sub-steps (search, draft, judge, safety, send, verify), retries and failures.

    {"at": "...", "case_id": "case-…", "step": "investigate", "status": "ok|retry|error|blocked|info",
     "detail": "...", "ms": 1234, "attempt": 2}

The current case is held in a context variable, so agents deep inside a workflow step can trace without being told
which case they're working on."""
import contextvars, json, pathlib, time
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = pathlib.Path("runs") / "trace"
current_case: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_case", default=None)


def path_for(case_id: str) -> pathlib.Path:
    return ROOT / f"{case_id}.jsonl"


def event(step: str, status: str = "info", detail: str = "", case_id: str | None = None, **fields):
    """Append one trace line for the given (or current) case. Without a case, nothing is written."""
    cid = case_id or current_case.get()
    if not cid:
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "case_id": cid, "step": step,
           "status": status, "detail": str(detail)[:500], **fields}
    with open(path_for(cid), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


@contextmanager
def for_case(case_id: str):
    token = current_case.set(case_id)
    try:
        yield
    finally:
        current_case.reset(token)


@contextmanager
def timed(step: str, detail: str = ""):
    """Trace a step with its duration; an exception is traced as an error and re-raised."""
    t0 = time.monotonic()
    try:
        yield
    except BaseException as e:
        event(step, "error", f"{type(e).__name__}: {e}", ms=round((time.monotonic() - t0) * 1000))
        raise
    else:
        if detail:
            event(step, "ok", detail, ms=round((time.monotonic() - t0) * 1000))


def read(case_id: str) -> list[dict]:
    p = path_for(case_id)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
