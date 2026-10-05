# Try it yourself

The copilot's only input is **OpenTelemetry**. For the POC, the data comes from the simulator: a realistic Acme Shop
(storefront → orders-api → orders-db PostgreSQL, plus an external payment provider) that emits OTel logs, metrics
and traces. No cloud account is involved.

```
python -m sim ──OTLP──▶ OTel Collector ──▶ runs/otel/telemetry.jsonl ──▶ python -m copilot watch
```

All commands run from the repo root.

## One-time setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                       # all tests pass
echo 'GOOGLE_API_KEY=...' > .env                    # Gemini key, read automatically; .env is git-ignored
.venv/bin/python -m copilot kb index                # knowledge-base index (once, and after editing docs/kb/)
```
The Collector is already in `tools/otelcol-contrib` (v0.161.0). If it's missing:
see "Getting the Collector" at the end.

## 1. Start the Collector (terminal 1, leave it running)
```bash
tools/otelcol-contrib --config otel/collector.yaml
```
Optional, to **see** the data: start Jaeger first (terminal 0) and open **http://localhost:16686**
```bash
tools/jaeger --config otel/jaeger.yaml
```
The Collector sends it a copy of every trace. In the UI pick service `storefront`, click *Find Traces*, and open a red
(failed) one: you see the request go storefront → orders-api → orders-db / payfast, and where it failed. Jaeger keeps
traces in memory (gone when it stops) and shows traces only; logs and metrics stay in the copilot's file.

## 2. Send an incident (terminal 2)
```bash
.venv/bin/python -m sim list                        # the scenarios, one per use case
.venv/bin/python -m sim run db_down                 # 20 healthy minutes + the incident, instantly
```
The telemetry lands in `runs/otel/telemetry.jsonl`.

## 3. Run the copilot on it
```bash
.venv/bin/python -m copilot watch --once            # read everything, then stop
```
For each incident you see the signals, the case they join (with the reason), "seen before" from memory, and the
workflow: diagnosis with citations (M4), recommendation and lane (M5), close or wait for approval.

**What to expect per scenario** (the offline tests check the grouping of every one):

| scenario | use case | expected |
|---|---|---|
| `db_down` | U6 cascade | ONE case: orders-db, orders-api, storefront; root orders-db |
| `vm_stopped` | U1 | one storefront case |
| `process_crash` | U3 | one storefront case (VM is running, the process isn't) |
| `cpu_runaway` | U4 | one reports-batch case, with a CPU anomaly |
| `lookalike_cpu` | U4 trap | TWO unrelated cases: storefront and reports-batch |
| `disk_full` | U5 | one case rooted at orders-db; data disk → escalate, never auto |
| `bad_release` | U7 | one storefront case; the rollout is attached as context |
| `firewall_blocked` | U8 | one storefront case; fix is a narrow `firewall.restore` |
| `payfast_outage` | U9 | one case: payments-provider + storefront; escalate (not ours to fix) |
| `guest_agent_noise` | U10 | one case, "no action needed" |
| `scheduled_stop` | U11 | one case (the monitoring alert still fires); the schedule is context, "no action" |
| `novel` | U12 | one case on an unknown service; nothing in the KB → escalate |
| `healthy` | - | nothing |

**Between scenarios, start clean.** Each `sim run` is a separate world: an incident left open by the previous run
(its alerts never resolve) would still look open to the next one. Stop the Collector (Ctrl+C), then:
```bash
.venv/bin/python -m copilot reset --yes             # keeps the knowledge base; --keep-memory also keeps past cases
```
Don't delete `runs/otel/telemetry.jsonl` while the Collector is running: it keeps writing to the deleted file.

**Gemini cost:** only new incidents call Gemini (about 5 calls each). A repeat of a known incident reuses the
checked diagnosis: 0 calls. `watch` prints the day's count; counts are in `runs/llm_usage.json`; the daily limit is
`COPILOT_LLM_DAILY_BUDGET` (default 200), after which new incidents are escalated without a Gemini diagnosis.

**Trace:** every incident's steps, timings, retries and errors are in `runs/trace/<case>.jsonl` (linked from its
ticket). To see failures handled: `COPILOT_FAULTS="llm=3" .venv/bin/python -m copilot watch` makes the next 3 Gemini calls
fail on purpose; the trace shows the retries, then a clean escalation with the error (kinds: llm, embeddings, cloud).

## 4. Live mode
```bash
.venv/bin/python -m copilot watch                   # terminal 3: follows the file
.venv/bin/python -m sim run vm_stopped --live --tick 5   # terminal 2: one simulated minute every 5 s
```
The incident appears in `watch` as it happens.

## 5. Approvals, tickets, memory
```bash
.venv/bin/python -m copilot cases [--waiting]
.venv/bin/python -m copilot approve <case> --by <your name> --reason "checked"
.venv/bin/python -m copilot reject  <case> --by <your name> --reason "not now"
.venv/bin/python -m copilot memory history storefront
```
Tickets: `runs/tickets/<case>.md`; notifications: `runs/notifications.jsonl`.

## 6. Execution against the simulator (M6)
Actions are applied to the simulated shop only while the simulator runs **live** (it reads `runs/sim/actions.jsonl`
every simulated minute, like a cloud API, and confirms each action through telemetry). Three terminals:
```bash
tools/otelcol-contrib --config otel/collector.yaml
.venv/bin/python -m sim run db_down --live --tick 3 --minutes 40
.venv/bin/python -m copilot watch
```
`watch` waits for the incident to settle (20 s without new signals), diagnoses, recommends and routes. For `db_down`
it waits for approval; then `copilot approve <case> --by <name>` makes the Compute agent send `vm.start db-01`, the
simulator applies it, and the copilot closes the case only after the alerts close in the telemetry. `cpu_runaway`
goes the auto lane: the Service agent restarts batch-01 with no human. The Safety Reviewer (code) blocks anything
outside the allowlist, without approval, above the blast limit, or on a data service.

## 7. The M8 evaluation (all scenarios, scored)
No Collector or simulator needed: the harness runs its own simulated shop per scenario, with a scripted approver.
```bash
.venv/bin/python -m copilot eval                          # all 15 scenarios, about 50 Gemini calls, ~6 min
.venv/bin/python -m copilot eval --only blip,db_down      # just some
```
The report is `runs/eval/m8-<time>.md`; each scenario's engine log, tickets and trace are in `runs/eval/m8-<time>/`.

## 8. The console (web page)
```bash
.venv/bin/python -m copilot console          # then open http://127.0.0.1:8765
```
Run it next to `watch` (section 3 or 6). It shows the incidents, approval cards (type your name, then Approve, or a
reason and Reject: the same as `copilot approve` / `reject`), each incident's diagnosis, evidence and step-by-step
trace, the latest signals, notifications and today's Gemini usage. "Ask the copilot" answers from the runbooks and
incidents with sources, or says "I don't know" (one Gemini call per question). Local only (127.0.0.1).

## Without the Collector
`python -m sim run db_down --file runs/otel/telemetry.jsonl` writes the same OTLP JSON directly. Handy for quick runs;
the Collector path is the real one.

## Getting Jaeger
```bash
V=2.21.0; cd tools
curl -fLO https://github.com/jaegertracing/jaeger/releases/download/v$V/jaeger-$V-linux-amd64.tar.gz
curl -fL https://github.com/jaegertracing/jaeger/releases/download/v$V/jaeger-$V-linux-amd64.sha256sum.txt -o sums.txt
tar xzf jaeger-$V-linux-amd64.tar.gz jaeger-$V-linux-amd64/jaeger
grep " \*jaeger-$V-linux-amd64/jaeger$" sums.txt | sed 's/ \*/  /' | sha256sum -c && mv jaeger-$V-linux-amd64/jaeger . && cd ..
```

## Getting the Collector
```bash
V=0.161.0; mkdir -p tools && cd tools
curl -fLO https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$V/otelcol-contrib_${V}_linux_amd64.tar.gz
curl -fL https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$V/otelcol-contrib_${V}_linux_amd64.tar.gz.sha256 | awk '{print $1"  otelcol-contrib_'$V'_linux_amd64.tar.gz"}' | sha256sum -c
tar xzf otelcol-contrib_${V}_linux_amd64.tar.gz otelcol-contrib && cd ..
```
