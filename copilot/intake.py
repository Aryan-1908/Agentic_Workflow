"""Structured intake (spec M1): OpenTelemetry data -> validated IncidentSignal objects.

The copilot has one input, OpenTelemetry (see otel/README.md for the contract). This module turns single OTLP
items into signals, deterministically (no LLM, no guessing):
  - log records: alerts (event.name=alert), infrastructure events (vm.*, deploy.rollout, firewall.rule_deleted),
    external status pages (event.name=status_page.component), and ERROR+ application logs
  - failed calls seen in traces, aggregated per caller -> callee per minute (by copilot/otel.py)
  - anomalies the detector finds in metrics (copilot/anomaly.py)
Free text (e.g. a forwarded email) can still go through the LLM, validated the same way. Anything that fails
becomes Rejected with a reason, never silently dropped."""
import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError

from .config import service_for_resource, services
from .signals import IncidentSignal, Rejected, Severity, SignalType

UNKNOWN_SERVICE = "unknown"

_SEVERITY_WORDS = {"critical": Severity.critical, "error": Severity.error, "warning": Severity.warning,
                   "info": Severity.info}


def _ts(v) -> datetime:
    """OTLP unix nanoseconds (int or string), epoch seconds, or ISO-8601, to an aware UTC datetime."""
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if isinstance(v, int) and v > 10**14:
        return datetime.fromtimestamp(v / 1e9, tz=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _severity_number(n: int) -> Severity:
    """OTel SeverityNumber: 1-8 trace/debug, 9-12 info, 13-16 warn, 17-20 error, 21-24 fatal."""
    return Severity.critical if n >= 21 else Severity.error if n >= 17 else Severity.warning if n >= 13 else Severity.info


def attributes(kvs: list[dict] | None) -> dict:
    """OTLP [{key, value: {stringValue|intValue|doubleValue|boolValue}}] -> plain dict."""
    out = {}
    for kv in kvs or []:
        v = kv.get("value", {})
        out[kv["key"]] = next((int(x) if k == "intValue" else x for k, x in v.items()), None)
    return out


def _service(res: dict) -> str:
    return res.get("service.name") or service_for_resource(res.get("host.name")) or UNKNOWN_SERVICE


def _resource(res: dict) -> dict[str, str]:
    return {k: str(v) for k, v in {"type": "host", "name": res.get("host.name"),
                                   "cloud": res.get("cloud.provider")}.items() if v}


# ---------- logs -------------------------------------------------------------------------------------

INFRA_EVENTS = {   # event.name -> (title, severity); info ones are context, never incidents on their own
    "vm.stopped": ("VM stopped", Severity.warning),
    "vm.stopped_by_schedule": ("VM stopped by its instance schedule", Severity.info),
    "vm.started": ("VM started", Severity.info),
    "vm.host_error": ("host error, VM restarted", Severity.error),
    "vm.deleted": ("VM deleted", Severity.warning),
    "deploy.rollout": ("new release rolled out", Severity.info),
    "firewall.rule_deleted": ("firewall rule deleted", Severity.warning),
    "remediation.applied": ("remediation applied", Severity.info),     # the environment confirming an action
}
_STATUS_SEVERITY = {"major_outage": Severity.critical, "partial_outage": Severity.error,
                    "degraded_performance": Severity.warning, "under_maintenance": Severity.info}


def from_log_record(res_attrs: dict, scope: str, rec: dict) -> IncidentSignal | None:
    """One OTLP log record -> a signal, or None if it isn't one (info/debug application logs)."""
    a = attributes(rec.get("attributes"))
    body = str((rec.get("body") or {}).get("stringValue", "")).strip()
    ts = _ts(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano"))
    service, resource = _service(res_attrs), _resource(res_attrs)
    host = resource.get("name", "")
    uid = a.get("log.record.uid") or hashlib.sha256(f"{service}|{host}|{ts.isoformat()}|{body}".encode()).hexdigest()[:16]
    event = a.get("event.name")

    if event == "alert":
        state = "closed" if a.get("alert.state") == "resolved" else "open"
        return IncidentSignal(
            source="alert", service=service, severity=_SEVERITY_WORDS.get(a.get("alert.severity"), Severity.warning),
            timestamp=ts, signal_type=SignalType.alert, signal_id=f"alert:{a['alert.id']}:{state}",
            title=f"{a.get('alert.name', 'alert')}" + (f" on {host}" if host else ""), state=state,
            resource=resource, metric=f"alert/{a.get('alert.name')}", raw={"attributes": a, "body": body})
    if event == "status_page.component":
        sev = _STATUS_SEVERITY.get(a.get("status"))
        if not sev:
            return None
        return IncidentSignal(
            source=f"status:{a.get('provider', service)}", service=a.get("provider") or service, severity=sev,
            timestamp=ts, signal_type=SignalType.status,
            signal_id=f"status:{a.get('provider', service)}:{a.get('component')}:{a.get('status')}:{uid}",
            title=body or f"{a.get('component')} {a.get('status')}",
            resource={"type": "external_component", "name": str(a.get("component"))}, raw={"attributes": a})
    if event in INFRA_EVENTS:
        title, sev = INFRA_EVENTS[event]
        return IncidentSignal(
            source="infra_event", service=service, severity=sev, timestamp=ts, signal_type=SignalType.event,
            signal_id=f"event:{uid}", title=f"{host}: {title}" if host else title,
            resource={**resource, **({"actor": str(a["actor"])} if a.get("actor") else {})},
            metric=f"event/{event}", raw={"attributes": a, "body": body})
    if event:
        raise ValueError(f"unknown event.name {event!r}")
    if int(rec.get("severityNumber", 0)) < 17:
        return None
    return IncidentSignal(
        source="log", service=service, severity=_severity_number(int(rec["severityNumber"])), timestamp=ts,
        signal_type=SignalType.event, signal_id=f"log:{uid}",
        title=f"{host + ' ' if host else ''}{scope}: {body.splitlines()[0][:160] if body else ''}",
        resource=resource, metric=f"log/{scope}",       # same logger on the same host = same condition -> repeats collapse
        raw={"attributes": a, "body": body, "scope": scope})


# ---------- traces (aggregated by copilot/otel.py) -------------------------------------------------------

def from_failed_calls(caller: str, callee: str, host: str | None, minute: datetime, count: int, total: int,
                      example: str) -> IncidentSignal:
    """Failed calls from one service to another within one minute, from spans with status ERROR."""
    return IncidentSignal(
        source="trace", service=caller, severity=Severity.error if count / max(total, 1) > 0.2 else Severity.warning,
        timestamp=minute, signal_type=SignalType.event,
        signal_id=f"trace:{caller}:{callee}:{minute:%Y%m%dT%H%M}",
        title=f"{caller} -> {callee}: {count}/{total} calls failed ({example[:120]})",
        resource={"type": "host", "name": host, "peer": callee} if host else {"peer": callee},
        metric=f"trace/{callee}", value=float(count), raw={"example": example, "total": total})


# ---------- anomaly detector ------------------------------------------------------------------------

def from_anomaly(payload: dict) -> IncidentSignal:
    """{"id", "metric", "resource": {...}, "service"?, "observed", "expected", "zscore", "detected_at"}"""
    z = abs(float(payload["zscore"]))
    sev = Severity.critical if z >= 6 else Severity.error if z >= 4 else Severity.warning
    res = {k: str(v) for k, v in (payload.get("resource") or {}).items()}
    return IncidentSignal(
        source="anomaly_detector",
        service=payload.get("service") or service_for_resource(res.get("name")) or UNKNOWN_SERVICE,
        severity=sev,
        timestamp=_ts(payload["detected_at"]),
        signal_type=SignalType.anomaly,
        signal_id=f"anomaly:{payload['id']}",
        title=f"{payload['metric']} is {payload['observed']} (expected ~{payload['expected']}, z={z:.1f})",
        resource=res,
        metric=payload["metric"],
        value=float(payload["observed"]),
        raw=payload,
    )


# ---------- free text (LLM fallback) ------------------------------------------------------------------

class _Extracted(BaseModel):
    """What the LLM must pull out of a free-text alert. It must not invent: unknown -> null."""
    service: str | None = Field(description="service or system named in the alert; null if not named")
    severity: Severity
    started_at: str | None = Field(description="ISO-8601 time the problem started, only if stated; else null")
    title: str = Field(description="one-line summary using only words from the alert")
    resource_name: str | None = Field(description="host/VM/resource name if stated; else null")


def from_text(text: str, source_hint: str, llm, received_at: datetime | None = None) -> IncidentSignal:
    ext = llm.with_structured_output(_Extracted).invoke(
        "Extract the alert fields. Use only information present in the text; use null for anything missing.\n\n"
        + text)
    # Grounding: a service or resource the text never mentions is not accepted, so the LLM can't make one up.
    name = ext.resource_name if ext.resource_name and ext.resource_name in text else None
    service = ext.service if ext.service and ext.service.lower() in text.lower() else None
    service = service or service_for_resource(name) or UNKNOWN_SERVICE
    if service not in services() and service != UNKNOWN_SERVICE:
        service = UNKNOWN_SERVICE
    return IncidentSignal(
        source=f"freeform:{source_hint}",
        service=service,
        severity=ext.severity,
        timestamp=_ts(ext.started_at) if ext.started_at else (received_at or datetime.now(timezone.utc)),
        signal_type=SignalType.alert,
        signal_id="text:" + hashlib.sha256(text.encode()).hexdigest()[:16],
        title=ext.title,
        resource={"name": name} if name else {},
        raw={"text": text, "timestamp_from": "text" if ext.started_at else "received_at"},
    )


# ---------- entry point ----------------------------------------------------------------------------------

def safely(source_hint: str, raw, build) -> list[IncidentSignal | Rejected]:
    """Run a parser; a malformed item becomes Rejected with the reason (which field, and why)."""
    try:
        out = build()
        return [] if out is None else out if isinstance(out, list) else [out]
    except ValidationError as e:
        reason = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg'].removeprefix('Value error, ')}"
                           for err in e.errors())
        return [Rejected(source_hint=source_hint, reason=reason[:300], raw=raw)]
    except (KeyError, TypeError, ValueError) as e:
        return [Rejected(source_hint=source_hint, reason=f"{type(e).__name__}: {e}".splitlines()[0][:300], raw=raw)]


def intake(payload, source_hint: str, llm=None) -> list[IncidentSignal | Rejected]:
    """Single items: source_hint "anomaly" (detector payload) or anything else = free text (needs the LLM).
    OTLP documents are read by copilot/otel.py."""
    if source_hint == "anomaly":
        return safely(source_hint, payload, lambda: from_anomaly(payload))
    if llm is None:
        return [Rejected(source_hint=source_hint, reason="unknown format and no LLM configured", raw=payload)]
    return safely(source_hint, payload,
                  lambda: from_text(payload if isinstance(payload, str) else str(payload), source_hint, llm))
