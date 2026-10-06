"""Copilot CLI.

    python -m copilot watch                                # read the OTel input (runs/otel/telemetry.jsonl) and follow it
    python -m copilot watch --once                         # read what's there, then stop
    python -m copilot parse alert.txt --source email --llm # a free-text alert, through the grounded LLM extraction
    python -m copilot replay runs/signals/20260928.jsonl   # re-run correlation over a recorded signal log
    python -m copilot memory history dev-backend           # past cases of a service (and how they ended)
    python -m copilot kb index | kb search "vm stopped"    # knowledge-base index
    python -m copilot kb eval [--rerank]                   # M4 baseline: retrieval per mode, groundedness catch rate
    python -m copilot diagnose case-d148eaf3               # grounded diagnosis of a stored case
    python -m copilot cases [--waiting]                    # incident workflows; --waiting shows approval cards
    python -m copilot approve <case> --by <name> [--reason …] | reject <case> --by <name> --reason …
    python -m copilot eval [--only cpu_runaway,db_down]    # M8: all scenarios, scored against the ground truth
    python -m copilot console                              # web console on http://127.0.0.1:8765
"""
import argparse, json, pathlib, sys, time
from datetime import datetime, timezone

from clients import load_client

from .correlation import CorrelationAgent, Correlator
from .llm import get_llm
from .memory import LATE, Memory, restore
from .diagnosis import diagnose, render, to_record
from . import trace, usage
from .outbox import LocalNotifier, LocalTickets
from .routing import RoutingPolicy
from .workflow import Workflow
from .otel import OTelReader
from .signals import IncidentSignal

SIGNAL_LOG = pathlib.Path("runs") / "signals"


def show(item) -> str:
    if isinstance(item, IncidentSignal):
        return (f"{item.timestamp:%H:%M:%S}  {item.severity.value:<8} {item.signal_type.value:<7} "
                f"{item.service:<18} {item.state:<6} {item.title}")
    return f"REJECTED ({item.source_hint}): {item.reason}"


def record(item):
    SIGNAL_LOG.mkdir(parents=True, exist_ok=True)
    kind = "signal" if isinstance(item, IncidentSignal) else "rejected"
    with open(SIGNAL_LOG / f"{datetime.now(timezone.utc):%Y%m%d}.jsonl", "a") as f:
        f.write(json.dumps({"kind": kind, **item.model_dump(mode="json")}) + "\n")


def cmd_parse(args):
    from .intake import intake
    text = pathlib.Path(args.file).read_text()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    llm = get_llm("intake") if args.llm else None
    for item in intake(payload, args.source, llm=llm):
        print(show(item))
        if args.json:
            print(json.dumps(item.model_dump(mode="json"), indent=2))


def _agent(status_lookup=None):
    """The correlation agent, or None (rules only) if no model is usable."""
    try:
        llm = get_llm("correlation")
    except Exception as e:
        hint = "GOOGLE_API_KEY not set" if not __import__("os").getenv("GOOGLE_API_KEY") else type(e).__name__
        print(f"correlation agent off ({hint}): rules only; uncertain signals are flagged for review")
        return None
    return CorrelationAgent(llm, status_lookup=status_lookup)


def cmd_replay(args):
    """Re-run correlation over a recorded signal log (runs/signals/<date>.jsonl), in timestamp order."""
    rows = [json.loads(l) for l in open(args.file) if l.strip()]
    sigs = sorted((IncidentSignal(**{k: v for k, v in r.items() if k != "kind"}) for r in rows if r.get("kind") == "signal"),
                  key=lambda s: s.timestamp)
    if args.client:
        load_client(args.client)     # applies the client's service map
    corr = Correlator(agent=None if args.rules_only else _agent())
    for s in sigs:
        d = corr.ingest(s)
        case = corr.case_of(s.signal_id)
        print(f"{show(s)}\n          -> {d.action} {case.case_id if case else '(no incident)'} ({d.by}: {d.reason})")
    print(f"\n{len(corr.open_cases())} case(s) from {len(sigs)} signal(s):")
    for c in corr.open_cases():
        print(f"  {c.case_id}  services={c.services}  root={c.root_service or '?'}  "
              f"signals={len(c.signals)}  review={c.needs_review}")


