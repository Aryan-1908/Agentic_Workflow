# Milestone plan

The plan of record for building the spec ([SPEC.md](SPEC.md)). **Milestones build the platform** (its capabilities);
they aren't use cases. The problems the platform handles are in [USE_CASES.md](USE_CASES.md), and can be added
at any time. Each milestone has a demo, which uses whichever use case is available (the ones named below are
examples), and a **"done when"** list of capability checks. A milestone is only marked done when all of them pass.

Legend: ✅ done · 🟡 built, live check pending · ⬜ not started

---

> **29 Sep 2026: single input = OpenTelemetry; no real cloud account for the POC.** All data arrives as OTLP through
> the OTel Collector; the simulator (`python -m sim`) produces realistic telemetry, one scenario per use case. "Live"
> below means through the real Collector with simulated data. Checks done earlier on GCP (read-only) are kept as history.

## M1 · Provider-agnostic LLM client + structured intake · 🟡
**Builds**
- `copilot/llm.py`: model chosen by config (`COPILOT_MODEL=provider:model`, per-role overrides), Gemini default.
- `copilot/signals.py`: validated `IncidentSignal` (source, service, severity, timestamp, signal type + id, resource, raw).
- `copilot/intake.py`: deterministic parsers for Cloud Monitoring alerts, anomaly events, Statuspage feeds; grounded LLM
  fallback for free text; `Rejected` with a reason for anything invalid.
- `copilot/otel.py` + `python -m copilot watch`: reads the OTel Collector's output: log records (alerts, infra events,
  status pages, errors), traces (failed calls per caller -> callee), metrics (anomaly detector). Contract: otel/README.md.
- `sim/`: the simulated Acme Shop emitting OTLP logs, metrics and traces; 13 scenarios; sends to the Collector.
- `otel/collector.yaml` + `tools/otelcol-contrib`: the OTel Collector, OTLP in, one JSON file out.

**Demo (e.g. U1):** `storefront-down` → an uptime `IncidentSignal` appears in `watch`; `storefront-restore` → its `closed` signal.

**Done when**
- [x] offline tests pass for every source format, bad payloads, and LLM grounding (13 tests)
- [x] OTel input (29 Sep): every simulator scenario is read with no rejections (offline test), and `db_down` through
      the real Collector arrives as valid signals of every kind (infra event, alerts, error logs, failed calls)
- [x] alerts open and close: a stopped VM's alert opens, and closes after the VM starts (offline test)
- [x] a new event appears in `watch` within 1 min of happening · 29 Sep, through the Collector: VM stopped at
      06:04:06.9, printed by `watch` at 06:04:09.8 (**~3 s**); both alerts within the next 2 s
- history: 28 Sep, read-only on a real GCP project, VM audit events and error logs arrived as valid signals
- [ ] switching `COPILOT_MODEL` to another provider needs no code change (checked with one other provider)

## M2 · Tool-enabled Correlation Agent · ✅
**Builds**
- `copilot/correlation.py`: `Case` (signals, services, root service, rationale, review flag) and the `Correlator`.
  Rules for the clear-cut decisions: duplicate, closing, repeat of the same condition, related (same service or a
  dependency, within 15 min), cases joined through a shared upstream, a downstream *local* symptom (CPU/memory/disk)
  not merged through a dependency. Every placement records its reason.
- `CorrelationAgent`: an LLM with the spec's tools (`list_open_cases`, `dependency_lookup`, `status_feed_lookup`,
  `merge_into_case`, `open_new_case`), used only for uncertain signals (unknown service, related but late, downstream
  local symptom). It can only merge into an existing open case; with no valid decision, the signal is flagged for review.
- `copilot/anomaly.py`: z-score detector (sustained, same direction), run by the OTel reader on host metrics.
- The dependency graph = service map + edges learned from trace client spans (`config.observe_edge`).
- `python -m copilot watch` shows the case each signal joins; `python -m copilot replay <log>` re-runs a recorded log.
- Scenarios for the checks: `python -m sim run db_down | payfast_outage | lookalike_cpu` (and 10 more).

**Demo (e.g. U6 cascade):** `sim run db_down` → alerts, errors and failed calls from db, orders-api and storefront → **one** case, root orders-db.

