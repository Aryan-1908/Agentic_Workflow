# Use cases

**Milestones** (docs/MILESTONES.md) build the platform: intake, correlation, memory, RAG, workflow, agents,
hardening, evaluation. **Use cases** are the kinds of problems that platform handles. They're independent of the
milestones: new ones can be added at any time, and each one runs on the whole platform.

## What a use case is
| part | example (VM stopped unexpectedly) | used by |
|---|---|---|
| signals that reveal it | VM audit event "stopped" by a user, uptime alert failing | intake (M1) |
| how signals group | the VM's service plus anything that depends on it | correlation (M2), service map |
| knowledge to diagnose it | runbook "VM stopped", past postmortems | RAG (M3–M4) |
| the right action | `vm.start` | action allowlist (M6) |
| risk level → lane | tier 3 → auto; tier 1 → approval | routing (M5) |
| how we check it | ground truth for grouping, cause, action, lane, final state | evals (M8) |

**Adding a use case** = a runbook in `docs/kb/`, an entry in the service map if a new resource is involved, an
action type if none fits, and an eval case. No platform change unless it needs a new kind of signal or action.

**How it runs:** every use case is a simulator scenario (`python -m sim run <scenario>`) that emits it as OpenTelemetry
data through the Collector. The last column names the scenario.

## Catalogue (draft; grows over time)
| # | use case | category | signals | action | lane | scenario |
|---|---|---|---|---|---|---|
| U1 | VM stopped unexpectedly | compute | audit event, uptime alert | `vm.start` | by tier | `vm_stopped` |
| U2 | VM crashed / host error / preempted | compute | system event | `vm.start` or escalate | by tier | - |
| U3 | app process / container stopped on a VM | application | uptime alert, error logs | `service.restart` | auto | `process_crash` |
| U4 | insufficient resources (CPU/memory saturation) | capacity | CPU alert, anomaly | `vm.resize` / `mig.resize` | approval | `cpu_runaway`, `lookalike_cpu` |
| U5 | disk filling up | capacity | disk alert, error logs | `logs.rotate`; escalate if data disk | auto / escalate | `disk_full` |
| U6 | app can't connect to the database | dependency | uptime/health alert, DB VM event, error logs | fix the DB side (`vm.start`), not the app | approval | `db_down` |
| U7 | bad release | change | errors right after a deploy/template change | `mig.rollback` | approval | `bad_release` |
| U8 | firewall rule removed / network blocked | network | uptime alert, audit event | `firewall.restore` (narrow) | approval | `firewall_blocked` |
| U9 | external provider outage | third party | status page, checkout errors | escalate (not ours to fix) | escalate | `payfast_outage` |
| U10 | recurring agent / system errors on a VM | noise | error log bursts | none; collapse and report | none | `guest_agent_noise` |
| U11 | scheduled start/stop, manual starts | expected change | info-level audit events | none: kept as context, attached to a related case if one opens | none | `scheduled_stop` |
| U12 | unknown problem, no runbook | anything | any | escalate (low confidence) | escalate | `novel` |
| U13 | GKE: CrashLoopBackOff, pods Pending, bad image | kubernetes | GKE events, alerts | rollback / scale / escalate | approval | - (needs a GKE model in the simulator) |

### Tricky variants (the spec's eval traps), applied to the use cases above
- **look-alike alerts:** the same alert on two unrelated services at once (U4 on two VMs: scenario `lookalike_cpu`) must stay two cases;
- **cascade:** U6 shows up as alerts on three services, which must become one case fixed at the DB;
- **unsafe fix:** for U8, "open all ports" looks like a fix and must be blocked;
- **flapping:** U3 comes back after a restart, and the circuit breaker must stop retrying and escalate (scenario `flapping`);
- **alert storm:** U1 reported from 3 regions plus repeats must become one case and one action;
- **self-healing blip:** closes by itself, so no action.

### Parked (not in the spec's incident scope; decided from SPEC.md, 28 Sep)
- cost: "find unused resources and reduce cost", "cost suddenly increased, investigate". The same platform could
  handle them (the signals would be inventory and billing), but they aren't incidents.
