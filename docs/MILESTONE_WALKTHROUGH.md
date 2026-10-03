# Milestone walkthrough

How each of the eight milestones is covered, and the idea behind each one. Written to be read aloud
in a review: every section says what the spec asked for, what was built, **why it was built that
way**, and how to show it working.

The system is a copilot for incident response. Telemetry comes in; related signals become one
incident; the incident is diagnosed against the company's own runbooks; a remediation is proposed
with its blast radius; and depending on risk and confidence it either runs by itself, waits for a
person, or goes to on-call. Everything is recorded.

Two ideas run through all eight milestones, and they explain most of the design decisions:

**Deterministic first, the model only where judgement is needed.** Rules are predictable, testable,
and cheap. The LLM is used where the problem is genuinely open-ended — reading a runbook, phrasing a
cause — and nowhere else. Correlation decides the clear cases in code and asks the model only about
uncertain signals. Routing is code. The safety reviewer is code.

**Nothing the model says is trusted on its own.** A diagnosis must cite a runbook section that was
actually retrieved. A remediation must be an action the allowlist registered. The safety reviewer
re-checks everything immediately before execution, including after a human approved. The model
proposes; the code disposes.

---

## M1 · Provider-agnostic LLM client + structured intake

**Spec:** parse alerts and anomalies into a validated `IncidentSignal` — source, service, severity,
timestamp, signal type.

### The idea

Everything downstream depends on signals being trustworthy, so intake is where guessing is banned.
Two concepts carry this milestone.

**One input, not many.** The copilot reads OpenTelemetry and nothing else. A new source is not new
code in the copilot — it is a Collector receiver or an emitter following the OTLP contract. That
keeps cloud-specific parsing out of the core, and it is why the same copilot can read a simulator
today and a real cloud tomorrow without touching its logic.

**Rejected, never silently dropped.** A payload that cannot be parsed becomes an explicit `Rejected`
with a reason. The alternative — dropping it — means an incident nobody ever hears about, which is
the worst failure an incident system can have.

**Provider-agnostic by configuration.** `COPILOT_MODEL=provider:model`, with per-role overrides, so
the diagnosis model, the judge and the recommender can differ. Changing provider is a config change,
not a code change.

### What was built

| File | What it does |
|---|---|
| `copilot/signals.py` | The `IncidentSignal` model with validators — a timestamp must carry a timezone and cannot be in the future; service, source and title cannot be blank |
| `copilot/intake.py` | Deterministic parsers: alerts (`event.name=alert`), infrastructure events (`vm.*`, `deploy.rollout`, `firewall.rule_deleted`), status pages, ERROR-level logs. Free text goes through the LLM and is validated the same way |
| `copilot/otel.py` | Reads OTLP; aggregates failed calls seen in traces per caller→callee per minute |
| `copilot/anomaly.py` | Z-score detector over metrics, for sustained moves in one direction |
| `copilot/llm.py` | `init_chat_model` — the provider comes from config |

### How to show it

Run any scenario in the UI. Every row in the result table began as a raw OTLP record: the service
name, severity and timestamp on screen were parsed here.

**Open:** one live check — a real event appearing in `watch` within a minute. It needs someone to
start a VM while the copilot is running.

---

## M2 · Tool-enabled Correlation Agent

**Spec:** temporal and topological clustering, dependency-graph lookup, status-feed lookup, and a
merge-into-case tool.

### The idea

This milestone exists because of the first pain point in the spec: one problem produces many pages.
Correlation is what turns a firehose into an incident.

**Rules for the clear cases, the model only for the uncertain ones.** Six rules decide most signals:

1. **duplicate** — the same signal id again (repeat polls, re-deliveries) → dropped
2. **closing** — an alert's "closed" signal → marks the open one closed
3. **repeat** — the same condition already in a case → counted, not re-added
4. **related** — same service, or a dependency edge, within the window → merged, with the reason recorded
5. **info** — a VM start or a scheduled stop never opens a case; it is kept as context
6. **otherwise** — a new case

Only genuinely uncertain signals (an unknown service, or one related but late) reach the Correlation
Agent, and even then it may only merge into an open case or open a new one. It cannot invent.

