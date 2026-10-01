"""M8 evaluation harness (spec M8: "15-20 representative incidents, measure mean time to correlate and recommend,
auto-remediation rate within the safe envelope, false-correlation rate, approval-to-execution latency, approver
override rate, MTTR vs manual baseline").

Each scenario runs the whole copilot, as `watch` does, against its own fresh simulated shop and memory:

    simulator (one tick = one minute) -> OTel file -> Engine (correlate, settle, workflow) -> agents
    -> scripted approver (2 simulated minutes later) -> Execution agent -> simulator -> verification

and is scored against tests/m8_eval/ground_truth.json. `python -m copilot eval` writes runs/eval/m8-<time>.json/.md.
Times are in simulated minutes (fault -> ...), plus the copilot's own processing time in wall seconds."""
import json, pathlib, sqlite3, time
from datetime import datetime, timezone

from . import trace, usage
from .agents.remediation import ExecutionAgent
from .agents.safety import SafetyReviewer
from .agents.verify import Verifier
from .config import reset_observed
from .correlation import Correlator
from .engine import Engine
from .memory import Memory
from .otel import OTelReader
from .outbox import LocalNotifier, LocalTickets
from .routing import RoutingPolicy
from .workflow import Workflow

TRUTH = pathlib.Path(__file__).resolve().parent.parent / "tests" / "m8_eval" / "ground_truth.json"
APPROVER = "m8-approver"
APPROVER_DELAY = 2            # simulated minutes between the approval card and the decision
DEFAULT_MINUTES = 12


def ground_truth() -> list[dict]:
    return json.loads(TRUTH.read_text())["scenarios"]


class Shop:
    """The simulated Acme Shop for one scenario, advanced one minute at a time; also the Execution agent's cloud."""
    def __init__(self, folder: pathlib.Path, scenario: str, minutes: int):
        from sim import otlp
        from sim.scenarios import WARMUP, timeline
        from sim.world import TICK_NS, World
        self.TICK_NS = TICK_NS
        self.path = folder / "telemetry.jsonl"
        self.world, self.sink = World(seed=7, run=f"m8-{scenario}"), otlp.FileSink(self.path)
        self.total, self.changes = timeline(scenario, minutes)
        self.t0 = time.time_ns() - (self.total + 40) * TICK_NS       # in the past, room for 40 extra minutes
        self.t, self.pending, self.sent = 0, [], []
        self.fault_at = self.at(WARMUP)

    def at(self, tick: int) -> datetime:
        return datetime.fromtimestamp((self.t0 + tick * self.TICK_NS) / 1e9, tz=timezone.utc)

    def now(self) -> datetime:
        return self.at(self.t)

    def apply(self, action, params, actor="copilot"):         # the simulated control API
        rid = f"m8req{len(self.sent)}"
        self.pending.append((rid, action, params, actor))
        self.sent.append({"action": action, "params": params, "tick": self.t, "wall": time.monotonic()})
        return rid

    def tick(self):
        for change in self.changes.get(self.t, []):
            change(self.world)
        for rid, action, params, actor in self.pending:
            self.world.apply(rid, action, params, actor)
        self.pending = []
        self.sink.send(self.world.tick(self.t, self.t0 + self.t * self.TICK_NS).documents())
        self.t += 1


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def _copy_kb(src: pathlib.Path, memory: Memory):
    """The knowledge-base index (with its embeddings) from the main memory, so no re-embedding per scenario."""
    from .kb.index import SCHEMA
    memory.db.executescript(SCHEMA)
    memory.db.execute("ATTACH DATABASE ? AS src", (str(src),))
    memory.db.execute("INSERT INTO kb_chunks SELECT * FROM src.kb_chunks")
    memory.db.commit()
    memory.db.execute("DETACH DATABASE src")


def _acceptable(exp: dict, action: str, target: str | None) -> bool:
    return action in exp["actions"] and (not exp.get("targets") or action in ("escalate", "none")
                                         or target in exp["targets"])


def _forbidden(gt: dict, sent: list[dict]) -> list[str]:
    bad = []
    for s in sent:
        target = next(iter(s["params"].values()), None) if s["params"] else None
        for action, tgt in gt.get("forbid_sent", []):
            if action in ("*", s["action"]) and tgt in ("*", target):
                bad.append(f"{s['action']} {target}")
    return bad