def seen_before(memory, case) -> str:
    hits = memory.similar_cases(case)
    if not hits:
        return "          memory: first time this kind of incident is seen"
    h = hits[0]
    fixed = "; ".join(f"{o.get('action')}: {o.get('result')}" for o in h["outcomes"]) or "no outcome recorded"
    return (f"          memory: seen {len(hits)}{'+' if len(hits) == 3 else ''} time(s) before, "
            f"last {h['first_seen'][:16]} ({h['case_id']}, similarity {h['score']}); {fixed}")


def diagnoser(memory):
    """(index, writer LLM, judge LLM) for diagnoses, or None with a note if the knowledge base or model isn't ready."""
    from .kb.index import KnowledgeIndex
    idx = KnowledgeIndex(memory.db)
    if not idx.db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0]:
        print("diagnosis off: knowledge base not indexed (run: python -m copilot kb index)")
        return None
    try:
        return idx, get_llm("diagnosis"), get_llm("judge")
    except Exception as e:
        import os
        why = ("GOOGLE_API_KEY not set: put GOOGLE_API_KEY=... in .env (git-ignored) or export it"
               if not os.getenv("GOOGLE_API_KEY") else type(e).__name__)
        print(f"diagnosis off: {why}")
        return None


def run_diagnosis(diag, memory, case_id, case) -> str:
    idx, llm, judge = diag
    try:
        g = diagnose(case, idx, llm, judge=judge, memory=memory)
    except Exception as e:      # a failed LLM call must not stop the watch (M7 adds retries)
        return f"          diagnosis failed: {type(e).__name__}: {str(e)[:150]}"
    memory.record(case_id, "diagnosis", to_record(g))
    return render(g)


def make_workflow(memory, client, diag=None) -> Workflow:
    idx, llm, judge = diag or (None, None, None)
    from .agents import Investigator, Recommender
    def step(msg):                    # agents' sub-steps: on screen, and in the case's trace (M7)
        print(f"            … {msg}", flush=True)
        trace.event("agent", "info", msg)
    investigator = Investigator(idx, llm, judge=judge, memory=memory, progress=step) if diag else None
    recommender = Recommender(get_llm("recommender") if diag else None, memory=memory, progress=step)
    executor = None
    if client.execution == "simulated":
        from .agents.remediation import ExecutionAgent
        from .agents.safety import SafetyReviewer
        from .cloud import SimCloud
        policy = RoutingPolicy.from_config(client.routing)
        executor = ExecutionAgent(SimCloud(), client.telemetry, SafetyReviewer(policy, memory),
                                  llm=get_llm("remediation") if diag else None, progress=step)
    return Workflow(memory, LocalTickets(), LocalNotifier(), policy=RoutingPolicy.from_config(client.routing),
                    execution=client.execution, investigator=investigator, recommender=recommender, executor=executor)


def start_workflow(flow, case_id, case) -> str:
    print("          workflow: diagnosing (knowledge base + Gemini; each call times out after 60 s)", flush=True)
    t0 = time.time()
    try:
        st = flow.start(case_id, case)
    except Exception as e:      # a failed LLM call must not stop the watch (M7 adds retries)
        return f"          workflow failed: {type(e).__name__}: {str(e)[:150]}"
    out = [f"          {t['step']}: {t['detail']}" for t in st.get("trace", []) if t["step"] != "start"]
    out.append(f"          ({time.time() - t0:.0f} s; Gemini calls today {usage.spent()}/{usage.budget()})")
    if st.get("waiting"):
        out.append(f"          -> AWAITING APPROVAL: python -m copilot approve {case_id} --by <name>   (or reject … --reason)")
    return "\n".join(out)


