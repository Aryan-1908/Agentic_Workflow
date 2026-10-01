# Autonomous Correlation, Detection & Remediation Copilot (spec)

Domain: Observability / AIOps / Incident Management. Drafted by Mohinder Kumar, 25 Sep 2026 (source: index.pdf).
Transcribed so it stays with the code.

## Scenario
The ops team at a mid-size tech company is overwhelmed by disconnected alerts and anomalies. Each incident needs an
engineer to cross-reference signals, search docs and past incidents for a probable cause, decide on a fix, open a
ticket and remediate by hand. Build a multi-agent system that correlates related signals into one incident,
diagnoses the likely root cause using the organization's own knowledge base, proposes a remediation, and, gated by
human approval proportional to blast radius, executes it, closing the loop from signal to fix.

## Pain points
- Alert fragmentation: anomalies and threshold alerts are never correlated; one problem produces many pages.
- Manual root-causing: engineers search docs, history and discussion by hand; nothing synthesizes signals into one cause.
- No recommendation layer: nothing decides which action is appropriate, why, or whether it's safe unattended.
- No approval gate: remediation is fully manual or fully automatic; no blast-radius-aware human checkpoint.

## Mandatory components (multi-agent team)
- Signal Correlation Agent: ingests alerts, anomalies, external dependency-health signals; clusters temporally /
  topologically related events into one incident context; dedupes noise.
- Diagnosis / RAG Agent: grounded retrieval over internal docs, past incidents, runbooks to propose a root cause, with
  groundedness evaluation so it never fabricates a cause or step.
- Remediation Recommendation Agent: concrete action (restart, scale, rollback, config change, or "escalate only") with
  blast radius, confidence and rollback plan.
- Approval Orchestrator Agent: routes to auto-execution, single-approver sign-off, or escalation by blast-radius /
  confidence thresholds; owns the human-in-the-loop interrupt and the audit trail of who approved what.
- Action Execution Agent: executes the approved remediation through automation integrations, verifies post-action
  signal state, rolls back or escalates on failure.
- Safety / Guardrail Reviewer Agent: hard-blocks auto-execution above the blast-radius threshold without sign-off,
  blocks fabricated remediation steps, enforces an allowlist of executable action types.
- Persistent case memory: per-incident and per-service history (past incidents, remediations, outcomes).

## End-to-end workflow
signal intake (alerts + anomalies + external status feeds) → correlate (cluster, dedupe) → diagnose (RAG,
groundedness-checked) → recommend (action + blast radius + confidence + rollback plan) → conditional routing:
low blast radius + high confidence → auto-execute, log, notify; elevated / ambiguous → human-approval interrupt
(approval card); safety-critical / low confidence → hard-block, escalate to on-call → execute with checkpointing →
verify post-action signal state → close the loop (case memory, ticket update, notify).
A stateful, checkpointed workflow: an incident can sit "awaiting approval" indefinitely without losing state.

## Guardrails
- No auto-execution above a configured blast-radius threshold: hard human-approval interrupt, no exceptions.
- Root cause and remediation steps checked for groundedness against retrieved KB content before shown or executed.
- Action allowlist / denylist: the Execution Agent only invokes pre-registered remediation types, never arbitrary commands.
- Full audit trail: every recommendation, approval/rejection and execution outcome logged on the incident.
- Least-privilege credentials for executing actions, separate from read-only lookup credentials.
- Rate limiting / circuit breaker on repeated remediation attempts against the same target.

## Success metrics
Mean time to correlate and to recommend; auto-remediation rate within the safe envelope; false-positive /
false-correlation rate; approval-to-execution latency and approver override rate; MTTR reduction vs manual baseline.

## Milestones (each demoable)
- M1 Provider-agnostic LLM client + structured intake: parse alerts/anomalies into a validated IncidentSignal
  (source, service, severity, timestamp, signal type).
- M2 Tool-enabled Correlation Agent: temporal/topological clustering, dependency-graph lookup, status-feed lookup, "merge into case".
- M3 Persistent memory + semantic index: per-service incident history, vector index over runbooks and postmortems.
- M4 A handful of runbooks for RAG + evaluation baseline: hybrid search + reranking; groundedness evaluation.
- M5 Ordered steps: intake → correlate → diagnose → recommend → conditional routing, durable checkpoint at approval.
- M6 Two specialized agents (investigate / recommend) passing information; later the full six-agent team.
- M7 Logging for every step, retries before failing cleanly; later tracing, fallback, circuit breakers.
- M8 15-20 example incidents (safe and risky); the system auto-fixes the safe ones and always asks before risky ones.

## Eval data
15-20 examples: low-risk (safe to auto-fix), clearly high-risk (always to a person), tricky cases where two similar
alerts are unrelated, and a "fix" that looks reasonable but is unsafe.
