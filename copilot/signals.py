"""IncidentSignal: the one shape every alert, anomaly and status-feed event is turned into (spec M1)."""
from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    critical = "critical"
    error = "error"
    warning = "warning"
    info = "info"


class SignalType(str, Enum):
    alert = "alert"          # a monitoring threshold / uptime check fired
    anomaly = "anomaly"      # a metric deviates from its own baseline
    status = "status"        # an external dependency reports degraded health
    event = "event"          # something happened to a resource: VM stopped/crashed (audit log), an error log line


class IncidentSignal(BaseModel):
    # the five fields the spec requires
    source: str = Field(description="where it came from: alert, log, trace, infra_event, anomaly_detector, status:<provider>")
    service: str = Field(description="logical service it concerns, e.g. storefront")
    severity: Severity
    timestamp: datetime = Field(description="when the condition started, UTC")
    signal_type: SignalType
    # what correlation, diagnosis and the audit trail need
    signal_id: str = Field(description="stable id from the source, so repeats can be deduplicated")
    title: str
    state: str = Field(default="open", pattern="^(open|closed)$")
    resource: dict[str, str] = Field(default_factory=dict, description="e.g. {type: gce_instance, name: web-01, zone: ...}")
    metric: str | None = None
    value: float | None = None
    raw: dict = Field(default_factory=dict, description="original payload, kept for the audit trail")

    @field_validator("service", "source", "title", "signal_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v.strip()

    @field_validator("timestamp")
    @classmethod
    def utc_and_not_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        v = v.astimezone(timezone.utc)
        if v > datetime.now(timezone.utc) + timedelta(minutes=5):   # small allowance for clock skew
            raise ValueError("timestamp is in the future")
        return v


class Rejected(BaseModel):
    """A payload intake could not turn into a valid signal. Kept, never silently dropped."""
    source_hint: str
    reason: str
    raw: dict | str