**Done when** (offline check passes · live demo pending)
- [x] duplicates (repeat polls, multi-region re-fires) collapse into one signal per condition · offline c01, c02
- [x] a cascade across dependent services becomes one case · offline c03, c04, c06
- [x] two look-alike but unrelated alerts at the same time become two cases · offline c05, c12, c13
- [x] an external status-page outage is attached to the services that depend on it · offline c07
- [x] time from first signal to case is recorded per case · offline test_time_to_correlate
- [x] live, observe-only (28 Sep): repeated guest-agent errors after each VM start collapse into one signal per burst, in the start's case
- [x] live, observe-only (28 Sep): two VMs stopped by the same schedule, with no dependency written, stay two cases (no guessing)
- [x] simulated through OTel (29 Sep): `db_down` → one case rooted at orders-db (offline test + real Collector);
      `payfast_outage` → one case payments-provider + storefront; `lookalike_cpu` → two cases; all 13 scenarios group
      as expected (tests/test_otel.py)
- [x] dependencies learned from traces: storefront → orders-api, payments-provider; orders-api → orders-db

## M3 · Persistent case memory + semantic index · ✅
**Builds**
- `copilot/memory.py` (SQLite, `runs/memory.sqlite`): every case (full signals, rationale, repeats), its services and
  signature, and case events (recommendation / approval / execution / outcome / note, filled from M5-M6). A case keeps
  its id across restarts and replays (identified by its signals).
- Lookups: `history(service)`, `similar_cases(case)` (same services + signal signature; info context left out),
  `outcome_stats(action, service)`; `restore()` reloads open cases into the correlator after a restart.
- `copilot/kb/index.py`: chunks `docs/kb/**/*.md` at headings, stable chunk ids `<path>#<heading>`, only changed
  chunks re-embedded, cosine search. Embeddings provider-agnostic (`COPILOT_EMBEDDINGS`, default Gemini).
- `watch` saves cases and prints "memory: seen N time(s) before…"; `copilot memory history|show`, `copilot kb index|search`.
- Two starter runbooks (`docs/kb/runbooks/`); the full knowledge base is M4.

**Demo:** replay a day of real events: the recurring guest-agent error is recognised as "seen before" from its 2nd time on.

**Done when**
- [x] cases and outcomes survive a restart · offline test; live (28 Sep): a second run kept the same 6 cases and ids, no duplicates
- [x] a repeated incident retrieves its past case as the top match · offline test; live (28 Sep): recurring guest-agent error "seen 1, 2, 3+ times before", similarity 1.0
- [x] an open case continues after a restart instead of splitting · offline test
- [x] the index is rebuilt from `docs/kb/` with stable chunk ids, and only changed chunks are re-embedded · offline test (stand-in embedder)
- [x] live (28 Sep): `kb index` with Gemini embeddings indexed 10 chunks; `kb search` returned the right runbook for all top 5

## M4 · Runbooks for RAG + evaluation baseline · ✅
**Builds**
- Knowledge base `docs/kb/`: 12 runbooks (9 for use cases U1, U3–U10, 3 **decoys**: GKE OOMKilled, Cloud SQL failover,
  TLS expiry) and 6 postmortems (82 sections).
- Search (`copilot/kb/index.py`): keyword (BM25), vector (embeddings), **hybrid** (reciprocal-rank fusion, default),
  optional LLM **reranking**. Diagnosis finds the best-matching documents by section, then reads each whole document.
- `copilot/diagnosis.py`: Gemini drafts root cause + steps with citations; **groundedness check** removes any claim that
  has no citation, cites a section not retrieved for the case, uses an unregistered action, uses an action its cited
  runbook doesn't contain, or that the LLM judge finds unsupported. No grounded root cause = "insufficient knowledge:
  escalate". Only the checked result is shown or stored. Action names: `copilot/actions.py` (executable in M6).
- Eval sets: `tests/kb_eval/retrieval.json` (33 questions, 8 paraphrased with no shared keywords, traps against the
  decoys) and `groundedness.json` (3 clean diagnoses, 9 planted fabrications: 6 structural, 3 semantic).
