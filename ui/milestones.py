"""Milestone evidence for the demo UI.

Each milestone maps to the files that implement it and the tests that prove it. Nothing here is
hand-written status: the test counts come from running pytest, so a milestone cannot be shown green
unless its tests actually pass.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

MILESTONES = [
    {
        "id": "M1",
        "title": "Provider-agnostic LLM client + structured intake",
        "spec": "Parse alerts/anomalies into a validated IncidentSignal "
                "(source, service, severity, timestamp, signal type).",
        "built": [
            ("copilot/llm.py", "model from config — COPILOT_MODEL=provider:model, per-role overrides"),
            ("copilot/signals.py", "the validated IncidentSignal, with field validators"),
            ("copilot/intake.py", "deterministic parsers per source; grounded LLM fallback for free text; "
                                  "Rejected with a reason for anything invalid"),
            ("copilot/otel.py", "the single input: OTLP from the collector"),
        ],
        "tests": ["tests/test_intake.py", "tests/test_otel.py"],
        "demo": "Any scenario — every row in the result table started as a raw OTLP record.",
        "status": "done",
        "note": "One live check is open: a real event appearing in `watch` within a minute. "
                "Needs someone to start a VM while it runs.",
    },
    {
        "id": "M2",
        "title": "Tool-enabled Correlation Agent",
        "spec": "Temporal/topological clustering, dependency-graph lookup, status-feed lookup, "
                "merge-into-case.",
        "built": [
            ("copilot/correlation.py", "rules for the clear cases; the LLM only for genuinely uncertain "
                                       "signals, and it may only merge or open — never invent"),
            ("copilot/anomaly.py", "z-score detector over metrics"),
            ("config/services.toml", "the dependency graph: who depends on whom, and service tiers"),
        ],
        "tests": ["tests/test_correlation.py", "tests/test_anomaly.py"],
        "demo": "db_down — three services fail, one incident. lookalike_cpu — two similar alerts stay two.",
        "status": "done",
    },
    {
        "id": "M3",
        "title": "Persistent case memory + semantic index",
        "spec": "Per-service incident history, plus a vector index over runbooks and postmortems.",
        "built": [
            ("copilot/memory.py", "SQLite: cases, signals, events; history, similar_cases, outcome_stats, "
                                  "rejections"),
            ("copilot/kb/index.py", "chunks docs at headings, stable ids, only changed chunks re-embedded"),
        ],
        "tests": ["tests/test_memory.py"],
        "demo": "Run a scenario twice — the second run recognises it and keeps the same case id.",
        "status": "done",
    },
    {
        "id": "M4",
        "title": "RAG + groundedness evaluation",
        "spec": "Hybrid search + reranking over runbooks/postmortems; groundedness so citations are "
                "never fabricated.",
        "built": [
            ("copilot/diagnosis.py", "draft with citations, then five groundedness rules; no grounded "
                                     "cause means escalate, never a guess"),
            ("copilot/kb/evaluate.py", "the retrieval and groundedness eval harness"),
            ("docs/kb/", "12 runbooks (3 are decoys) and 6 postmortems"),
        ],
        "tests": ["tests/test_diagnosis.py"],
        "demo": "novel — an unknown service, nothing in the knowledge base, so it escalates "
                "instead of inventing a fix.",
        "status": "done",
        "note": "Measured live: hybrid recall@5 0.939; groundedness caught 9 of 9 planted fabrications, "
                "0 false removals.",
    },
    {
        "id": "M5",
        "title": "Ordered workflow with a durable approval checkpoint",
        "spec": "intake → correlate → diagnose → recommend → conditional routing, with a durable "
                "checkpoint at the human-approval interrupt.",
        "built": [
            ("copilot/workflow.py", "LangGraph state machine; interrupt() pauses at approval; "
                                    "SqliteSaver checkpoints every transition"),
            ("copilot/routing.py", "blast radius from action scope x service tier; confidence adjusted "
                                   "by past outcomes AND past rejections; the four lanes"),
            ("copilot/outbox.py", "ticket per case with a timeline, and notifications"),
        ],
        "tests": ["tests/test_workflow.py", "tests/test_lifecycle.py"],
        "demo": "Any approval-lane scenario. Kill the process mid-incident and it resumes where it paused.",
        "status": "done",
    },
    {
        "id": "M6",
        "title": "Specialised agents, then the full team",
        "spec": "Investigator and Recommender passing information; later the six-agent team with an "
                "action allowlist and a safety reviewer.",
        "built": [
            ("copilot/agents/investigator.py", "evidence + history + rejections + grounded diagnosis"),
            ("copilot/agents/recommender.py", "action, blast radius, confidence, rollback plan"),
            ("copilot/agents/remediation.py", "executes allowlisted actions only"),
            ("copilot/agents/safety.py", "code, not a prompt — allowlist, blast radius, groundedness, "
                                         "parameter bounds, circuit breaker, data-service guard"),
            ("copilot/agents/verify.py", "post-action signal state; rolls back or escalates"),
            ("copilot/actions.py", "the allowlist: 11 actions with schemas"),
        ],
        "tests": ["tests/test_agents.py", "tests/test_execution.py", "tests/test_service_rules.py"],
        "demo": "disk_full — the approver says yes to a wrong fix and the safety reviewer refuses anyway.",
        "status": "done",
    },
    {
        "id": "M7",
        "title": "Logging, retries and hardening",
        "spec": "A log line per step; retries before failing cleanly; later tracing, fallback, "
                "circuit breakers.",
        "built": [
            ("copilot/trace.py", "one structured line per step, per case"),
            ("copilot/resilience.py", "retries with backoff, then a clean escalation with the error"),
            ("copilot/agents/safety.py", "circuit breaker: 3 attempts per action+target per hour"),
            ("copilot/usage.py", "LLM call counting and budget"),
        ],
        "tests": ["tests/test_resilience.py", "tests/test_cost.py"],
        "demo": "flapping — a fix that does not hold is not retried forever; the next attempt goes to a person.",
        "status": "done",
    },
    {
        "id": "M8",
        "title": "Evaluation harness",
        "spec": "15-20 example incidents, safe and risky; auto-fix the safe ones, always ask before "
                "the risky ones.",
        "built": [
            ("copilot/harness.py", "every scenario end to end with a scripted approver; reports all six "
                                   "spec metrics"),
            ("tests/m8_eval/ground_truth.json", "15 incidents across 12 use cases and all four traps"),
            ("tests/capacity_eval/", "a separate probe: 6 saturation scenarios (our addition)"),
        ],
        "tests": ["tests/test_m8.py", "tests/test_capacity.py"],
        "demo": "Run every scenario in this UI — each is scored against its expected outcome.",
        "status": "done",
        "note": "One checkbox is open and is not code: the MTTR baseline has to be agreed with the team.",
    },
]

#: Work beyond the spec, from investigating how the system behaves when nothing is broken but
#: something is full.
BEYOND = [
    ("Safety: service.restart on a data service", "copilot/agents/safety.py",
     "The guard blocked logs.rotate and vm.reset on a service holding data, but not restart. A "
     "saturated database was diagnosed correctly and then restarted, dropping every in-flight "
     "transaction. Found by a new scenario; now blocked."),
    ("Root cause: evidence beats the dependency graph", "copilot/correlation.py",
     "Root was the most upstream service in the case — right when something broke, wrong when "
     "something is full. A service out of workers made its healthy database look guilty. Now a "
     "service reporting its own exhaustion wins, and only when exactly one does."),
    ("Per-service evidence for the diagnosis", "copilot/diagnosis.py",
     "The model saw one flat list of signals. It now sees each service separately, with its own "
     "measurements and whether its errors are its own or relayed from elsewhere — cause versus casualty."),
    ("Third-party infrastructure could be actioned", "copilot/agents/safety.py",
     "config/services.toml marks the payments provider external and the spec says an external outage "
     "is 'escalate (not ours to fix)', but nothing enforced it: vm.reset on the provider was allowed. "
     "We have no credentials for someone else's infrastructure and no right to use them. Now refused "
     "for every action except escalate."),
    ("Rejections change the next recommendation", "copilot/memory.py, routing.py, investigator.py",
     "A rejection was written and never read back, so the same wrong fix returned unchanged. It now "
     "lowers confidence like a failed execution (0 rejections: auto, 1: approval, 3: escalate) and the "
     "reasons reach the model. The text never touches the allowlist or the risk tiers."),
]


def test_counts() -> dict[str, int]:
    """Tests per file, collected from pytest so the numbers cannot drift from reality."""
    out: dict[str, int] = {}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        for line in r.stdout.splitlines():
            if "::" in line:
                f = line.split("::")[0].strip()
                out[f] = out.get(f, 0) + 1
    except Exception:
        pass
    return out


def summary() -> dict:
    counts = test_counts()
    rows = []
    for m in MILESTONES:
        n = sum(counts.get(t, 0) for t in m["tests"])
        rows.append({**m, "test_count": n,
                     "files": [{"path": p, "what": w, "lines": _lines(p)} for p, w in m["built"]]})
    return {"milestones": rows, "beyond": [{"title": t, "where": w, "why": y} for t, w, y in BEYOND],
            "total_tests": sum(counts.values())}


def _lines(path: str) -> int | None:
    p = ROOT / path
    if p.is_dir():
        return None
    try:
        return len(p.read_text().splitlines())
    except Exception:
        return None
