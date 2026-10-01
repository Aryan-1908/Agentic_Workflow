# OpenTelemetry input

The copilot has **one input: OpenTelemetry**. Every source sends OTLP to the OTel Collector
(`otel/collector.yaml`), which writes it to `runs/otel/telemetry.jsonl`. The copilot reads only that file.

```
simulator (python -m sim)  ─┐                        ┌─▶ runs/otel/telemetry.jsonl ─▶ copilot intake (its only input)
later: GCP / AWS / Azure    ├─ OTLP ─▶ OTel Collector ┤
  receivers in the Collector ┘                        └─▶ Jaeger (traces, UI on http://localhost:16686): for people only
```

Run from the repo root (Jaeger is optional, for looking at traces):
```bash
tools/jaeger --config otel/jaeger.yaml                # UI: http://localhost:16686
tools/otelcol-contrib --config otel/collector.yaml
```

## Contract: how incidents look in OTel
Standard OpenTelemetry semantic conventions wherever one exists. Any source (the simulator today, a cloud receiver
or an application SDK later) must follow this for the copilot to understand it.

**Resource attributes** (who it's about): `service.name` (required), `host.name`, `cloud.provider`, `deployment.environment`.

**Logs**
| kind | how it's recognised | becomes |
|---|---|---|
| application error | `severityNumber` ≥ 17 (ERROR); scope name = the logger (e.g. `GCEGuestAgent`) | event signal; repeats of the same logger on the same host collapse |
| alert from a monitoring system | `event.name = "alert"`; `alert.id`, `alert.name`, `alert.state` (`firing` / `resolved`), `alert.severity` | alert signal (opens, then closes) |
| infrastructure event | `event.name` = `vm.stopped`, `vm.started`, `vm.stopped_by_schedule`, `vm.host_error`, `vm.deleted`, `deploy.rollout`, `firewall.rule_deleted`, `remediation.applied` (with `request.id`, `action`, `target`, `result`); `actor` | event signal (info-level ones are context) |
| external status page | `event.name = "status_page.component"`; `provider`, `component`, `status` | status signal (only when not `operational`) |

Every log record should carry `log.record.uid` (stable id); without it the copilot derives one from its content.

**Metrics** (gauges): `system.cpu.utilization`, `system.filesystem.utilization` (0-1), per host. The copilot's
anomaly detector watches them (sustained deviation from each host's own baseline).

**Traces**: `SERVER` spans for requests a service handles, `CLIENT` spans for calls it makes, with `peer.service`
(or `db.system` + `server.address` for databases). They give the copilot:
- the **dependency graph** (who calls whom), learned from the spans, added to `config/services.toml`;
- **error signals**: failed calls from one service to another, per minute (`status.code = ERROR`).