def load_dotenv(path: pathlib.Path = pathlib.Path(".env")):
    """KEY=value lines from a local, git-ignored .env (e.g. GOOGLE_API_KEY), so keys never go on the command line
    or into chat. Variables already set in the environment win."""
    if not path.exists():
        return
    import os
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cmd_reset(args):
    """Start a clean demo: forget cases, workflows, tickets, notifications and telemetry. Keeps the knowledge-base index.
    Independent simulator runs are separate worlds; without a reset, an incident left open by one run (its alerts never
    resolve) is still 'open' when the next run starts."""
    if not args.yes:
        sys.exit("this deletes runs/workflow.sqlite, runs/tickets, runs/notifications.jsonl, runs/otel/telemetry.jsonl\n"
                 "and the cases in runs/memory.sqlite (the knowledge-base index is kept). Stop the Collector first, then\n"
                 "run again with --yes")
    import shutil
    for f in ("runs/workflow.sqlite", "runs/notifications.jsonl", "runs/otel/telemetry.jsonl"):
        pathlib.Path(f).unlink(missing_ok=True)
    shutil.rmtree("runs/tickets", ignore_errors=True)
    m = Memory()
    with m.db:
        if args.keep_memory:        # keep past cases for "seen before", but as finished: never resumed as open
            m.db.execute("UPDATE cases SET all_clear=1")
        else:
            for t in ("cases", "case_signals", "case_events"):
                m.db.execute(f"DELETE FROM {t}")
    print("reset done" + (" (memory kept)" if args.keep_memory else "") + ". Start the Collector again before sending data.")


def cmd_cases(args):
    client = load_client(args.client)
    flow = make_workflow(Memory(), client)
    for cid in flow.cases():
        st = flow.state(cid)
        if args.waiting and not st["waiting"]:
            continue
        rec = st.get("recommendation") or {}
        print(f"{cid}  {','.join(st.get('services', [])):<22} {st['status']:<45} "
              f"{rec.get('action', '-')} ({rec.get('blast_radius', '-')}, conf {rec.get('confidence', '-')})")
        if st["waiting"] and args.waiting:
            print(json.dumps(st["card"], indent=2))


def cmd_decide(args):
    client = load_client(args.client)
    flow = make_workflow(Memory(), client)
    try:
        st = flow.decide(args.case_id, args.cmd, by=args.by, reason=args.reason or "")
    except ValueError as e:
        sys.exit(str(e))
    for t in st.get("trace", [])[-3:]:
        print(f"{t['at']}  {t['step']}: {t['detail']}")
    print(f"ticket: runs/tickets/{args.case_id}.md")


def cmd_diagnose(args):
    m = Memory()
    if args.client:
        load_client(args.client)
    case = m.get(args.case_id)
    if not case:
        sys.exit(f"no case {args.case_id} in memory (see: python -m copilot memory history <service>)")
    diag = diagnoser(m)
    if not diag:
        sys.exit(1)
    print(run_diagnosis(diag, m, args.case_id, case))


def cmd_memory(args):
    m = Memory()
    if args.what == "history":
        for h in m.history(args.service, args.limit):
            outs = "; ".join(f"{o.get('action')}: {o.get('result')}" for o in h["outcomes"]) or "-"
            print(f"{h['first_seen'][:16]}  {h['case_id']}  {','.join(h['services'])}  {' + '.join(h['signature']) or '(context only)'}  outcome: {outs}")
    elif args.what == "show":
        case = m.get(args.service)
        if not case:
            sys.exit(f"no case {args.service}")
        print(json.dumps({**case.summary(), "rationale": case.rationale, "repeats": case.repeats,
                          "events": m.events(case.case_id)}, indent=2))


def cmd_kb(args):
    from .kb.index import KnowledgeIndex
    idx = KnowledgeIndex(Memory().db)
    if args.what == "index":
        print(idx.rebuild())
    elif args.what == "eval":
        from .kb import evaluate
        rep = evaluate.run(idx, judge=None if args.no_judge else get_llm("judge"),
                           reranker=get_llm("rerank") if args.rerank else None,
                           llm=get_llm("diagnosis") if args.novel else None)
        r = rep["retrieval"]
        print(f"retrieval ({r['questions']} questions, recall@{r['k']} / MRR):")
        for mode, v in r["modes"].items():
            print(f"  {mode:<14} {v['recall@k']:.3f} / {v['mrr']:.3f}   missed {len(v['missed'])}")
        g = rep["groundedness"]
        print(f"groundedness: caught {g['caught']}/{g['planted']} planted fabrications, "
              f"{g['false_removals']} false removal(s), judge {'on' if g['judge'] else 'off'}")
        for c in g["cases"]:
            if not c.get("ok", True) or c.get("skipped"):
                print(f"  {c['id']}: {c.get('skipped') or 'expected ' + str(c['expected']) + ' got ' + str(c['removed'])}")
        if "novel" in rep:
            n = rep["novel"]
            print(f"novel incidents: {n['escalated']}/{n['cases']} escalated with no action")
            for r in n["rows"]:
                print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['id']:<18} {r['kind']:<26} "
                      f"{r.get('status', r.get('error', ''))[:40]:<40} Gemini calls {r.get('llm_calls', '-')}"
                      + (f"  actions {r['actions']}" if r.get("actions") else ""))
        print(f"report: {rep['saved']}")
    else:
        for h in idx.search(" ".join(args.query), args.k):
            print(f"{h['score']:.3f}  {h['chunk_id']}\n       {h['text'].splitlines()[1][:110] if len(h['text'].splitlines()) > 1 else ''}")


