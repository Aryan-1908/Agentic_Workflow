"""Every test writes its runtime files (LLM usage, traces) to a temporary folder, never into runs/."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_files(tmp_path, monkeypatch):
    from copilot import resilience, trace, usage
    monkeypatch.setattr(usage, "PATH", tmp_path / "llm_usage.json")
    monkeypatch.setattr(trace, "ROOT", tmp_path / "trace")
    monkeypatch.setattr(resilience, "BACKOFF", 0)          # retries without waiting
    monkeypatch.delenv("COPILOT_FAULTS", raising=False)
    resilience.reset_faults()
