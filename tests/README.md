# Test cases

Run everything offline (no cloud, no LLM key): `.venv/bin/python -m pytest -q`

| layer | what | where | needs |
|---|---|---|---|
| offline tests | parsers, rules, agents with a scripted LLM, every simulator scenario through the OTel reader | `tests/` | nothing |
| through the Collector | simulated incidents as real OTLP via the OTel Collector (docs/LIVE_TEST.md) | `python -m sim` + `python -m copilot watch` | the Collector binary; Gemini for diagnosis |
| evals (M8) | use-case instances from the simulator, scored | M8 | same |

A milestone's "done when" box is ticked only when its checks pass.

## M1 · OTel input (`test_otel.py`, `test_intake.py`)
| test | proves |
|---|---|
| every scenario, read from OTLP | no simulator output is rejected; each becomes the expected incidents (13 scenarios) |
| unique ids per run | two runs of a scenario never share signal ids (seen live 29 Sep) |
| alerts open and close | a stopped VM's alert opens, and closes (same alert id) after the VM starts |
| reader | returns only new data; a line the Collector is still writing is left for the next read |
| unknown `event.name` | Rejected with the reason, not dropped |
| log severity | follows OTel SeverityNumber: INFO → no signal, ERROR → error, FATAL → critical |
| anomaly payload / malformed anomaly | parsed / Rejected with the missing field |
| free text without / with an LLM | rejected, not dropped / fields extracted and validated |
| LLM inventing a service or resource | discarded: only names that appear in the text are kept |

## M2 · correlation (`test_correlation.py`, `correlation_cases/*.json`, `test_anomaly.py`, `test_otel.py`)
Each `correlation_cases/*.json` file is one incident pattern: signals in arrival order and the expected grouping.

