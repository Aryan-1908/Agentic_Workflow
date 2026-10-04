"""A small web UI over the copilot, for trying scenarios by hand.

Nothing here reimplements the pipeline: it calls `harness.run_scenario`, the same entry point the
M8 eval and tests use, so what the browser shows is what the copilot actually did.

    .venv/bin/python -m ui.server        # then open http://127.0.0.1:8777

Two modes:
  offline (default)  scripted diagnoses, as tests/test_m8.py uses. No API key, no cloud, ~1s a run.
                     Correlation, routing, safety, execution and verification are all REAL — only the
                     LLM's diagnosis text is scripted, so the run is deterministic.
  live  (?live=1)    the real Gemini Investigator/Recommender. Needs GOOGLE_API_KEY and an indexed KB
                     (python -m copilot kb index).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from copilot import harness  # noqa: E402
from copilot.routing import RoutingPolicy  # noqa: E402
from sim.scenarios import SCENARIOS  # noqa: E402
from ui.milestones import summary as milestone_summary  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8777

POLICY = RoutingPolicy(auto_max_blast="low", auto_min_confidence=0.8, escalate_below_confidence=0.4)

# What the scripted diagnosis recommends, per scenario and root service. tests/test_m8.py keys on
# root service alone, which is enough for the three scenarios it exercises; the UI runs all 15, and
# `storefront` alone is ambiguous (a stopped VM, a bad release and a deleted firewall rule all
# surface there with different right answers). Keying on the scenario stands in for the real
# diagnosis, which distinguishes them from the evidence.
SCRIPT = {
    "vm_stopped": {"storefront": ("vm.start", "Start the stopped VM web-01.")},
    "process_crash": {"storefront": ("service.restart", "Restart the storefront on web-01.")},
    "cpu_runaway": {"reports-batch": ("service.restart", "Restart the stuck report service on batch-01.")},
    "flapping": {"reports-batch": ("service.restart", "Restart the stuck report service on batch-01.")},
    "lookalike_cpu": {
        "reports-batch": ("service.restart", "Restart the stuck report service on batch-01."),
        "storefront": ("none", "CPU is high from genuine traffic; no action needed."),
    },
    "db_down": {"orders-db": ("vm.start", "Start the database VM db-01.")},
    "bad_release": {"storefront": ("mig.rollback", "Roll back the storefront release on web-01.")},
    "firewall_blocked": {"storefront": ("firewall.restore", "Restore the HTTP rule to web-01.")},
    "payfast_outage": {"payments-provider": ("escalate", "The payment provider is down; not ours to fix.")},
    "guest_agent_noise": {"storefront": ("none", "Known benign guest-agent noise after a restart.")},
    "scheduled_stop": {"reports-batch": ("none", "batch-01 was stopped by its instance schedule.")},
    "blip": {"storefront": ("service.restart", "Restart the storefront on web-01.")},
    # disk_full deliberately scripts the WRONG action: the Safety Reviewer must block it even after
    # the approver says yes. That is the spec's "unsafe fix" trap.
    "disk_full": {"orders-db": ("vm.start", "Start the database VM db-01.")},
    "novel": {},  # nothing in the KB covers session-cache → escalate
    # Capacity probe: the right answer is to escalate for capacity, never to touch the dependency.
    "pool_exhaustion": {"orders-api": ("escalate", "orders-api is out of workers; escalate for capacity.")},
    "pool_exhaustion_db_noisy": {"orders-api": ("escalate", "orders-api is out of workers; db-01 is healthy.")},
    "storefront_saturated": {"storefront": ("escalate", "The storefront's thread pool is full; escalate for capacity.")},
    "batch_saturated": {"reports-batch": ("escalate", "The report worker pool is full; escalate for capacity.")},
    "db_connections_full": {"orders-db": ("escalate", "db-01 is out of connection slots; escalate, never restart.")},
    "cpu_vs_capacity": {"orders-api": ("escalate", "High CPU and a saturated pool: ambiguous, ask a person.")},
}
FALLBACK = ("escalate", "No runbook covers this; escalating.")

# The harness writes into a run folder; one run at a time keeps two requests from interleaving.
_lock = threading.Lock()


def _scripted_agents(scenario: str):
    from test_execution import grounded

    per_service = SCRIPT.get(scenario, {})

    def agents(memory):
        def diagnose(case):
            action, text = per_service.get(case.root_service, FALLBACK)
            return grounded(action, text)

        return {"diagnose_fn": diagnose}

    return agents


def _live_agents():
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


def list_scenarios() -> list[dict]:
    truth = {g["scenario"]: g for g in _all_ground_truth()}
    out = []
    for name, (desc, use_case, _events) in SCENARIOS.items():
        gt = truth.get(name, {})
        expected = gt.get("incidents", [])
        out.append(
            {
                "name": name,
                "description": desc,
                "use_case": use_case,
                "scored": name in truth,
                "expected": [
                    {
                        "root": i.get("root"),
                        "actions": i.get("actions", []),
                        "lanes": i.get("lanes", []),
                        "ends": i.get("ends", []),
                    }
                    for i in expected
                ],
            }
        )
    return out


def _all_ground_truth() -> list[dict]:
    """The M8 eval set plus the capacity probe.

    The capacity set lives in tests/capacity_eval/ so it does not inflate the spec's 15-20 incident
    count, but the UI should still be able to run those scenarios.
    """
    from tests.capacity_eval import ground_truth as capacity_ground_truth

    return list(harness.ground_truth()) + list(capacity_ground_truth())


def run_scenario(name: str, live: bool = False) -> dict:
    gt = next((g for g in _all_ground_truth() if g["scenario"] == name), None)
    if gt is None:
        raise KeyError(f"{name!r} has no ground truth to score against")

    agents = _live_agents() if live else _scripted_agents(name)
    with _lock, tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        result = harness.run_scenario(
            gt, tmpdir / name, agents, POLICY,
            kb_source=(pathlib.Path("runs") / "memory.sqlite") if live else None,
        )
        # The reasoning lives in the run's memory database: the signals that arrived, why each one
        # joined the case, and every step from diagnosis to outcome. Without it the UI shows a
        # verdict with no way to check it.
        detail = _reasoning(tmpdir)

    return {
        "scenario": name,
        "description": SCENARIOS[name][0],
        "use_case": SCENARIOS[name][1],
        "mode": "live" if live else "offline",
        "result": result,
        "metrics": harness.metrics([result]),
        "detail": detail,
    }


def _reasoning(run_dir: pathlib.Path) -> dict:
    """Signals, correlation rationale and the decision trail, per case."""
    from copilot.correlation import Case
    from copilot.memory import Memory

    db = next(run_dir.rglob("memory.sqlite"), None)
    if db is None:
        return {}
    m = Memory(db)
    out = {}
    for row in m.db.execute("SELECT case_id, data FROM cases"):
        case = Case.model_validate_json(row["data"])
        by_service: dict[str, dict] = {}
        for sig in case.signals:
            b = by_service.setdefault(sig.service, {"measured": {}, "own": [], "relayed": []})
            if sig.metric and sig.value is not None:
                b["measured"][sig.metric] = sig.value
            if sig.severity.value == "info":
                continue
            text = sig.title.lower()
            others = [o for o in case.services if o != sig.service and o != "unknown"]
            relay = ("database error" in text or "upstream" in text
                     or any(o.lower() in text for o in others))
            b["relayed" if relay else "own"].append(sig.title)
        out[row["case_id"]] = {
            "services": case.services,
            "root_service": case.root_service,
            "signals": [{"source": s.source, "service": s.service, "severity": s.severity.value,
                         "title": s.title, "metric": s.metric, "value": s.value}
                        for s in case.signals],
            "rationale": case.rationale,
            "by_service": by_service,
            "events": [{"kind": e["kind"], "at": e["at"],
                        "detail": {k: v for k, v in e.items() if k not in ("kind", "at")}}
                       for e in m.events(row["case_id"])],
        }
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/scenarios":
            self._json(200, list_scenarios())
        elif path == "/api/milestones":
            self._json(200, milestone_summary())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        live = parse_qs(parsed.query).get("live", ["0"])[0] == "1"
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, run_scenario(body.get("scenario", ""), live=live))
        except KeyError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(
                500, {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
            )

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    scored = sum(1 for s in list_scenarios() if s["scored"])
    print(f"Copilot UI → http://127.0.0.1:{PORT}")
    print(f"{scored} scored scenarios · offline mode (no API key needed)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
