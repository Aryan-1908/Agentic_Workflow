"""LLM usage: every Gemini call is counted, per role and per day, against a daily budget (cost rule, 29 Sep:
"use Gemini mainly when something different is reported"). Over budget, a call raises BudgetExceeded; the workflow
turns that into an escalation with the rule-based information instead of spending more.

    COPILOT_LLM_DAILY_BUDGET=200      generation calls per day (default 200); embeddings are counted but not budgeted
Counts are kept in runs/llm_usage.json: {"2026-09-29": {"diagnosis": 3, "judge": 7, "embeddings": 12}}."""
import json, os, pathlib
from datetime import date

from langchain_core.callbacks import BaseCallbackHandler

PATH = pathlib.Path("runs") / "llm_usage.json"
UNBUDGETED = {"embeddings"}


class BudgetExceeded(RuntimeError):
    pass


def budget() -> int:
    return int(os.getenv("COPILOT_LLM_DAILY_BUDGET", "200"))


def _load(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def today(path: pathlib.Path | None = None) -> dict[str, int]:
    return _load(path or PATH).get(date.today().isoformat(), {})


def spent(path: pathlib.Path | None = None) -> int:
    return sum(n for role, n in today(path).items() if role not in UNBUDGETED)


def add(role: str, n: int = 1, path: pathlib.Path | None = None):
    """Count n calls for a role; raises BudgetExceeded (without counting) if a budgeted call would go over."""
    path = path or PATH          # looked up at call time, so it can be redirected (tests)
    if role not in UNBUDGETED and spent(path) + n > budget():
        raise BudgetExceeded(f"daily LLM budget of {budget()} calls used up (COPILOT_LLM_DAILY_BUDGET)")
    data = _load(path)
    day = data.setdefault(date.today().isoformat(), {})
    day[role] = day.get(role, 0) + n
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


class CountingCallback(BaseCallbackHandler):
    """Attached to every chat model get_llm() creates: counts each call before it's made."""
    raise_error = True          # let BudgetExceeded stop the call instead of being swallowed

    def __init__(self, role: str):
        self.role = role

    def on_chat_model_start(self, serialized, messages, **kwargs):
        add(self.role)

    def on_llm_start(self, serialized, prompts, **kwargs):
        add(self.role)


class CountedEmbeddings:
    """Wraps an embeddings model so its calls are counted too (not budgeted: they cost far less)."""

    def __init__(self, inner):
        self.inner = inner

    def embed_query(self, text):
        add("embeddings")
        return self.inner.embed_query(text)

    def embed_documents(self, texts):
        add("embeddings", len(texts))
        return self.inner.embed_documents(texts)