**The exception that encodes real operational knowledge.** A *local* symptom — CPU, memory, disk —
on a service *downstream* of the case is **not** merged through the dependency edge. An upstream
outage explains downstream *errors*; it does not explain downstream *CPU*. Without this rule, one
outage swallows every unrelated alert that happens nearby.

**Every placement records its reason.** When someone asks why two alerts became one incident, the
answer is a stored sentence, not a guess.

### What was built

| File | What it does |
|---|---|
| `copilot/correlation.py` (442 lines) | `Case`, the `Correlator`, the six rules, and the agent for uncertain signals |
| `config/services.toml` | The dependency graph and service tiers — who depends on whom, and what is customer-facing |

### How to show it

**`db_down`** — the database stops; the orders API and the storefront checkout fail with it. Three
services, three sets of alerts, **one** incident, rooted at the database.

**`lookalike_cpu`** — CPU high on two unrelated services at the same time. They stay **two**
incidents. This is the trap most systems fail.

---

## M3 · Persistent case memory + semantic index

**Spec:** per-service incident history, plus a vector index over runbooks and past postmortems.

### The idea

**Recurring incidents should get faster, not repeat the same investigation.** Memory is what makes
the second occurrence cheaper than the first.

**A case is identified by its signals, not by a generated id.** Saving a case whose signals are
already stored updates that case rather than adding a new one. This is what lets the copilot restart,
or replay a day of history, without duplicating anything.

**Stable chunk ids.** Knowledge-base documents are cut into chunks at their headings, and a chunk's
id is `<path>#<heading-slug>`. That id survives a rebuild of the index, which matters because
citations point at it — a diagnosis written last week still cites something that resolves today.

**Only changed chunks are re-embedded.** Rebuilding the index is cheap, so it can happen whenever a
runbook changes.

### What was built

| File | What it does |
|---|---|
| `copilot/memory.py` (197 lines) | SQLite: `cases`, `case_signals`, `case_events`. Lookups: `history(service)`, `similar_cases(case)`, `outcome_stats(action, service)`, `rejections(action, service)` |
| `copilot/kb/index.py` (189 lines) | Chunking at headings, stable ids, incremental embedding, stored in the same database |

### How to show it

Run a scenario twice. The second run recognises it — same case id, "seen N times before". Live on
28 Sep a recurring guest-agent error was recognised from its second occurrence, at similarity 1.0.

---

## M4 · RAG + groundedness evaluation

**Spec:** hybrid search and reranking over runbooks and postmortems; groundedness evaluation so a
root cause or a step is never fabricated.

### The idea

This is the milestone that decides whether the system can be trusted, and the concept behind it is
worth stating plainly: **the copilot has no knowledge of its own.** It quotes your documents. If it
cannot find a document that explains the incident, it says so and escalates.

**Hybrid retrieval, because meaning and exactness are different problems.** Keyword search (BM25)
finds error strings, acronyms and action names. Vector search finds paraphrases — "the machine was
powered off by a colleague" matching a runbook about a stopped VM. Reciprocal-rank fusion combines
them. Measured on 33 questions: keyword recall@5 0.848, vector 0.909, **hybrid 0.939**.

**Decoys in the evaluation set.** Three runbooks (GKE OOMKilled, Cloud SQL failover, TLS expiry)
describe technologies the company does not run. They exist so the eval proves the system can *avoid*
plausible-but-wrong documents, not merely find relevant ones.

**Five groundedness rules**, applied to the root cause and to every step:

1. it cites at least one section
2. every cited section was actually retrieved for *this* case — no invented or outside citations
3. the step's action is a registered action type
4. the action appears in a cited section — the runbook must literally contain `` `vm.start` ``
5. an LLM judge confirms the cited text supports the claim for this incident

Rules 1–4 are code and catch structural fabrication. Rule 5 catches the subtler case: a real step
from a real runbook, applied to the wrong situation. Anything that fails is removed and listed with
its reason. **With no grounded root cause, the result is "insufficient knowledge: escalate."**

Measured: 9 of 9 planted fabrications caught, 0 false removals. Rules alone caught the 6 structural
ones; the judge caught the 3 semantic ones.

### What was built