- `copilot kb eval [--rerank]` (baseline report in runs/eval/), `copilot diagnose <case>`, diagnoses in `watch`.

**Demo:** a live case (e.g. guest-agent burst) gets a diagnosis with citations; a planted fake step is removed with the reason.
Live 28 Sep: `diagnose case-d148eaf3` (real guest-agent case) → grounded, cause and "no automatic action" steps, all cited.

**Queued items, resolved 1 Oct (before M8):**
- [x] novel-incident eval (`tests/kb_eval/novel.json`, `copilot kb eval --novel`): live 1 Oct **7/7 escalated with no
      action**; the 3 unknown services with 0 Gemini calls. Note: `k8s-oom` was still described via the generic
      app-process-crashed runbook (a VM runbook for a pod), but no action survived
- [x] relevance floor: every KB document has an "Applies to:" line; only documents that apply to the incident's
      services can ground it; none applies → escalate without an LLM call
- [x] a fix that doesn't hold counts against itself: the same incident back within 30 min of a verified fix records
      that fix as failed (confidence drops, so the next attempt goes to a person; the next diagnosis is fresh)
- [x] diagnose once the case has settled (done in M6)
- [x] confidence calibration, measured in M8 (1 Oct): every action recommended at 0.75-1.0 was right (10/10), so no
      sign of overconfidence on this set and the thresholds stay (auto ≥ 0.8, escalate < 0.4). The wrong drafts were
      caught by grounding (confidence capped at 0.2), not by confidence. A real calibration curve needs cases where it
      is wrong: re-check when use cases are added (`python -m copilot eval` prints the table)

Noted for later: hybrid finds more (recall) but ranks the best section lower than vector alone (MRR 0.804 vs 0.879);
`kb eval --rerank` measures whether LLM reranking fixes the order.