| case | pattern | proves (M2 "done when" / catalogue #) |
|---|---|---|
| c01 | same alert delivered twice; status page polled 3× | duplicates collapse |
| c02 | outage re-fires from several regions + CPU on the same VM | one signal per condition · #17 |
| c03 | db down → orders-api → storefront checkout | cascade = one case, root orders-db · #14 |
| c04 | same cascade, symptoms before cause | order doesn't matter |
| c05 | CPU high on web-01 and batch-01 in the same minute | look-alikes stay two cases · #13 |
| c06 | storefront and reports fail separately, then their shared db | a shared upstream cause joins cases |
| c07 | payments status page degrades, then checkout errors | external outage joins dependents · #11 |
| c08 | same service, 3 hours apart | time window |
| c09 | related services, 25 min apart | uncertain → kept apart, flagged for review |
| c10 | signal with no known service | never merged by rules, flagged |
| c11 | alert opens then closes | closing updates the case, not a new one |
| c12 | unrelated reports disk + payments blip | no false correlation |
| c13 | the db cascade **and** the CPU trap at once | an upstream outage doesn't swallow downstream CPU |
| c14 | downstream errors, then db disk 97% | upstream local symptoms still correlate |
| c15 | VM start then errors; later a scheduled stop | info events open no incident; they're context for a related case |
| c16 | open db incident, then the db VM is started | an info event joins the open case |

Scenarios through OTel (`test_otel.py`): `db_down` → one case rooted at orders-db; `lookalike_cpu` → two cases;
`payfast_outage` → payments-provider + storefront; info events (rollout, scheduled stop) attach as context; the
dependency graph is learned from traces; the anomaly detector sees `cpu_runaway`.

Plus: time-to-correlate recorded per case; root service is the upstream one; the Correlation Agent (scripted LLM) can
merge with a reason, **cannot** merge into a case that doesn't exist, and can keep a signal separate. Anomaly detector:
sustained spike and drop detected; single blip, mixed directions, short history and flat-line noise ignored.

These tests were checked against deliberate breakage: ignoring the dependency graph fails 8 tests, and merging
everything fails 6, so the suite does catch wrong correlation.

**Add a case:** copy a file in `correlation_cases/`, change the signals and `expect`. Fields:
`signals[]`: id, minute, service, title, resource, metric, severity, source, type, [signal_id], [state].
`expect`: groups, roots, needs_review, all_clear, unique_signals, duplicates, repeats.

## M3 · memory + index (`test_memory.py`)
| test | proves |
|---|---|
| survive a restart | cases and outcomes are there in a new process |
| replay the same history | no duplicates; the stored case id stays the same |
| repeated incident | the past one of the same kind is the top match; same message on another service, or another kind, isn't |
| signature | info context (VM start) doesn't count toward similarity |
| history / outcome stats | per service, newest first; outcomes counted per action and service |
| incident in progress across a restart | the next signal joins the restored case |
| index | stable chunk ids; only changed sections re-embedded; deleted docs leave; search finds the right runbook |

Checked against deliberate breakage: no de-duplication, info in the signature, and ignoring the service each fail tests.

## M4 · search, groundedness, diagnosis (`test_diagnosis.py`, `kb_eval/*.json`)
| test | proves |
|---|---|
| keyword search | exact error strings find the right runbook |
| keyword baseline | recall@5 ≥ 0.84 on the eval set, and it misses only paraphrased questions |
| hybrid | fuses keyword and vector rankings |
| reranker | its order is applied; ids it invents are ignored |
| 12 groundedness cases | every planted fabrication removed, the 3 clean diagnoses untouched (semantic cases with a scripted judge) |
| no grounded root cause | "insufficient knowledge: escalate", confidence capped |
| pipeline | retrieves whole matching runbooks, uses similar past cases, removes a made-up citation before anything is shown |

Found while testing: section-level search matched only a runbook's Symptoms, never its Cause or Remediation, so
diagnosis now reads each matching document whole.

## M5 · workflow (`test_workflow.py`)
| test | proves |
|---|---|
| 8 lane cases | auto, elevated blast → approval, ambiguous → approval, low confidence / critical / runbook says so / ungrounded → escalate, no action → none |
| blocked actions | a client can require a person for an action whatever its score |
| blast radius | grows with service tier; unknown service +1; unregistered action = critical |
| none lane | closes with ticket, team notification, memory events |
| auto lane | recorded as not executed in an observe-only project |
| approval | pauses, survives a new process, approve continues; who/when/why in memory and ticket |
| reject | needs a reason; approve needs a name; closes without executing; can't decide twice |
| escalate | ungrounded diagnosis → on-call notified with the reason |
| idempotent | replaying history doesn't re-run a started workflow |
| action target | the resource the step names, else the root service's; unclear → escalate, never a guess (seen live 29 Sep) |
| interrupted | a workflow stopped part-way (Ctrl+C, failed LLM call) resumes from its last step on the next start (seen live 28 Sep) |

Checked against deliberate breakage: removing the pause, or keeping state only in memory, each fail tests.

## M6 step 0 · cost controls (`test_cost.py`)
| test | proves |
|---|---|
| known incident | a repeat reuses the stored, checked diagnosis: the LLM is not called |
| after a rejection | the LLM is called again (something is different) |
| counting + budget | every chat call is counted; the call over budget is refused (and not counted) |
| embeddings | counted, not budgeted |
| out of budget | the case gets an ungrounded "budget used up" diagnosis and routes to escalate |

## M6 step 1 · Investigator and Recommender (`test_agents.py`)
| test | proves |
|---|---|
| Investigator, new incident | drafts with the LLM, gives the evidence and similar-case history |
| Investigator, known incident | reuses the checked diagnosis: no LLM call |
| Recommender, clear target | no LLM call |
| Recommender, stuck | asks the LLM, which chooses among the case's own resources; a person must confirm (never auto) |
| LLM answers outside the case | ignored (3 variants): no target → escalate |
| none / escalate | no LLM call |
| handoff | the workflow passes the Investigator's structured Investigation to the Recommender |

## M6 step 2 · execution (`test_execution.py`)
The simulator runs in-process; each verification check advances it one minute and applies the actions sent to it.
| test | proves |
|---|---|
| auto restart of a runaway process | executed, the simulated shop changed, verified resolved |
| approved vm.start on db_down | the whole cascade recovers; alerts closed |
| 7 Safety Reviewer blocks | no approval, auto above limit, not allowlisted, data service, VM outside the incident, out of bounds, missing parameter; nothing sent |
| step not in the grounded diagnosis | blocked (fabricated step) |
| circuit breaker | blocked after 3 attempts in an hour |
| Compute / Service preconditions | no evidence the VM is stopped; restart on a stopped VM |
| wrong fix / environment refusal / no confirmation | failed, failed, not_applied → escalate |
| silence while an alert is open | not recovered (no requests means no errors, but the alert is still open) |
| verification uses the latest case | all alerts open now are checked, not the snapshot from the start (seen live) |
| ambiguous recovery | Gemini asked; "wait" then resolved; no usable LLM → escalate; clear results never ask it |
| workflow | approve → execute → verified close; outcome recorded for future confidence |

Checked against deliberate breakage: verification ignoring alerts, no approval check, the simulator ignoring vm.start,
and verifying the old snapshot each fail tests.

## M7 · trace, retries, lifecycle (`test_resilience.py`, `test_lifecycle.py`)
| test | proves |
|---|---|
| fails twice, then works | 3rd attempt succeeds; both retries in the trace |
| fails every time | raised after 3 attempts, traced as error |
| budget used up | not retried |
| investigation keeps failing | escalated, error in the on-call notification, retries in the trace |
| send fails once / always | retried and verified / failed after 3 attempts → escalated, nothing sent |
| every step in the trace | start → investigate → recommend → route → approval waiting → approval → execute → close, with timings; ticket links the trace |
| flapping, whole engine | 3 verified fixes, each a new incident; 4th blocked by the circuit breaker → on-call; 3 sends |
| resolved incident | closes; late signals don't reopen it; memory marks it finished |

Checked against deliberate breakage: no retries, no step tracing, an engine that never closes incidents, and no circuit
breaker each fail tests. All tests write traces and usage to a temporary folder (tests/conftest.py).

## M8 · evaluation harness (`test_m8.py`, ground truth in `m8_eval/ground_truth.json`)
The harness (`copilot/harness.py`) runs each scenario through the whole copilot against a fresh simulated shop, with a
scripted approver. These offline tests script the diagnoses, so they check the harness and its scoring; the live run is
`python -m copilot eval` (real Gemini agents).

| test | proves |
|---|---|
| ground truth | every scenario has one; 15-20 incidents; all five spec traps present |
| auto-lane incident | fixed automatically, scored right, times measured, no correlation errors |
| cascade | one incident with root orders-db, approved, fixed; approval-to-send latency measured |
| look-alike with a wrong fix | two incidents; the approver rejects the restart of web-01 (an override); nothing forbidden sent |
| unsafe fix, rubber-stamped | the approver approves logs.rotate on the database VM; the Safety Reviewer still blocks it |
| self-healing blip | recovered before the approved action ran: closed "recovered by itself", nothing sent |
| healthy shop | no incident, false-correlation rate 0 |
| flapping | seen first by the anomaly detector, then by the alert: still recognised as the same fix not holding, so the retry goes to a person |
| report | JSON and Markdown written with the spec's metrics |

## After M8 · console and Ask (`test_console.py`)
| test | proves |
|---|---|
| answer with valid citations | passed on, with its sources; the prompt marks the sources as data, not instructions |
| made-up source / no source / model says it doesn't know | "I don't know", no citations |
| question names an incident or a service | that incident is given as a source |
| no model, empty question | no call made |
| incidents and detail | the waiting card, diagnosis, timeline, signals and notifications are shown |
| approve from the console | the same audit trail as `copilot approve`; no name → refused |
| Ask sees live incidents | the recommendation of a waiting incident is in the sources |
| page and API | served; no `innerHTML` in the page (data shown as text only) |
| requests from elsewhere | another Host name, another Origin, a form post: all refused (403); the case is still waiting |