| File | What it does |
|---|---|
| `copilot/diagnosis.py` (320 lines) | Retrieval, the draft prompt, `check_groundedness`, and the per-service evidence breakdown |
| `copilot/kb/evaluate.py` | The retrieval and groundedness eval harness |
| `docs/kb/` | 12 runbooks (3 decoys) and 6 postmortems |
| `tests/kb_eval/` | 33 retrieval questions, 8 paraphrased; 12 groundedness cases, 9 planted fabrications |

### How to show it

**`novel`** — an unknown service, `session-cache`, starts failing. Nothing in the knowledge base
covers it. The system escalates rather than inventing a fix. This is the single most important
behaviour in the project: **an honest "I don't know" beats a confident wrong answer.**

---

## M5 · Ordered workflow with a durable approval checkpoint

**Spec:** intake → correlate → diagnose → recommend → conditional routing, with a durable checkpoint
at the human-approval interrupt, so an incident can sit "awaiting approval" indefinitely without
losing state.

### The idea

**Durable execution.** A human approval can take minutes or hours. If the process restarts in the
meantime — a deploy, a crash, a laptop closing — the incident must not be lost or restarted from
scratch. LangGraph's `interrupt()` pauses the graph and `SqliteSaver` checkpoints every transition,
so `copilot approve <case>` in a *different process tomorrow* resumes at the node after the pause.

**Blast radius is a property of the action and the service, not of the model's confidence.** It is
computed as action scope × service tier: restarting a process is a smaller act than rolling back a
deployment, and doing either to a customer-facing service is bigger than to an internal one.

**Four lanes, decided in code:**

```
no grounded cause                        → escalate
confidence below escalate threshold      → escalate
target unclear                           → escalate
blast radius critical                    → escalate
action is "none"                         → none
blast radius above the auto limit        → approval
confidence below the auto limit          → approval
otherwise                                → auto
```

Routing is code, not a prompt, **so the guardrail cannot be talked around.** Thresholds live in the
client config, so a cautious client and a permissive one run the same code.

**Confidence is adjusted by experience.** Past resolutions raise it; past failures and past
rejections lower it. That is the learning loop, and it needs no retraining — just counting.

**Settling.** A case waits for a quiet period before its workflow starts. This is why an alert storm
produces one workflow instead of one per signal.

### What was built

| File | What it does |
|---|---|
| `copilot/workflow.py` (300 lines) | The LangGraph state machine, `interrupt()` at approval, SQLite checkpointing per case |
| `copilot/routing.py` (129 lines) | Blast radius, confidence adjustment, the four lanes |
| `copilot/outbox.py` | A ticket per case with a timeline, plus notifications |
| `copilot/engine.py` | The main loop, settling, and incident lifecycle |

### How to show it

Any approval-lane scenario pauses with an approval card. Kill the process, start it again, approve —
it continues from the pause, not from the beginning.

---

## M6 · Specialised agents, then the full team

**Spec:** an Investigator and a Recommender passing structured information; then the six-agent team —
Correlation, Diagnosis/RAG, Remediation Recommendation, Approval Orchestrator, Action Execution and
Safety Reviewer — with an action allowlist.

### The idea

**Separation of concerns, with exactly one component holding execution authority.** Several agents
observe, reason and recommend. Only the execution agent changes anything, and only through the
allowlist.

**The allowlist is the security boundary.** Eleven registered actions, each with a parameter schema, a
blast-radius rule, a rollback and a verification. The executor will never send anything else. This is
what makes prompt injection survivable: an attacker who can write a log line can address the model,
but cannot invent an action.

**The Safety Reviewer is code, not a prompt** — the file says so: *"nothing the LLM writes can argue
past it."* It runs immediately before execution, **every time, including after a human approved**, and
checks:

- the action is on the allowlist
- the diagnosis is grounded
- the action is one of the grounded steps (not a fabricated one)
- required parameters are present and within bounds
- the target is part of this incident
- a data-holding service is not being interrupted
- the lane actually permits execution
- the circuit breaker has not tripped

**Being right about the service does not make the action safe.** A database can be correctly
identified as the problem and still must not be restarted, because it holds data.

### What was built

