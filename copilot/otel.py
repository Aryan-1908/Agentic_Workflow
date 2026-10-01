"""Reads the copilot's single input: the OTel Collector's output file (otel/collector.yaml, one OTLP JSON document
per line) and turns it into IncidentSignals.

  resourceLogs     -> alerts, infrastructure events, status-page updates, ERROR application logs (intake.from_log_record)
  resourceSpans    -> failed calls per caller -> callee per minute; every client span also teaches the dependency graph
  resourceMetrics  -> the anomaly detector, per host and metric

The reader remembers how far it has read, so each poll returns only what's new."""
import json, pathlib
from collections import defaultdict
from datetime import datetime, timezone

from .anomaly import detect
from .config import observe_edge, service_for_resource
from .intake import _ts, attributes, from_anomaly, from_failed_calls, from_log_record, safely

DEFAULT_PATH = pathlib.Path("runs") / "otel" / "telemetry.jsonl"
SPAN_CLIENT, STATUS_ERROR = 3, 2
WATCHED_METRICS = ("system.cpu.utilization", "system.filesystem.utilization")


class OTelReader:
    def __init__(self, path: str | pathlib.Path = DEFAULT_PATH, from_start: bool = True):
        self.path = pathlib.Path(path)
        self.offset = 0 if from_start or not self.path.exists() else self.path.stat().st_size
        self.series: dict[tuple, list] = defaultdict(list)    # (service, host, metric) -> [(ts, value)]
        self.reported: set[str] = set()

    def poll(self) -> list:
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:      # the file was rotated or recreated
            self.offset = 0
        out = []
        with open(self.path) as f:
            f.seek(self.offset)
            while line := f.readline():
                if not line.endswith("\n"):              # a line still being written: read it next time
                    break
                self.offset = f.tell()
                out += self.document(line)
        return out

    def document(self, line: str) -> list:
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as e:
            return safely("otlp", line[:200], lambda: (_ for _ in ()).throw(ValueError(f"not JSON: {e}")))
        out = []
        for rl in doc.get("resourceLogs", []):
            res = attributes(rl.get("resource", {}).get("attributes"))
            for sl in rl.get("scopeLogs", []):
                scope = (sl.get("scope") or {}).get("name") or "app"
                for rec in sl.get("logRecords", []):
                    out += safely("otlp.log", rec, lambda rec=rec: from_log_record(res, scope, rec))
        out += self._spans(doc.get("resourceSpans", []))
        out += self._metrics(doc.get("resourceMetrics", []))
        return out

    def _spans(self, resource_spans) -> list:
        calls = defaultdict(lambda: {"total": 0, "failed": 0, "example": "", "host": None})
        for rs in resource_spans:
            res = attributes(rs.get("resource", {}).get("attributes"))
            caller, host = res.get("service.name"), res.get("host.name")
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    if sp.get("kind") != SPAN_CLIENT or not caller:
                        continue
                    a = attributes(sp.get("attributes"))
                    callee = a.get("peer.service") or service_for_resource(a.get("server.address")) or a.get("server.address")
                    if not callee:
                        continue
                    observe_edge(caller, callee)
                    minute = _ts(sp["startTimeUnixNano"]).replace(second=0, microsecond=0)
                    c = calls[(caller, callee, minute)]
                    c["total"] += 1
                    c["host"] = host
                    if (sp.get("status") or {}).get("code") == STATUS_ERROR:
                        c["failed"] += 1
                        c["example"] = c["example"] or (sp.get("status") or {}).get("message", "error")
        return [x for (caller, callee, minute), c in calls.items() if c["failed"]
                for x in safely("otlp.trace", c, lambda: from_failed_calls(
                    caller, callee, c["host"], minute, c["failed"], c["total"], c["example"]))]

    def _metrics(self, resource_metrics) -> list:
        touched = set()
        for rm in resource_metrics:
            res = attributes(rm.get("resource", {}).get("attributes"))
            key_base = (res.get("service.name"), res.get("host.name"))
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    if m.get("name") not in WATCHED_METRICS:
                        continue
                    for p in (m.get("gauge") or {}).get("dataPoints", []):
                        key = (*key_base, m["name"])
                        self.series[key].append((_ts(p["timeUnixNano"]), float(p.get("asDouble", p.get("asInt", 0)))))
                        touched.add(key)
        out = []
        for service, host, metric in touched:
            payload = detect(sorted(self.series[(service, host, metric)]), metric,
                             {"type": "host", "name": host}, service=service)
            if payload and payload["id"] not in self.reported:
                self.reported.add(payload["id"])
                out += safely("anomaly", payload, lambda: from_anomaly(payload))
        return out
