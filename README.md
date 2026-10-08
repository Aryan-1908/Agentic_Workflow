# Remediation Copilot POC

A multi-agent incident copilot: signals → correlate → diagnose (grounded in runbooks) → recommend → approval by
blast radius → execute an allowlisted action → verify → remember. Spec: [docs/SPEC.md](docs/SPEC.md).
Plan and status: [docs/MILESTONES.md](docs/MILESTONES.md), [ARCHITECTURE.md](ARCHITECTURE.md).
Use cases: [docs/USE_CASES.md](docs/USE_CASES.md).

**Single input: OpenTelemetry.** Everything arrives as OTLP through the OTel Collector ([otel/](otel/README.md)).
For the POC the telemetry comes from a simulator of a realistic shop; no cloud account is used. Other clouds plug in
later as Collector receivers, with no change to the copilot.

**Status:** M2-M7 done; M1 and M8 each have one open check (another LLM provider; the team's manual MTTR baseline).
The M8 evaluation (15 scenarios, live Gemini) handled 16/16 incidents right and passed 5/5 traps. The web console
(after M8) shows incidents, approvals, signals and "Ask the copilot".

## Quick start
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    echo 'GOOGLE_API_KEY=...' > .env                     # Gemini key, read automatically; .env is git-ignored
    .venv/bin/python -m copilot kb index                 # knowledge base (once)
    .venv/bin/python -m pytest -q                        # offline tests

## Live demo (one terminal each, in this order)
    tools/jaeger --config otel/jaeger.yaml               # optional: trace viewer, http://localhost:16686
    tools/otelcol-contrib --config otel/collector.yaml   # the OTel Collector (prints nothing when healthy)
    .venv/bin/python -m copilot watch                    # the copilot: reads new telemetry every 5 s
    .venv/bin/python -m copilot console                  # web console: http://127.0.0.1:8765
    .venv/bin/python -m sim run db_down --live --tick 3 --minutes 400   # the incident

- The simulator creates the telemetry and applies approved fixes, so keep it running until the incident shows
  resolved. `--minutes` are simulated minutes: minutes × tick = real seconds (400 × 3 s = 20 minutes to approve).
  If it has stopped, an approved fix is reported "not_applied" and escalated.
- Next scenario: once the incident is closed, start the simulator again with another scenario (`python -m sim list`).
  Run ones that end escalated (e.g. `payfast_outage`) last: an open incident absorbs related new signals.
- Clean start: stop the Collector, `python -m copilot reset --yes` (add `--keep-memory` to keep past incidents, so
  repeats reuse their diagnosis with no Gemini call), then start the Collector again.

Full guide, with every scenario and what to expect: [docs/LIVE_TEST.md](docs/LIVE_TEST.md).

## Commands
| command | what it does |
|---|---|
| `python -m sim list` / `run <scenario> [--live --tick S --minutes N]` | simulated Acme Shop telemetry (OTLP) for a use case |
| `python -m copilot watch [--once] [--tail]` | read the OTel input: signals → incidents → diagnosis → workflow |
| `python -m copilot console [--port 8765]` | web console: incidents, approval cards, signals, Gemini usage, Ask the copilot |
| `python -m copilot cases [--waiting]` | incident workflows and approval cards |
| `python -m copilot approve / reject <case> --by <name>` | decide a case waiting for approval |
| `python -m copilot eval [--only a,b]` | M8: every scenario through the copilot, scored; report in `runs/eval/` |
| `python -m copilot reset --yes [--keep-memory]` | clean demo (stop the Collector first; keeps the knowledge-base index) |
| `python -m copilot memory history <service>` / `show <case>` | past incidents |
| `python -m copilot kb index / search / eval` | knowledge base: index, search, M4 evaluation |
| `python -m copilot diagnose <case>` | grounded diagnosis of a stored case |

Gemini is called only for new, unclear or failed-before cases, within a daily budget (`COPILOT_LLM_DAILY_BUDGET`,
default 200). Tests: `.venv/bin/python -m pytest -q` (catalogue: [tests/README.md](tests/README.md)).