| File | Role |
|---|---|
| `copilot/agents/investigator.py` | Evidence + history + past rejections + grounded diagnosis |
| `copilot/agents/recommender.py` | Action, target, blast radius, confidence, rollback. Code first; the LLM only when the target is genuinely ambiguous, and then only from the case's own resources |
| `copilot/agents/remediation.py` (182) | Executes allowlisted actions |
| `copilot/agents/safety.py` | The guardrail, as code |
| `copilot/agents/verify.py` | Post-action signal state; rolls back or escalates |
| `copilot/actions.py` | The allowlist — 11 actions with schemas |

### How to show it

**`disk_full`** — the scripted approver is a rubber stamp, approving anything. It approves a wrong
fix, and the Safety Reviewer refuses to execute it anyway. **This is the demo that matters most: the
system protects against a careless human, not only a careless model.**

---

## M7 · Logging, retries and hardening

**Spec:** a log line for every step; failed calls retried before giving up cleanly; later tracing,
fallback and circuit breakers.

### The idea

**Nothing disappears silently.** Every step of every case goes into a trace keyed by case id, linked
from the ticket. Every external call — Gemini, embeddings, the control API — is retried with backoff,
and each retry is recorded. After the last attempt the error is raised and becomes an escalation:
never a hang, never a silent drop.

**The circuit breaker stops automation loops.** At most 3 attempts per action+target per hour. This
is what prevents the worst failure mode of an automated remediation system: retrying a fix that is
making things worse.

**Budget as a first-class concern.** Every LLM call is counted and budgeted, because the design rule
is that Gemini is used only for new, unclear or previously-failed cases. Repeats reuse a checked
diagnosis for zero calls.

**Fault injection for demos.** `COPILOT_FAULTS="llm=2,cloud=1"` makes the next calls fail on purpose,
so retry and escalation behaviour can be shown rather than described.

### What was built

| File | What it does |
|---|---|
| `copilot/trace.py` | One structured line per step, per case |
| `copilot/resilience.py` | Retries with backoff; fault injection |
| `copilot/agents/safety.py` | The circuit breaker |
| `copilot/usage.py` | LLM call counting and budget |

### How to show it

**`flapping`** — a fix works, then the problem returns. The first attempt runs automatically; the
next goes to a person. The system notices its own fix did not hold.

---

## M8 · Evaluation harness

**Spec:** 15–20 example incidents, some safe to auto-fix and some clearly risky, plus tricky cases —
two similar alerts that are unrelated, and a fix that looks reasonable but is unsafe. Check the system
reacts correctly every time.

### The idea

**You cannot stage these incidents in production.** You cannot stop a production database on a
Tuesday to see what the copilot does. So the whole company is simulated: four hosts, a real dependency
chain, and OTLP telemetry every simulated minute. The copilot cannot tell the difference, because it
receives the same format a real cloud would send.

**Ground truth per scenario.** Each defines the incidents a correct copilot opens, the acceptable
actions, lanes and end states, and a `forbid_sent` list of actions that must never reach the shop. A
scripted approver approves only acceptable actions, which makes the override rate measurable. One
scenario uses a *rubber-stamp* approver that approves anything — to prove the guardrails hold without
a careful human.

**The four traps from the spec**, each a scenario:

| Trap | Scenario | What it proves |
|---|---|---|
| look-alike | `lookalike_cpu` | Two similar alerts on unrelated services stay two incidents |
| cascade | `db_down` | Three services, one incident, fixed at the source |
| unsafe fix | `disk_full` | A plausible fix is blocked despite approval |
| flapping | `flapping` | A fix that does not hold is not retried forever |
| self-healing | `blip` | A problem that fixes itself gets no action |

**All six spec metrics are computed:** mean time to correlate, mean time to recommend, auto-remediation
rate within the safe envelope, false-correlation rate, approval-to-execution latency, approver override
rate, and MTTR.

### What was built

| File | What it does |
|---|---|
| `copilot/harness.py` (367 lines) | Every scenario end to end, scripted approver, the metrics report |
| `sim/world.py` (244) | The Acme Shop: hosts, dependencies, and its telemetry each simulated minute |
| `sim/scenarios.py` | What goes wrong and when |
| `tests/m8_eval/ground_truth.json` | 15 incidents across 12 use cases and every trap |

### How to show it

Run every scenario in the UI; each is scored against its expected outcome.

