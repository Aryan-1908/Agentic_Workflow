"""Live-mode capacity probe: the REAL Gemini diagnosis, not the scripted one.

Everything proven so far used scripted diagnoses. That tests correlation, root-cause selection,
routing and the guardrails — but never the model's reasoning. This answers the open question:

    Given a pool exhaustion where the database LOOKS slow but is healthy,
    does the real model blame the database?

The scripted run did (root=orders-db on pool_exhaustion_db_noisy). If the real one does too, that is
a finding about production behaviour. If it does not, the gap is narrower than the two failing tests
in tests/test_capacity.py suggest, and some of the planned work is unnecessary.

`copilot eval` cannot run these: the capacity set is deliberately outside tests/m8_eval/ so it does
not inflate the spec's 15-20 incident count. This drives the same harness entry point directly.

    export GOOGLE_API_KEY=...        # or put it in .env (git-ignored)
    .venv/bin/python run_live_capacity.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from copilot import harness  # noqa: E402
from copilot.__main__ import load_dotenv  # noqa: E402
from copilot.routing import RoutingPolicy  # noqa: E402
from tests.capacity_eval import ground_truth  # noqa: E402

POLICY = RoutingPolicy(auto_max_blast="low", auto_min_confidence=0.8, escalate_below_confidence=0.4)


def live_agents():
    from copilot.agents import Investigator, Recommender
    from copilot.kb.index import KnowledgeIndex
    from copilot.llm import get_llm

    llm, judge, rec_llm, rem_llm = (
        get_llm(r) for r in ("diagnosis", "judge", "recommender", "remediation")
    )

    def agents(memory):
        idx = KnowledgeIndex(memory.db)
        return {
            "investigator": Investigator(idx, llm, judge=judge, memory=memory),
            "recommender": Recommender(rec_llm, memory=memory),
            "remediation_llm": rem_llm,
        }

    return agents


def main() -> int:
    load_dotenv()
    import os

    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set (export it, or put it in .env)")
        return 1

    from copilot.memory import Memory

    kb = pathlib.Path("runs") / "memory.sqlite"
    if not kb.exists() or not Memory(kb).db.execute(
        "SELECT count(*) FROM kb_chunks"
    ).fetchone()[0]:
        print("The knowledge base is not indexed. Run first:\n  .venv/bin/python -m copilot kb index")
        print("Without it nothing is retrieved, every case escalates, and the probe would pass")
        print("for the wrong reason.")
        return 1

    agents = live_agents()
    print(f"{'scenario':<26} {'root':<14} {'action → target':<30} {'sent':<8} verdict")
    print("-" * 100)

    wrong = []
    for gt in ground_truth():
        name = gt["scenario"]
        expected = {i["root"] for i in gt["incidents"]}
        forbidden = {(a, t) for a, t in gt.get("forbid_sent", [])}
        with tempfile.TemporaryDirectory() as tmp:
            r = harness.run_scenario(gt, pathlib.Path(tmp) / name, agents, POLICY, kb_source=kb)

        if not r["incidents"]:
            print(f"{name:<26} {'(no incident)':<14}")
            continue
        for i in r["incidents"]:
            ok_root = i["root"] in expected
            sent_bad = i["sent"] and (i["action"], i["target"]) in forbidden
            verdict = "UNSAFE — forbidden action sent" if sent_bad else (
                "ok" if ok_root else f"WRONG SERVICE (expected {sorted(expected)})")
            if sent_bad or not ok_root:
                wrong.append((name, i["root"], i["action"], i["target"], verdict))
            print(f"{name:<26} {i['root']:<14} "
                  f"{i['action'] + ' → ' + str(i['target']):<30} "
                  f"{('SENT' if i['sent'] else 'blocked'):<8} {verdict}")

    print()
    hard = [w for w in wrong if w[0] == "pool_exhaustion_db_noisy"]
    if hard:
        print("The real model made the same mistake as the scripted one: it blamed the database")
        print("while orders-api was the service that ran out of workers. The root-cause rule reads")
        print("the dependency graph, not the evidence — see tests/test_capacity.py.")
    else:
        print("The real model did NOT blame the database on pool_exhaustion_db_noisy.")
        print("The gap is narrower than the scripted run suggested; re-check whether the")
        print("root-cause fix is still worth doing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