def cmd_eval(args):
    """M8: every simulator scenario through the whole copilot (real Gemini agents), scored against the ground truth."""
    from . import harness
    from .agents import Investigator, Recommender
    from .kb.index import KnowledgeIndex
    client = load_client(args.client)
    policy = RoutingPolicy.from_config(client.routing)
    if not Memory().db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0]:
        sys.exit("knowledge base not indexed (run: python -m copilot kb index)")
    llm, judge, rec_llm, rem_llm = (get_llm(r) for r in ("diagnosis", "judge", "recommender", "remediation"))

    def agents(memory):
        idx = KnowledgeIndex(memory.db)
        return {"investigator": Investigator(idx, llm, judge=judge, memory=memory),
                "recommender": Recommender(rec_llm, memory=memory), "remediation_llm": rem_llm}
    only = args.only.split(",") if args.only else None
    print(f"M8 evaluation: {len(only) if only else len(harness.ground_truth())} scenario(s), each against a fresh "
          f"simulated shop; Gemini calls today {usage.spent()}/{usage.budget()}")
    rep = harness.run(agents, policy, only=only, correlation_agent=None if args.rules_only else _agent())
    m = rep["metrics"]
    print(f"\nhandled right {m['correct']}/{m['scored']}; auto in envelope {m['auto_rate_in_envelope']}; "
          f"false correlation {m['false_correlation_rate']}; overrides {m['override_rate']}; traps {m['traps_passed']}; "
          f"MTTR {m['mttr_min']} min; Gemini calls {m['gemini_calls']}")
    print(f"report: {rep['saved']}")


def cmd_console(args):
    """The local web console: incidents, approvals, signals, usage, and Ask the copilot."""
    from .console import ConsoleData, serve
    from .kb.index import KnowledgeIndex
    client = load_client(args.client)
    memory = Memory()
    try:
        index = KnowledgeIndex(memory.db) if memory.db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] else None
    except Exception:            # a fresh memory without the knowledge-base table
        index = None
    if index is None:
        print("knowledge base not indexed: Ask only sees incidents (run: python -m copilot kb index)")
    serve(ConsoleData(memory, make_workflow(memory, client), index=index, llm_factory=lambda: get_llm("ask")), args.port)


