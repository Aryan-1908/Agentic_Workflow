"""M4 evaluation baseline: retrieval quality per search mode, and how many planted fabrications the groundedness
check catches (tests/kb_eval/*.json). `python -m copilot kb eval` runs it with the real models and saves a report."""
import json, pathlib, time

from ..diagnosis import Diagnosis, check_groundedness

EVAL_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "tests" / "kb_eval"


def retrieval(index, modes=("keyword", "vector", "hybrid"), k: int = 5, reranker=None) -> dict:
    """recall@k (a gold document is in the top k) and MRR per mode, plus the questions each mode missed."""
    qs = json.loads((EVAL_DIR / "retrieval.json").read_text())["questions"]
    runs = [(m, None) for m in modes] + ([("hybrid+rerank", reranker)] if reranker is not None else [])
    out = {}
    for name, rr in runs:
        found, rr_sum, missed = 0, 0.0, []
        for q in qs:
            hits = index.search(q["q"], k=k, mode=name.split("+")[0], reranker=rr)
            rank = next((i for i, h in enumerate(hits, 1) if any(h["chunk_id"].startswith(g + "#") for g in q["gold"])), None)
            if rank:
                found += 1
                rr_sum += 1 / rank
            else:
                missed.append(q["q"])
        out[name] = {"recall@k": round(found / len(qs), 3), "mrr": round(rr_sum / len(qs), 3), "missed": missed}
    return {"k": k, "questions": len(qs), "modes": out}


def groundedness(index, judge=None) -> dict:
    """Every planted fabrication must be removed, and nothing removed from the clean diagnoses."""
    cases = json.loads((EVAL_DIR / "groundedness.json").read_text())["cases"]
    rows, caught, planted, false_removals = [], 0, 0, 0
    for c in cases:
        if c.get("needs_judge") and judge is None:
            rows.append({"id": c["id"], "skipped": "needs the judge"})
            continue
        retrieved = {cid: index.get(cid) for cid in c["retrieved"]}
        missing = [cid for cid, ch in retrieved.items() if ch is None]
        if missing:
            raise SystemExit(f"{c['id']}: sections not in the index: {missing} (run `copilot kb index`)")
        g = check_groundedness(Diagnosis(**c["diagnosis"]), retrieved, judge=judge, incident=c["incident"])
        got = sorted(r.where for r in g.removed)
        want = sorted(c["expect_removed"])
        planted += len(want)
        caught += len(set(got) & set(want))
        false_removals += len(set(got) - set(want))
        rows.append({"id": c["id"], "kind": c["kind"], "expected": want, "removed": got,
                     "ok": got == want, "reasons": [r.problem for r in g.removed]})
    return {"planted": planted, "caught": caught, "catch_rate": round(caught / planted, 3) if planted else None,
            "false_removals": false_removals, "judge": judge is not None, "cases": rows}


def novel_case(c: dict):
    """A Case for a novel-incident eval entry: one error signal per title, on the entry's service and host."""
    from datetime import datetime, timedelta, timezone
    from ..correlation import Case
    from ..signals import IncidentSignal
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    sigs = [IncidentSignal(source="log", service=c["service"], severity="error", timestamp=t0 + timedelta(minutes=i),
                           signal_type="event", signal_id=f"novel:{c['id']}:{i}", title=t,
                           resource={"type": "host", "name": c["host"]}, metric=f"log/{c['id']}")
            for i, t in enumerate(c["titles"])]
    return Case(case_id=f"novel-{c['id']}", signals=sigs, opened_at=t0, last_placed_at=t0)


def novel(index, llm=None, judge=None) -> dict:
    """Incidents the knowledge base doesn't cover must end in escalation, never an action (use case U12)."""
    from ..diagnosis import diagnose
    from .. import usage
    cases = json.loads((EVAL_DIR / "novel.json").read_text())["cases"]
    rows = []
    for c in cases:
        before = usage.spent()
        try:
            g = diagnose(novel_case(c), index, llm, judge=judge)
        except Exception as e:
            rows.append({"id": c["id"], "kind": c["kind"], "ok": False, "error": f"{type(e).__name__}: {e}"})
            continue
        actions = [s.action for s in g.diagnosis.steps if s.action not in ("escalate", "none")]
        ok = (not g.grounded) or not actions
        rows.append({"id": c["id"], "kind": c["kind"], "ok": ok, "status": g.status, "actions": actions,
                     "llm_calls": usage.spent() - before, "summary": g.diagnosis.summary[:160]})
    return {"cases": len(rows), "escalated": sum(r["ok"] for r in rows), "rows": rows}


def run(index, judge=None, reranker=None, save: bool = True, llm=None) -> dict:
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "retrieval": retrieval(index, reranker=reranker),
              "groundedness": groundedness(index, judge)}
    if llm is not None:
        report["novel"] = novel(index, llm, judge)
    if save:
        path = pathlib.Path("runs") / "eval" / f"kb-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=1))
        report["saved"] = str(path)
    return report