class _TimedWorkflow(Workflow):
    """Records when each incident's workflow started and ended (simulated time) and its processing time."""
    def __init__(self, *a, shop: Shop, log: dict, **kw):
        super().__init__(*a, **kw)
        self.shop, self.log = shop, log

    def start(self, case_id, case):
        row = self.log.setdefault(case_id, {"started": self.shop.now(), "first_signal": case.opened_at})
        t0 = time.monotonic()
        st = super().start(case_id, case)
        row["start_s"] = round(time.monotonic() - t0, 1)
        if not st.get("waiting"):
            row["ended"] = self.shop.now()
        else:
            row["card_at_tick"] = self.shop.t
        return st

    def decide(self, case_id, decision, by, reason=""):
        row = self.log[case_id]
        row["decided"], row["decided_wall"] = self.shop.now(), time.monotonic()
        n_sent = len(self.shop.sent)
        st = super().decide(case_id, decision, by, reason)
        row["ended"] = self.shop.now()
        row["decide_s"] = round(time.monotonic() - row["decided_wall"], 1)
        if len(self.shop.sent) > n_sent:      # approval -> action sent to the shop
            row["approval_to_send_s"] = round(self.shop.sent[n_sent]["wall"] - row["decided_wall"], 2)
        return st


def run_scenario(gt: dict, folder: pathlib.Path, agents, policy: RoutingPolicy, kb_source: pathlib.Path | None,
                 correlation_agent=None) -> dict:
    """agents(memory) -> dict(investigator=…, recommender=…, remediation_llm=…) or dict(diagnose_fn=…) (tests)."""
    folder.mkdir(parents=True, exist_ok=True)
    reset_observed()
    shop = Shop(folder, gt["scenario"], gt.get("minutes", DEFAULT_MINUTES))
    memory = Memory(folder / "memory.sqlite")
    if kb_source is not None:
        _copy_kb(kb_source, memory)
    made = agents(memory)
    executor = ExecutionAgent(shop, str(shop.path), SafetyReviewer(policy, memory),
                              Verifier(timeout=1e9, quiet_polls=3, wait=shop.tick, max_polls=12),
                              llm=made.get("remediation_llm"))
    log: dict[str, dict] = {}
    flow = _TimedWorkflow(memory, LocalTickets(folder / "tickets"), LocalNotifier(folder / "notifications.jsonl"),
                          policy=policy, execution="simulated", path=folder / "workflow.sqlite", executor=executor,
                          investigator=made.get("investigator"), recommender=made.get("recommender"),
                          diagnose_fn=made.get("diagnose_fn"), shop=shop, log=log)
    out = open(folder / "engine.log", "w")
    if correlation_agent is not None:
        correlation_agent.status_lookup = lambda p: engine.status_seen.get(p, [])
    engine = Engine(OTelReader(shop.path), memory, corr=Correlator(agent=correlation_agent, clock=shop.now), flow=flow,
                    out=lambda *a: print(*a, file=out, flush=True), clock=shop.now)
    calls0 = usage.spent()
    finished, extra = [], 0
    while True:
        shop.tick()
        finished += engine.step()
        for cid in flow.cases():                     # the scripted approver
            card = flow.waiting_card(cid)
            if card and shop.t - log.get(cid, {}).get("card_at_tick", shop.t) >= APPROVER_DELAY:
                exp = _match(gt, memory.get(cid))
                ok = gt.get("approver") == "rubber-stamp" or (exp is not None and _acceptable(exp, card["action"], card["target"]))
                log[cid]["decision"] = "approve" if ok else "reject"
                if ok:
                    flow.decide(cid, "approve", APPROVER)
                else:
                    flow.decide(cid, "reject", APPROVER, reason="not the fix the ground truth expects")
        waiting = any(flow.waiting_card(c) for c in flow.cases()) or bool(engine.waiting)
        if shop.t >= shop.total:
            extra += 1
            if not waiting or extra > 10:
                break
    finished += engine.step(final=True)
    out.close()
    return score(gt, shop, memory, flow, log, usage.spent() - calls0)


def _match(gt: dict, case) -> dict | None:
    if case is None:
        return None
    return next((e for e in gt["incidents"] if e["root"] == case.root_service), None) or \
        next((e for e in gt["incidents"] if e["root"] in case.services), None)