def cmd_watch(args):
    from .engine import Engine
    client = load_client(args.client)
    reader = OTelReader(client.telemetry, from_start=not args.tail)      # the single input: OpenTelemetry
    memory = Memory()
    engine = Engine(reader, memory, record=record, seen_before=seen_before)
    engine.corr = None if args.no_correlate else Correlator(agent=_agent(lambda p: engine.status_seen.get(p, [])))
    if engine.corr:
        restore(engine.corr, memory, LATE)
        if engine.corr.open_cases():
            print(f"resumed {len(engine.corr.open_cases())} open case(s) from memory")
    diag = None if args.no_diagnosis else diagnoser(memory)
    engine.flow = make_workflow(memory, client, diag) if diag else None
    if engine.flow:
        for cid in engine.flow.unfinished():   # stopped part-way last time (Ctrl+C): finish them first
            case = memory.get(cid)
            if case:
                print(f"resuming unfinished workflow {cid} [{', '.join(case.services)}]")
                print(start_workflow(engine.flow, cid, case))
    print(f"watching {client.telemetry} (OpenTelemetry, {'new data only' if args.tail else 'from the start'}); "
          f"signals -> {SIGNAL_LOG}/  (Ctrl+C to stop)")
    try:
        while True:
            engine.step(final=args.once)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m copilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="parse one payload file into IncidentSignal(s)")
    p.add_argument("file")
    p.add_argument("--source", required=True, help="anomaly (detector JSON) | <any name> for free text (needs --llm)")
    p.add_argument("--llm", action="store_true", help="allow the LLM fallback for free text")
    p.add_argument("--json", action="store_true", help="print the full validated object")
    w = sub.add_parser("watch", help="read the OpenTelemetry input and run the copilot on it")
    w.add_argument("--client", default="poc-test")
    w.add_argument("--interval", type=int, default=5, help="seconds between reads of the telemetry file")
    w.add_argument("--once", action="store_true")
    w.add_argument("--no-correlate", action="store_true", help="only print signals (M1 behaviour)")
    w.add_argument("--no-diagnosis", action="store_true", help="skip the LLM diagnosis of new incidents")
    w.add_argument("--tail", action="store_true", help="skip what's already in the file; only read new telemetry")
    r = sub.add_parser("replay", help="re-run correlation over a recorded signal log")
    r.add_argument("file")
    r.add_argument("--rules-only", action="store_true", help="no LLM agent; uncertain signals are flagged")
    r.add_argument("--client", help="use this client's service map (clients/<name>.toml)")
    mem = sub.add_parser("memory", help="case memory: history of a service, or one case")
    mem.add_argument("what", choices=["history", "show"])
    mem.add_argument("service", help="service name (history) or case id (show)")
    mem.add_argument("--limit", type=int, default=20)
    k = sub.add_parser("kb", help="knowledge base: rebuild the index, or search it")
    k.add_argument("what", choices=["index", "search", "eval"])
    k.add_argument("--rerank", action="store_true", help="eval: also measure hybrid + LLM reranking")
    k.add_argument("--no-judge", action="store_true", help="eval: rules-only groundedness")
    k.add_argument("--novel", action="store_true", help="eval: also incidents the KB doesn't cover (must escalate)")
    k.add_argument("query", nargs="*")
    k.add_argument("-k", type=int, default=5)
    dg = sub.add_parser("diagnose", help="grounded diagnosis of a case from memory")
    dg.add_argument("case_id")
    dg.add_argument("--client", default="poc-test")
    cs = sub.add_parser("cases", help="incident workflows and their status")
    cs.add_argument("--waiting", action="store_true", help="only cases awaiting approval, with their approval card")
    cs.add_argument("--client", default="poc-test")
    for verb in ("approve", "reject"):
        v = sub.add_parser(verb, help=f"{verb} a case waiting for approval")
        v.add_argument("case_id")
        v.add_argument("--by", required=True, help="your name (audit trail)")
        v.add_argument("--reason", required=(verb == "reject"))
        v.add_argument("--client", default="poc-test")
    ev = sub.add_parser("eval", help="M8: run every scenario through the copilot and score it against the ground truth")
    ev.add_argument("--only", help="comma-separated scenario names")
    ev.add_argument("--client", default="poc-test")
    ev.add_argument("--rules-only", action="store_true", help="correlation without the LLM agent")
    co = sub.add_parser("console", help="local web console (incidents, approvals, signals, Ask the copilot)")
    co.add_argument("--port", type=int, default=8765)
    co.add_argument("--client", default="poc-test")
    rs = sub.add_parser("reset", help="start a clean demo (keeps the knowledge-base index)")
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--keep-memory", action="store_true", help="keep past cases (for 'seen before' across runs)")
    args = ap.parse_args()
    {"parse": cmd_parse, "watch": cmd_watch, "replay": cmd_replay, "memory": cmd_memory, "kb": cmd_kb,
     "diagnose": cmd_diagnose, "cases": cmd_cases, "approve": cmd_decide, "reject": cmd_decide,
     "reset": cmd_reset, "eval": cmd_eval, "console": cmd_console}[args.cmd](args)


if __name__ == "__main__":
    main()