**Open:** one checkbox that is not code — the MTTR baseline has to be agreed with the team before the
comparison means anything.

---

## Beyond the spec — four findings from testing

All eight milestones were already implemented when this work started. These came from asking a
question the existing scenarios did not: **every one of them is "something broke" — what happens when
nothing is broken and something is full?**

That difference matters because the root-cause rule — "the most upstream service in the case" — is
correct for an outage and wrong for saturation. A service that runs out of workers piles requests
against a dependency that is busy but perfectly healthy, and the graph blames the healthy one.

Six new scenarios were written for that shape (`tests/capacity_eval/`), and they found two real bugs.

### 1 · A data-holding service could be restarted

`copilot/agents/safety.py` — the guard blocked `logs.rotate` and `vm.reset` on a service marked
`data = true`, but not `service.restart`. A database saturated on connection slots was diagnosed
*correctly* and then restarted, dropping every in-flight transaction.

`vm.start` was deliberately **not** added: it starts something already stopped, so there is nothing in
flight to lose, and it is the right fix when a database VM is down.

### 2 · The dependency graph beat the evidence

`copilot/correlation.py` — root cause now prefers a service reporting **its own** resource exhaustion
over its most-upstream dependency, and only when exactly one service does. Two would be ambiguous, and
ambiguity belongs with a person rather than a tie-break rule.

The hard part was telling *"I am full"* from *"I am quoting someone else's failure"*: the storefront
logs `checkout failed: orders-api 503: no worker available`, which contains a saturation phrase and is
not saturated at all.

**Honest limitation:** this matches on log *text*, because saturation has no metric. The telemetry
carries CPU, disk and process state and nothing for pool, worker or queue depth. When those metrics
exist this should read them instead — the text matching is a stand-in, not the design.

### 3 · Per-service evidence for the diagnosis

`copilot/diagnosis.py` — the model used to see one flat list of signals. It now sees each service
separately, with its own measurements and whether its errors are its own or relayed from elsewhere.
Cause versus casualty, stated explicitly rather than inferred.

### 4 · Rejections now change the next recommendation

`copilot/memory.py`, `routing.py`, `investigator.py` — a rejection was written and never read back:
the workflow records it as `kind='approval'` and `outcome_stats()` filters `kind='outcome'`. So
confidence never moved when a person said no, and the same wrong fix returned unchanged.

```
0 rejections → confidence 0.9 → auto        runs by itself
1 rejection  → confidence 0.7 → approval    asks a person
3 rejections → confidence 0.3 → escalate    stops offering it
```

The reasons also reach the model, so it can see what was refused and why. **The text is untrusted
input** — it informs a diagnosis and never touches the allowlist, the risk tiers or the safety
reviewer. A test fails if anyone wires it in.

---

## What is not covered

Stated plainly, because a review should hear it from us first.

**The model's own reasoning has never been exercised.** Every test uses a *scripted* diagnosis. That
tests correlation, routing, the guardrails, execution and verification — the plumbing — but the
diagnosis step was written by hand each time. Running the real model needs a Gemini key:
`run_live_capacity.py` is ready for it. This is the largest open item.

**Nothing has run against real infrastructure.** Everything is the simulator. Real alert payloads are
messier than generated ones, and the first contact with a real cloud will find parser gaps. That is a
tool-layer problem, not an agent problem, which is why the design keeps cloud specifics out of the
copilot.

**Saturation has no metric**, as described above.

**`similar_cases()` has no evaluation.** The knowledge base is measured — recall, MRR, groundedness.
Case retrieval is not, so its quality is unknown.

---

## Running the demo

```bash
cd Agentic_Workflow
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m ui.server          # http://127.0.0.1:8777
```

No API key and no cloud account. The **Milestones & evidence** tab maps each milestone to its files
and tests, with the counts collected by running pytest rather than written by hand. The **Run a
scenario** tab stages any of the 21 scenarios.

Three scenarios tell the whole story in about two minutes:

1. **`db_down`** — three services fail, the copilot finds one incident and the real cause
2. **`disk_full`** — a human approves a wrong fix and the guardrail refuses it anyway
3. **`novel`** — nothing in the knowledge base covers it, so it escalates instead of guessing

**203 tests pass.**