**Done when**
- [x] retrieval recall@5 ≥ 0.9 on the eval set, baseline recorded for hybrid vs vector-only vs keyword-only
      · live 28 Sep (Gemini embeddings, 33 questions), recall@5 / MRR: keyword 0.848 / 0.813, vector 0.909 / 0.879,
        **hybrid 0.939** / 0.804. Hybrid missed 2 paraphrases ("machine powered off by a colleague…", "the program
        died…"). Report: runs/eval/kb-20260928-075107.json
- [x] 100% of planted fabricated causes/steps are caught · live 28 Sep with the Gemini judge: **9/9**, 0 false removals
      (rules alone catch the 6 structural ones)
- [x] no diagnosis reaches a human or the executor without passing the groundedness check · `diagnose()` only returns
      the checked result; tested (a made-up citation in a draft is removed before anything is shown)

## M5 · Ordered workflow with a durable approval checkpoint · ✅
**Builds**
- `copilot/workflow.py`: one LangGraph workflow per incident: diagnose → recommend → route → auto (execute) /
  approval (pause, then execute or close) / escalate / none → close. Checkpointed in `runs/workflow.sqlite` by case id;
  started from `watch` when a case becomes an incident; never re-run for a case already started.
- `copilot/routing.py`: recommendation from the grounded diagnosis (action, target, **blast radius** from action scope
  + service tier, **confidence** adjusted by past outcomes, **rollback** from the cited runbook); routing in code with
  thresholds from `[routing]` in the client config. M6 swaps in the Recommendation Agent.
- `copilot/outbox.py`: local ticket per case (`runs/tickets/<case>.md`, with timeline) and notifications
  (`runs/notifications.jsonl`: approvers / on-call / team).
- CLI: `copilot cases [--waiting]`, `copilot approve <case> --by <name>`, `copilot reject <case> --by <name> --reason …`.
- Execution is recorded, never performed (`dry-run`) until M6 adds simulated execution.

**Demo:** a case waits for approval, the process is killed and restarted, then `approve` resumes it where it stopped.

**Done when**
- [x] each lane exercised by a simulated incident through the Collector, with real Gemini diagnoses (29 Sep):
      `cpu_runaway` → **auto** (service.restart on batch-01, low, 0.9; dry run) · `db_down` → **approval** (vm.start on
      db-01, medium; approved → dry run) · `payfast_outage` → **escalate** (runbook says so; on-call notified) ·
      `guest_agent_noise` → **none** (known harmless noise). Diagnosis 16-26 s per incident.
- [x] a case awaiting approval survives a restart with no lost state · offline test (new process, approve, continues)
- [x] every approval or rejection records who, when and why · offline test (memory event, ticket timeline; a
      rejection without a reason and an approval without a name are refused)

## M6 · Specialised agents, then the full team · ✅
Decided 29 Sep: the spec's single Action Execution Agent dispatches to **one remediation agent per action family**;
remediation agents are code, and consult Gemini **only when stuck** (e.g. ambiguous verification). Cost rule: Gemini
only for new, unclear or failed-before cases.

**Builds (step 0: cost controls)**
- a known incident repeating (memory: same signature, stored grounded diagnosis, not rejected/failed) reuses that
  checked diagnosis: 0 Gemini calls
- a Gemini call counter and daily budget (`COPILOT_LLM_DAILY_BUDGET`); over budget, the case escalates with rule-based info
- judge calls only for claims that passed the free rule checks (already so)

**Builds (step 1) · built 29 Sep** `copilot/agents/`: the **Investigator** (evidence, similar past incidents, a
grounded diagnosis: reused, or new via Gemini) and the **Recommender** (action, target, blast radius, confidence,
rollback; code, with Gemini only when the target is unclear, restricted to the case's own resources). They pass a
structured `Investigation`. A target chosen by the LLM always needs a person's approval. Live 29 Sep:
`firewall_blocked` → Investigator 4 Gemini calls (24 s) → Recommender 0 calls → `firewall.restore`, high → approval.
Found: the target showed as `web-01`, but the action applies to the firewall rule; each action needs its own
parameters (rule name, VM, group), kept from the telemetry: part of step 2's parameter schemas.

**Builds (step 2) · built 29 Sep.** The six agents of the spec, as they are now:
Correlation (`correlation.py`) · Diagnosis/RAG = Investigator · Remediation Recommendation = Recommender · Approval
Orchestrator (`routing.py` + the workflow's approval pause) · Action Execution (`agents/remediation.py`: dispatcher +
Compute / Service / Deployment / Network agents; Escalation = the workflow's escalate step) · Safety Reviewer
(`agents/safety.py`, code). Verification from telemetry: `agents/verify.py`. The simulator applies actions sent through
`copilot/cloud.py` (in `--live` mode) and confirms them with `remediation.applied`. Diagnosis waits until the incident
has settled (20 s without new signals). Original plan below.

The six-agent team from the spec:
- Correlation, Diagnosis/RAG, Remediation Recommendation, Approval Orchestrator, Action Execution, Safety Reviewer.
- Action Execution = a dispatcher (code) to the remediation agents, each owning precondition check → execute (against
  the simulator) → verify from telemetry → rollback / escalate, with a circuit breaker:

  | agent | actions |
  |---|---|
  | Compute | `vm.start`, `vm.reset`, `vm.resize` |
  | Service | `service.restart`, `logs.rotate` |
  | Deployment | `mig.rollback`, `mig.resize`, `mig.recreate_instance` |
  | Network | `firewall.restore` |
  | Escalation | `escalate` |
- `copilot/actions.py`: the **action allowlist**, each action with a parameter schema, blast-radius rule, rollback and
  verification.
- Safety Reviewer as code (not a prompt): allowlist, blast-radius threshold, groundedness pass, parameter bounds.
- The simulator accepts actions (an approved `vm.start db-01` restarts db-01 in the simulated shop).

**Demo (e.g. U1):** storefront outage end to end: correlated, diagnosed with citations, `vm.start` executed against the
simulator, verified from the telemetry, closed.

**Done when**
- [x] a repeat of a known incident is handled with 0 Gemini calls; the budget stops calls when exhausted · live 29 Sep:
      `guest_agent_noise` 1st run 5 Gemini calls (32 s), repeat 0 calls (0 s); budget stop and escalation: offline tests
- [x] a low-risk incident is fixed with no human, and verified from the signals · live 29 Sep: `cpu_runaway` → auto →
      Service agent `service.restart` batch-01 → applied by the simulator → CPU alert closed → resolved
- [x] a high-risk incident never executes without approval · tests (approval lane without an approver, auto above the
      limit, LLM-chosen target: all blocked); live 29 Sep: `db_down` waited, approved → `vm.start` db-01 → 3 alerts closed
- [x] a non-allowlisted or out-of-bounds action is blocked, even if the LLM proposes it · tests: unregistered action,
      out-of-bounds size, VM outside the incident, missing parameter, data service, step not in the grounded diagnosis,
      circuit breaker; nothing reaches the environment
- [x] a failed action is rolled back or escalated, never left half-done · tests: wrong fix, environment refusal, no
      confirmation, silence while an alert is open → escalated; ambiguous → Gemini (wait/escalate), no LLM → escalate.
      No action defines an automatic rollback yet, so failures escalate

## M7 · Logging and retries (then hardening) · ✅
**Built 1 Oct:** `copilot/trace.py` (per-case trace, linked from tickets), `copilot/resilience.py` (3 attempts with
backoff for every Gemini, embeddings and control-API call; retries traced; fault injection `COPILOT_FAULTS`), clean
failure everywhere (a failed investigation, recommender, correlation agent or send escalates with the error),
`copilot/engine.py` (the main loop, shared by `watch`, tests and M8) with the **incident lifecycle**: a fixed or
no-action incident closes, a recurrence opens a new incident, late signals from before the fix join the closed one.
New scenario `flapping`.
**Builds**
- One structured log line per step, with a case trace id (`runs/trace/<case>.jsonl`), linked from the ticket.
- Retries with backoff on LLM and cloud calls; after retries, fail cleanly into escalation with the error attached.
- Circuit breaker: at most N remediation attempts per target per window, then escalate.
- Later hardening: tracing across the pipeline, fallback model, breakers on each external integration.

**Demo:** force the LLM and a tool call to fail: retries appear in the trace, then a clean escalation with the error.

**Done when**
- [x] every step of every case is in the trace · test + live: correlate, start, agent sub-steps, retries, investigate, recommend,
      route, approval waiting/decision, execute, escalate, close, lifecycle; each with timing or the error
- [x] forced failures are retried and then escalated, none disappear silently · live 1 Oct, `COPILOT_FAULTS=llm=3` on
      `bad_release`: draft retried (1 s, 2 s backoff), then escalated with the error; cloud send retried in tests
- [x] a flapping target stops being remediated after N attempts · live 1 Oct, `flapping`: 3 auto restarts, each a new
      incident, verified and closed; the 4th blocked by the circuit breaker and escalated; the simulator saw exactly 3

## M8 · Evaluation harness (runs 15-20 use-case instances) · 🟡 (only the MTTR baseline is open)
**Builds**
- `copilot/harness.py`: each scenario runs through the whole copilot (simulator → OTel file → engine → agents →
  scripted approver → Execution agent → verification) against its own fresh simulated shop and memory; nothing to
  tear down. Ground truth per scenario in `tests/m8_eval/ground_truth.json`: expected incidents (root), acceptable
  actions, targets, lanes and outcomes, actions that must never be sent, and the traps.
- Scripted approver, 2 simulated minutes after the card: approves only what the ground truth accepts, otherwise
  rejects (= an override); `rubber-stamp` (disk_full) approves anything, to prove the guardrails still hold.
- Report (`runs/eval/m8-<time>.md` + `.json`): the spec's metrics, a confidence-calibration table, every incident,
  every trap. CLI: `python -m copilot eval [--only a,b]`.
- New for M8: the "self-healing blip" trap (scenario `blip`; an approved action is not sent if every alert closed
  meanwhile: "recovered by itself").

**Found and fixed by the harness (1 Oct)**
- `flapping`: the first occurrence was seen by the anomaly detector and the recurrence by the CPU alert. Nothing in
  common, so the recurrence wasn't recognised and the restart ran automatically again. Recurrence is now matched on
  the resource that was fixed (test added first).
- `process_crash`, `blip`: Gemini picked the right runbook and step but left the root cause empty (the runbook doesn't
  say why a process dies), so it escalated. The draft prompt now asks for the matching documented situation as the
  root cause. The first wording invited an "it is not known why" clause, which the judge removed as unsupported;
  reworded to state only what the incident and the cited section show.

**Live result** (1 Oct, Gemini 2.5 Flash, report `runs/eval/m8-20261001-070612.md`): 15 scenarios, 16 incidents.

| spec metric | result |
|---|---|
| handled right (action, lane and outcome) | **16/16** |
| mean time to correlate (fault → incident settled) | 3.7 simulated min (mostly the monitoring alerts' own delay) |
| mean time to recommend | + 19.5 s of copilot processing per new incident |
| auto-remediation rate within the safe envelope | **2/2**, verified; 0 auto runs outside it |
| false-correlation rate | **0.0** (no merges, splits, spurious or missed incidents) |
| approval-to-execution latency | 0.01 s from approval to the action being sent |
| approver override rate | 0.0 (7 decisions) |
| MTTR (fault → verified fix) | 8.7 simulated min over 9 fixes (includes 2 min of approver time) |
| traps | **5/5**: look-alike, cascade, unsafe fix, flapping, self-healing blip |
| Gemini calls | 51 for the whole run (0 for healthy and novel) |

The earlier run that day, before the two fixes: 14/16 (both crash incidents escalated), 5/5 traps.

**Done when**
- [x] 100% of high-risk incidents go to a person or are escalated, never auto-executed · 0 auto executions outside the
      envelope; every tier-1 action went to approval; disk_full's rubber-stamped `logs.rotate` would still be blocked (test)
- [x] safe incidents are auto-fixed and verified · cpu_runaway and look-alike batch-01: auto, verified from telemetry
- [x] both correlation traps and the unsafe-fix trap are handled correctly · look-alike: 2 incidents, web-01 not
      restarted (escalated: real traffic); cascade: 1 incident, root orders-db; unsafe fix: escalated, nothing sent
- [ ] the MTTR baseline is agreed with the team (per incident type) and compared · our MTTR is measured per incident in
      the report; the manual number has to come from the team (spec: "MTTR vs manual baseline")

---

## Use cases
Moved to [USE_CASES.md](USE_CASES.md): the use-case catalogue, the eval traps applied to them, and where each can run.

## Credentials
None for the POC: the input is simulated OpenTelemetry and execution (M6) is simulated. Only `GOOGLE_API_KEY` for Gemini.

## After M8 · Copilot console (decided 29 Sep) · ✅ built 1 Oct
A local web page, **plain HTML + Python's built-in HTTP server** (no new packages, no Docker), reading the same files
the copilot writes:
- incidents list (status, lane) and incident detail: timeline, diagnosis with runbook citations, evidence
- approval cards with Approve / Reject (name + reason, same audit trail as `copilot approve`)
- latest signals from OTel, link to Jaeger for traces, today's Gemini usage vs budget
- **Ask the copilot**: questions answered only from memory, runbooks and live incidents, with citations, "I don't know"
  otherwise; one Gemini call per question, only when asked (the `ask` idea deferred on 28 Sep; reuses the M4 grounding check)

Built: `copilot/console.py` + `console.html` (`python -m copilot console`, http://127.0.0.1:8765) and `copilot/ask.py`.
- [x] incidents, detail (signals, diagnosis with citations and removed claims, recommendation, route, decision,
      execution, per-step trace with timings), Jaeger links, signals, notifications, Gemini usage; refreshes every 5 s
- [x] Approve / Reject goes through `Workflow.decide`, so the same checks (a name; a reason to reject) and audit trail
- [x] Ask: an answer is passed on only if it cites sources it was given (knowledge-base sections, `incident:<id>`);
      otherwise "I don't know". Live 1 Oct: "why was the last restart of batch-01 blocked?" answered from the incidents
      and the postmortem; "what is the database admin password?" → I don't know. 1 Gemini call each
- [x] local only: listens on 127.0.0.1, refuses other Host names (DNS rebinding) and posts that aren't JSON from its own
      page (another site can't press Approve); data is put in the page as text, never as HTML
- Limits: one request at a time (an approval keeps the page busy while the action is sent and verified); the signal
  list comes from `watch`'s signal log, so it is empty unless `watch` runs