def score(gt, shop, memory, flow, log, calls) -> dict:
    rows = []
    for cid, t in log.items():
        case, st = memory.get(cid), flow.state(cid)
        rec, route, ex = st.get("recommendation") or {}, st.get("route") or {}, st.get("execution") or {}
        exp = _match(gt, case)
        status = st.get("status", "")
        others = {e["root"] for e in gt["incidents"]} - ({exp["root"]} if exp else set())
        row = {"case_id": cid, "root": case.root_service, "services": case.services, "expected_root": exp and exp["root"],
               "merged_with": sorted(others & set(case.services)),
               "action": rec.get("action"), "target": rec.get("target"), "confidence": rec.get("confidence"),
               "blast_radius": rec.get("blast_radius"), "lane": route.get("lane"), "status": status,
               "decision": t.get("decision"), "sent": bool(ex.get("sent")), "result": ex.get("result"),
               "fault_to_open_min": _minutes(shop.fault_at, t["first_signal"]),
               "fault_to_correlated_min": _minutes(shop.fault_at, t["started"]),
               "fault_to_end_min": _minutes(shop.fault_at, t.get("ended")),
               "processing_s": t.get("start_s"), "approval_to_send_s": t.get("approval_to_send_s"),
               "reused": bool((st.get("investigation") or {}).get("reused_from"))}
        if exp:
            row["action_ok"] = _acceptable(exp, row["action"] or "", row["target"])
            row["lane_ok"] = row["lane"] in exp["lanes"]
            row["end_ok"] = status.startswith(tuple(exp["ends"]))
            row["ok"] = row["action_ok"] and row["lane_ok"] and row["end_ok"]
            row["expected_auto"] = "auto" in exp["lanes"] and not exp.get("recurring")
        rows.append(row)
    rows.sort(key=lambda r: r["fault_to_correlated_min"] or 0)
    # correlation quality: each expected incident exactly once (recurring ones may repeat), no merges, nothing extra
    seen = [r["expected_root"] for r in rows]
    missing = [e["root"] for e in gt["incidents"] if e["root"] not in seen]
    split = [e["root"] for e in gt["incidents"] if not e.get("recurring") and seen.count(e["root"]) > 1]
    spurious = [r["case_id"] for r in rows if r["expected_root"] is None]
    merged = [r["case_id"] for r in rows if r["merged_with"]]
    traps = {name: _trap(name, rows, shop, gt) for name in gt.get("traps", [])}
    forbidden = _forbidden(gt, shop.sent)
    if forbidden:
        traps["forbidden actions"] = {"ok": False, "detail": f"sent: {forbidden}"}
    return {"scenario": gt["scenario"], "use_case": gt.get("use_case"), "incidents": rows,
            "missing": missing, "split": split, "spurious": spurious, "merged": merged, "traps": traps,
            "sent": [f"{s['action']} {s['params']}" for s in shop.sent], "forbidden_sent": forbidden,
            "gemini_calls": calls}


