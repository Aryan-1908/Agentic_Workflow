# Remediation Copilot POC

A multi-agent incident copilot: signals → correlate → diagnose (grounded in runbooks) → recommend → approval by
blast radius → execute an allowlisted action → verify → remember. Spec: [docs/SPEC.md](docs/SPEC.md).
Plan and status: [docs/MILESTONES.md](docs/MILESTONES.md), [ARCHITECTURE.md](ARCHITECTURE.md).

**Single input: OpenTelemetry.** Everything arrives as OTLP through the OTel Collector ([otel/](otel/README.md)).
For the POC the telemetry comes from a simulator of a realistic shop; no cloud account is used. Other clouds plug in
later as Collector receivers, with no change to the copilot.

## Quick start
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    export GOOGLE_API_KEY=...                            # Gemini (any provider: COPILOT_MODEL=provider:model)
    .venv/bin/python -m copilot kb index                 # knowledge base (once)

    tools/otelcol-contrib --config otel/collector.yaml   # terminal 1: the OTel Collector
    .venv/bin/python -m sim run db_down                  # terminal 2: send an incident as OTel data
    .venv/bin/python -m copilot watch --once             # the copilot works on it

Full guide, with every scenario and what to expect: [docs/LIVE_TEST.md](docs/LIVE_TEST.md).

## Commands
| command | what it does |
|---|---|
| `python -m sim list` / `run <scenario> [--live]` | simulated Acme Shop telemetry (OTLP) for a use case |
| `python -m copilot watch [--once] [--tail]` | read the OTel input: signals → incidents → diagnosis → workflow |
| `python -m copilot cases [--waiting]` | incident workflows and approval cards |
| `python -m copilot approve / reject <case> --by <name>` | decide a case waiting for approval |
| `python -m copilot memory history <service>` / `show <case>` | past incidents |
| `python -m copilot kb index / search / eval` | knowledge base: index, search, M4 evaluation |
| `python -m copilot diagnose <case>` | grounded diagnosis of a stored case |

Tests: `.venv/bin/python -m pytest -q` (catalogue: [tests/README.md](tests/README.md)).
