"""OTLP JSON building blocks (the OTLP/HTTP JSON encoding: https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding)
and the two ways to deliver them: POST to the OTel Collector, or append to a file in the Collector's own output format."""
import json, os, pathlib, urllib.request

INFO, WARN, ERROR, FATAL = 9, 13, 17, 21
SEVERITY_TEXT = {INFO: "INFO", WARN: "WARN", ERROR: "ERROR", FATAL: "FATAL"}
SPAN_SERVER, SPAN_CLIENT = 2, 3
STATUS_OK, STATUS_ERROR = 1, 2


def value(v) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def attrs(d: dict) -> list[dict]:
    return [{"key": k, "value": value(v)} for k, v in d.items() if v is not None]


def resource(service: str, host: str | None = None, **extra) -> dict:
    return {"attributes": attrs({"service.name": service, "host.name": host, "cloud.provider": "sim",
                                 "deployment.environment": "poc", **extra})}


def log_record(ts_ns: int, severity: int, body: str, uid: str, trace_id: str | None = None, **attributes) -> dict:
    rec = {"timeUnixNano": str(ts_ns), "observedTimeUnixNano": str(ts_ns), "severityNumber": severity,
           "severityText": SEVERITY_TEXT.get(severity, "INFO"), "body": {"stringValue": body},
           "attributes": attrs({"log.record.uid": uid, **attributes})}
    if trace_id:
        rec["traceId"] = trace_id
    return rec


def gauge(name: str, unit: str, points: list[tuple[int, float, dict]]) -> dict:
    return {"name": name, "unit": unit, "gauge": {"dataPoints": [
        {"timeUnixNano": str(ts), "asDouble": v, "attributes": attrs(a)} for ts, v, a in points]}}


def span(trace_id: str, span_id: str, parent: str | None, name: str, kind: int, start_ns: int, end_ns: int,
         error: str | None = None, **attributes) -> dict:
    s = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": kind,
         "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(end_ns), "attributes": attrs(attributes),
         "status": {"code": STATUS_ERROR, "message": error} if error else {"code": STATUS_OK}}
    if parent:
        s["parentSpanId"] = parent
    return s


class Batch:
    """Collects one tick's telemetry, grouped by resource, as the three OTLP documents."""

    def __init__(self):
        self.logs: dict[str, tuple[dict, list]] = {}
        self.metrics: dict[str, tuple[dict, list]] = {}
        self.spans: dict[str, tuple[dict, list]] = {}

    @staticmethod
    def _add(bucket, res: dict, scope: str, item):
        key = json.dumps(res, sort_keys=True) + "|" + scope
        bucket.setdefault(key, (res, scope, []))[2].append(item)

    def log(self, res, scope, rec):
        self._add(self.logs, res, scope, rec)

    def metric(self, res, scope, m):
        self._add(self.metrics, res, scope, m)

    def span(self, res, scope, s):
        self._add(self.spans, res, scope, s)

    def documents(self) -> dict[str, dict]:
        def group(bucket, outer, inner, items):
            by_res: dict[str, dict] = {}
            for res, scope, rows in bucket.values():
                r = by_res.setdefault(json.dumps(res, sort_keys=True), {"resource": res, inner: []})
                r[inner].append({"scope": {"name": scope}, items: rows})
            return {outer: list(by_res.values())} if by_res else None
        docs = {"logs": group(self.logs, "resourceLogs", "scopeLogs", "logRecords"),
                "metrics": group(self.metrics, "resourceMetrics", "scopeMetrics", "metrics"),
                "traces": group(self.spans, "resourceSpans", "scopeSpans", "spans")}
        return {k: v for k, v in docs.items() if v}


class CollectorSink:
    """Sends OTLP/HTTP JSON to the OTel Collector (the normal path)."""

    def __init__(self, endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")):
        self.endpoint = endpoint.rstrip("/")

    def send(self, docs: dict[str, dict]):
        for signal, doc in docs.items():
            req = urllib.request.Request(f"{self.endpoint}/v1/{signal}", data=json.dumps(doc).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status >= 300:
                    raise RuntimeError(f"collector refused {signal}: HTTP {r.status}")


class FileSink:
    """Appends the same documents the Collector's file exporter writes (tests, or running without the Collector)."""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, docs: dict[str, dict]):
        with open(self.path, "a") as f:
            for doc in docs.values():
                f.write(json.dumps(doc) + "\n")
