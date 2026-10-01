"""Provider-agnostic model access (spec M1). The model is config, not code: any "provider:model" string that
LangChain's init_chat_model understands (google_genai:..., anthropic:..., openai:..., ollama:...).

    COPILOT_MODEL=google_genai:gemini-2.5-flash            default for every role
    COPILOT_MODEL_DIAGNOSIS=google_genai:gemini-2.5-pro    override for one role
"""
import logging, os

from langchain.chat_models import init_chat_model

from .usage import CountingCallback

DEFAULT_MODEL = "google_genai:gemini-2.5-flash"

# The Google SDK logs a notice about "automatic function calling" on every structured-output call; it isn't actionable.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


def model_name(role: str = "default") -> str:
    return os.getenv(f"COPILOT_MODEL_{role.upper()}") or os.getenv("COPILOT_MODEL") or DEFAULT_MODEL


def get_llm(role: str = "default", **kwargs):
    # Bounded: each call fails after `timeout`; resilience.call retries it (3 attempts) and traces every retry.
    kwargs.setdefault("timeout", int(os.getenv("COPILOT_LLM_TIMEOUT", "60")))
    kwargs.setdefault("max_retries", 0)       # retries happen in copilot/resilience.py, visible in the trace
    kwargs.setdefault("callbacks", [CountingCallback(role)])      # counted against the daily budget (copilot/usage.py)
    return init_chat_model(model_name(role), temperature=0, **kwargs)