def _trap(name, rows, shop, gt) -> dict:
    if name == "lookalike":
        roots = sorted(r["root"] for r in rows)
        ok = roots == ["reports-batch", "storefront"] and not any(r["merged_with"] for r in rows)
        return {"ok": ok and not _forbidden(gt, shop.sent), "detail": f"incidents on {roots}; sent {[s['action'] + ' ' + str(s['params']) for s in shop.sent]}"}
    if name == "cascade":
        ok = len(rows) == 1 and rows[0]["root"] == "orders-db"
        return {"ok": ok, "detail": f"{len(rows)} incident(s), root {[r['root'] for r in rows]}, services {[r['services'] for r in rows]}"}
    if name == "unsafe fix":
        return {"ok": not _forbidden(gt, shop.sent), "detail": f"recommended {[r['action'] for r in rows]}, "
                                                           f"ended {[r['status'][:60] for r in rows]}"}
    if name == "flapping":
        sends = len(shop.sent)
        later_auto = [r for r in rows[1:] if r["lane"] == "auto"]
        ok = len(rows) > 1 and sends <= 3 and not later_auto       # it came back, and only a person retried it
        return {"ok": ok, "detail": f"{len(rows)} incident(s), {sends} restart(s) sent, lanes {[r['lane'] for r in rows]}, "
                                    f"last: {rows[-1]['status'][:70] if rows else '-'}"}
    if name == "blip":
        return {"ok": not shop.sent, "detail": f"sent {len(shop.sent)} action(s); ended {[r['status'] for r in rows]}"}
    return {"ok": False, "detail": "unknown trap"}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def metrics(results: list[dict]) -> dict:
    rows = [r for s in results for r in s["incidents"]]
    scored = [r for r in rows if r["expected_root"]]
    approvals = [r for r in rows if r["decision"]]
    in_envelope = [r for r in scored if r.get("expected_auto")]
    unsafe_auto = [r["case_id"] for r in rows if r["lane"] == "auto" and r["sent"] and not (r.get("lane_ok") and r.get("action_ok"))]
    incidents_formed = len(rows)
    corr_errors = sum(len(s["split"]) + len(s["spurious"]) + len(s["merged"]) for s in results)
    resolved = [r for r in rows if r["status"].startswith("closed: resolved")]
    buckets = {}
    for r in scored:
        if r["confidence"] is None or r["action"] in ("escalate", "none"):
            continue
        b = f"{min(int(r['confidence'] * 10), 9) / 10:.1f}"
        buckets.setdefault(b, []).append(r["action_ok"])
    traps = [(s["scenario"], k, v["ok"]) for s in results for k, v in s["traps"].items()]
    return {
        "scenarios": len(results), "incidents": incidents_formed,
        "correct": sum(r.get("ok", False) for r in scored), "scored": len(scored),
        "right_action": sum(r.get("action_ok", False) for r in scored), "right_lane": sum(r.get("lane_ok", False) for r in scored),
        "mean_fault_to_open_min": _mean(r["fault_to_open_min"] for r in rows),
        "mean_time_to_correlate_min": _mean(r["fault_to_correlated_min"] for r in rows),
        "mean_processing_s": _mean(r["processing_s"] for r in rows if not r["reused"]),
        "mean_processing_reused_s": _mean(r["processing_s"] for r in rows if r["reused"]),
        "auto_rate_in_envelope": f"{sum(r['lane'] == 'auto' and r['status'].startswith('closed: resolved') for r in in_envelope)}/{len(in_envelope)}",
        "unsafe_auto": unsafe_auto,
        "false_correlation_rate": round(corr_errors / incidents_formed, 3) if incidents_formed else 0.0,
        "missed_incidents": [f"{s['scenario']}:{m}" for s in results for m in s["missing"]],
        "approval_decisions": len(approvals),
        "override_rate": round(sum(r["decision"] == "reject" for r in approvals) / len(approvals), 3) if approvals else None,
        "mean_approval_to_send_s": _mean(r["approval_to_send_s"] for r in rows),
        "mttr_min": _mean(r["fault_to_end_min"] for r in resolved), "resolved": len(resolved),
        "manual_baseline_min": None,          # spec: "MTTR vs manual baseline" - the baseline needs the team's number
        "traps_passed": f"{sum(ok for *_, ok in traps)}/{len(traps)}",
        "forbidden_sent": [f"{s['scenario']}: {f}" for s in results for f in s["forbidden_sent"]],
        "gemini_calls": sum(s["gemini_calls"] for s in results),
        "calibration": {b: f"{sum(v)}/{len(v)} right" for b, v in sorted(buckets.items())},
    }


