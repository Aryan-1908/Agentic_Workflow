"""Intake paths besides OTLP: the anomaly payload, and free text through the grounded LLM extraction."""
from datetime import datetime, timedelta, timezone

from copilot.intake import _Extracted, intake
from copilot.signals import IncidentSignal, Rejected, Severity, SignalType


def test_anomaly_payload():
    [s] = intake({"id": "db-01:cpu:20260115T0802", "metric": "system.cpu.utilization",
                  "resource": {"type": "host", "name": "db-01"}, "observed": 0.97, "expected": 0.41,
                  "zscore": 7.3, "detected_at": "2026-01-15T08:02:00Z"}, "anomaly")
    assert (s.service, s.signal_type, s.severity) == ("orders-db", SignalType.anomaly, Severity.critical)


def test_malformed_anomaly_is_rejected_with_a_reason():
    [r] = intake({"id": "x", "metric": "m", "zscore": 5}, "anomaly")
    assert isinstance(r, Rejected) and "detected_at" in r.reason


def test_unknown_format_without_llm_is_rejected_not_dropped():
    [r] = intake("disk almost full on something", "email")
    assert isinstance(r, Rejected) and "no LLM" in r.reason


class _FakeLLM:
    def __init__(self, extracted): self.extracted = extracted
    def with_structured_output(self, schema): return self
    def invoke(self, prompt): return self.extracted


def test_free_text_via_llm_is_validated_and_grounded():
    text = "PagerDuty: orders-api p99 latency above 2s on orders-02 since 2026-01-15T07:55:00Z"
    llm = _FakeLLM(_Extracted(service="orders-api", severity=Severity.error, started_at="2026-01-15T07:55:00Z",
                              title="orders-api latency high", resource_name="orders-02"))
    [s] = intake(text, "pagerduty", llm=llm)
    assert isinstance(s, IncidentSignal)
    assert (s.service, s.resource, s.raw["timestamp_from"]) == ("orders-api", {"name": "orders-02"}, "text")


def test_free_text_llm_cannot_invent_a_service_or_resource():
    llm = _FakeLLM(_Extracted(service="payments-provider", severity=Severity.warning, started_at=None,
                              title="slowness", resource_name="web-07"))
    [s] = intake("Something is slow, users are complaining", "email", llm=llm)
    assert s.service == "unknown" and s.resource == {}      # neither appears in the text
    assert s.raw["timestamp_from"] == "received_at"
    assert datetime.now(timezone.utc) - s.timestamp < timedelta(seconds=5)
