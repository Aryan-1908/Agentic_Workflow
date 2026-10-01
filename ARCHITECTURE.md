# Architecture and milestone status

Spec: [docs/SPEC.md](docs/SPEC.md). Alignment review: https://claude.ai/artifact/DmbhtLzvDvJeF9CXjDvBsC

```
 sources                        single input                      the copilot
 ───────                        ────────────                      ───────────
 simulator (python -m sim) ─┐
 later: GCP / AWS / Azure   ├─OTLP─▶ OTel Collector ─▶ telemetry.jsonl ─▶ intake ─▶ correlate ─▶ memory
   receivers, app SDKs     ─┘       (otel/collector.yaml)                   │                     │
                                                                           ▼                     ▼
                                                       diagnose (RAG, grounded) ─▶ recommend ─▶ route ─▶ auto / approval / escalate
```

## Decisions
- The spec is the scope. The earlier "any cloud task" / cost work is parked. (25 Sep)
- **Single input: OpenTelemetry** (29 Sep). All data arrives as OTLP through the OTel Collector (binary in `tools/`,
  config `otel/collector.yaml`), written to one file the copilot reads. New clouds are Collector receivers; the copilot
  doesn't change. Contract (which attributes mean what): [otel/README.md](otel/README.md).
- **No real cloud account for the POC** (29 Sep). A simulator emits realistic OTel logs, metrics and traces for the
  Acme Shop, one scenario per use case. M6 execution will be simulated against it. The earlier GCP input code is
  archived in `runs/archive/gcp-input-20260929.tar.gz`.
- Trace UI: Jaeger v2 (`tools/jaeger`, `otel/jaeger.yaml`, http://localhost:16686); the Collector sends it a copy of
  the traces. For people only; the copilot's input stays the file. (29 Sep)
- The service dependency graph = the service map (`config/services.toml`) plus edges learned from traces.
- The knowledge base is sample runbooks and postmortems for the Acme Shop. (25 Sep)
- Approvals, tickets and notifications are local stand-ins, behind interfaces. (25 Sep)
- LLM access is provider-agnostic (`COPILOT_MODEL=provider:model`), with Gemini as the default.

## Layout
| path | role |
|---|---|
| `otel/` | Collector config, Jaeger config (trace UI for people), the OTel contract |
| `sim/` | the simulator: `world.py` (the shop and its telemetry), `scenarios.py` (use cases), `otlp.py` (OTLP JSON, sinks) |
| `copilot/otel.py` | reads the Collector's output: logs, traces (failed calls, dependency edges), metrics (anomalies) |
| `copilot/agents/` | M6 agents: `investigator.py`, `recommender.py`, `remediation.py` (execution + per-family agents), `safety.py`, `verify.py` |
| `copilot/engine.py` | the main loop (watch, tests, M8): correlate → workflow → close / reopen |
| `copilot/trace.py`, `copilot/resilience.py` | per-case trace (runs/trace/), retries with backoff + fault injection |
| `copilot/cloud.py` | the environment's control API (the simulator's action inbox) |
| `copilot/` | `intake.py`, `signals.py`, `anomaly.py`, `correlation.py`, `memory.py`, `kb/index.py`, `kb/evaluate.py`, `diagnosis.py`, `actions.py`, `routing.py`, `workflow.py`, `outbox.py`, `llm.py`, `config.py`, CLI |
| `docs/kb/` | knowledge base: runbooks, postmortems, decoys |
| `config/services.toml` | Acme Shop services, tiers, dependencies |
| `clients/*.toml` | per environment: telemetry file, service map, routing thresholds, execution mode |
| `tests/` | offline tests; `tests/README.md` lists every test case |

## Milestones
Milestones build the platform: [docs/MILESTONES.md](docs/MILESTONES.md) (build list, demo, "done when" checks).
Use cases (the problems it handles, added any time): [docs/USE_CASES.md](docs/USE_CASES.md).

| | milestone | status |
|---|---|---|
| M1 | provider-agnostic LLM client + structured intake | 🟡 OTel intake live (event → watch in ~3 s); other-provider check open |
| M2 | Correlation Agent + anomaly detector | ✅ every simulator scenario groups correctly (tests + Collector runs) |
| M3 | case memory + semantic index | ✅ |
| M4 | runbooks/postmortems, hybrid search + rerank, groundedness | ✅ hybrid recall@5 0.939, groundedness 9/9 caught |
| M5 | ordered workflow with durable approval checkpoint | ✅ all four lanes live on simulated incidents with Gemini |
| M6 | investigator + recommender, then the six-agent team; action allowlist | ✅ remediation agents execute against the simulator and verify from telemetry |
| M7 | per-step trace, retries, circuit breaker | ✅ trace per case, retries + clean escalation, incident lifecycle, breaker live |
| M8 | 15-20 incident evals + metrics report | ⬜ (simulator scenarios) |