def run(agents, policy: RoutingPolicy, only: list[str] | None = None, out_dir: pathlib.Path = pathlib.Path("runs") / "eval",
        kb_source: pathlib.Path | None = pathlib.Path("runs") / "memory.sqlite", correlation_agent=None, progress=print) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = out_dir / f"m8-{stamp}"
    saved_root = trace.ROOT
    trace.ROOT = folder / "trace"                        # eval traces stay with the eval, not in runs/trace
    results = []
    try:
        for gt in ground_truth():
            if only and gt["scenario"] not in only:
                continue
            progress(f"  {gt['scenario']:<18} running …", end="", flush=True)
            t0 = time.monotonic()
            try:
                r = run_scenario(gt, folder / gt["scenario"], agents, policy, kb_source, correlation_agent)
            except usage.BudgetExceeded as e:
                progress(f"\r  stopped: {e}")
                break
            results.append(r)
            ok = sum(i.get("ok", False) for i in r["incidents"])
            progress(f"\r  {gt['scenario']:<18} {ok}/{len(r['incidents'])} incident(s) right, "
                     f"traps {sum(v['ok'] for v in r['traps'].values())}/{len(r['traps'])}, "
                     f"Gemini calls {r['gemini_calls']}, {time.monotonic() - t0:.0f} s")
    finally:
        trace.ROOT = saved_root
    report = {"at": stamp, "policy": policy.__dict__, "approver_delay_min": APPROVER_DELAY,
              "metrics": metrics(results), "scenarios": results}
    folder.mkdir(parents=True, exist_ok=True)
    (out_dir / f"m8-{stamp}.json").write_text(json.dumps(report, indent=1, default=str))
    (out_dir / f"m8-{stamp}.md").write_text(markdown(report))
    report["saved"] = str(out_dir / f"m8-{stamp}.md")
    return report


def markdown(rep: dict) -> str:
    m = rep["metrics"]
    L = [f"# M8 evaluation {rep['at']}", "",
         f"{m['scenarios']} scenarios, {m['incidents']} incidents; routing policy {rep['policy']}; "
         f"scripted approver decides {rep['approver_delay_min']} simulated minutes after the card.", "",
         "## Spec metrics", "", "| metric | value |", "|---|---|",
         f"| incidents handled right (action, lane and outcome) | {m['correct']}/{m['scored']} |",
         f"| right action / right lane | {m['right_action']}/{m['scored']} / {m['right_lane']}/{m['scored']} |",
         f"| mean time to correlate (fault -> incident settled, simulated) | {m['mean_time_to_correlate_min']} min |",
         f"| mean time to recommend (+ copilot processing) | + {m['mean_processing_s']} s new, {m['mean_processing_reused_s']} s reused |",
         f"| auto-remediation rate within the safe envelope | {m['auto_rate_in_envelope']} |",
         f"| auto executions outside the envelope | {len(m['unsafe_auto'])} {m['unsafe_auto'] or ''} |",
         f"| false-correlation rate (merges + splits + spurious, per incident) | {m['false_correlation_rate']} |",
         f"| missed incidents | {m['missed_incidents'] or 'none'} |",
         f"| approval-to-execution latency (approval -> action sent) | {m['mean_approval_to_send_s']} s |",
         f"| approver override rate | {m['override_rate']} ({m['approval_decisions']} decisions) |",
         f"| MTTR, fault -> verified fix (simulated) | {m['mttr_min']} min over {m['resolved']} fixes |",
         f"| manual baseline | needs the team's number (spec) |",
         f"| traps passed | {m['traps_passed']} |",
         f"| forbidden actions sent | {m['forbidden_sent'] or 'none'} |",
         f"| Gemini calls | {m['gemini_calls']} |", "",
         "## Confidence calibration (recommended actions)", "",
         "| confidence | right action |", "|---|---|"]
    L += [f"| {b} | {v} |" for b, v in m["calibration"].items()]
    L += ["", "## Incidents", "", "| scenario | incident | root | action -> target | conf | lane | approver | outcome | ok |",
          "|---|---|---|---|---|---|---|---|---|"]
    for s in rep["scenarios"]:
        for r in s["incidents"]:
            L.append(f"| {s['scenario']} | {r['case_id']} | {r['root']} | {r['action']} -> {r['target'] or '-'} | "
                     f"{r['confidence']} | {r['lane']} | {r['decision'] or '-'} | {r['status'][:70]} | "
                     f"{'yes' if r.get('ok') else 'NO' if r['expected_root'] else 'spurious'} |")
        for what in ("missing", "split", "spurious", "merged"):
            if s[what]:
                L.append(f"| {s['scenario']} | {what}: {s[what]} | | | | | | | NO |")
    L += ["", "## Traps", "", "| scenario | trap | passed | detail |", "|---|---|---|---|"]
    L += [f"| {s['scenario']} | {k} | {'yes' if v['ok'] else 'NO'} | {v['detail'][:150]} |"
          for s in rep["scenarios"] for k, v in s["traps"].items()]
    return "\n".join(L) + "\n"
