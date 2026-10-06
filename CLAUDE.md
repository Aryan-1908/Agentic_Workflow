# Remediation Copilot POC

A multi-agent AIOps copilot, built to the team spec in docs/SPEC.md. The plan of record (all milestones M1-M8,
with "done when" checks) is docs/MILESTONES.md; keep its checkboxes and status current. Milestones build the
platform; use cases (the problems it handles) are separate, in docs/USE_CASES.md. Status and layout are in
ARCHITECTURE.md. The knowledge base is sample docs; approvals are local stand-ins.

## Input: OpenTelemetry only
- The copilot's single input is OTLP through the OTel Collector (`tools/otelcol-contrib`, `otel/collector.yaml`),
  read from `runs/otel/telemetry.jsonl` by `copilot/otel.py`. Never add a cloud-specific input to the copilot; a new
  source is a Collector receiver or an OTel emitter following the contract in `otel/README.md`.
- No real cloud account for the POC: telemetry comes from the simulator (`python -m sim`). Each use case is a scenario
  in `sim/scenarios.py`; a new use case = a scenario + a runbook + an expectation in `tests/test_otel.py`.

## Layout
- `copilot/`: `signals.py`, `intake.py`, `otel.py` (M1); `correlation.py`, `anomaly.py` (M2); `memory.py`,
  `kb/index.py` (M3); `diagnosis.py`, `kb/evaluate.py`, `actions.py` (M4); `routing.py`, `workflow.py`, `outbox.py` (M5); `usage.py` (LLM budget); `agents/` (M6: investigator, recommender, remediation, safety, verify); `cloud.py` (control API); `engine.py` (main loop + lifecycle), `trace.py`, `resilience.py` (M7); `harness.py` (M8 eval, ground truth in
  `tests/m8_eval/`); `console.py` + `console.html`, `ask.py` (web console, after M8);
  `llm.py`, `config.py` (service map + dependencies learned from traces). Knowledge base: `docs/kb/`; eval sets: `tests/kb_eval/`
- `sim/`: `world.py` (the Acme Shop and its telemetry), `scenarios.py`, `otlp.py`
- `config/services.toml`: Acme Shop services, tiers, dependencies
- `clients/<name>.toml`: telemetry file, service map, routing thresholds, execution mode
- `tests/`: offline pytest suite (see tests/README.md); correlation cases are data files in `tests/correlation_cases/`

## Decisions
- Take every design decision from docs/SPEC.md and cite the part used. Ask the user only when the spec doesn't answer it.

## Rules for the copilot
- Execution only ever runs pre-registered action types, never arbitrary commands (spec guardrail).
- Intake never guesses: unknown fields stay null/"unknown", bad payloads become Rejected with a reason.
- Nothing the LLM says is accepted unless it is grounded in the input or the knowledge base.
- Every new behaviour gets a test case; when a run shows a wrong decision, add it as a case first, then fix.
- Keep it simple: no containers or sandboxes (user preference). The Collector runs as a local binary.
- Cost: Gemini only for new, unclear or failed-before cases (user rule). Deterministic first; repeats reuse checked
  diagnoses; every call goes through `get_llm` (counted, budgeted in copilot/usage.py). Agents follow the same rule.

## Dev
- venv at `.venv`; the Collector and Jaeger binaries in `tools/` (git-ignored; download steps in docs/LIVE_TEST.md).
  Jaeger (UI :16686) is for people to see traces; the copilot never reads from it.
- No git commits during the POC until results are good (user preference).
- LLM is Gemini by default (`COPILOT_MODEL`, `GOOGLE_API_KEY`).
