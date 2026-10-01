"""Correlation test cases. Each file in tests/correlation_cases/ is one incident pattern: signals in arrival order
(minute offsets from a base time) and the expected grouping. Add a file to add a case; no code needed.

Case file fields:
  signals[]: id, minute, service, title, resource, metric, severity, source, type, [signal_id], [state]
  expect:    groups (lists of ids that must share a case, and only those), roots {id: service},
             needs_review [ids], all_clear [ids], unique_signals {id: n}, duplicates [ids], repeats [ids],
             context [ids] (info signals that open no case)"""
import json, pathlib
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from copilot.correlation import Correlator, CorrelationAgent, condition_key
from copilot.signals import IncidentSignal

CASES = sorted((pathlib.Path(__file__).parent / "correlation_cases").glob("*.json"))
BASE = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)   # fixed and in the past: intake rejects future timestamps


def make_signal(d: dict) -> IncidentSignal:
    return IncidentSignal(
        source=d.get("source", "cloud_monitoring"), service=d["service"], severity=d.get("severity", "critical"),
        timestamp=BASE + timedelta(minutes=d["minute"]), signal_type=d.get("type", "alert"),
        signal_id=d.get("signal_id", f"sig-{d['id']}"), title=d["title"], state=d.get("state", "open"),
        resource={"name": d["resource"]} if d.get("resource") else {}, metric=d.get("metric"))


class Clock:
    """Processing time that moves with the signals, so time-to-correlate is deterministic."""
    def __init__(self): self.now = BASE
    def __call__(self): return self.now


def run_case(case: dict, agent=None):
    clock = Clock()
    corr = Correlator(agent=agent, clock=clock)
    decisions, ids = {}, {}
    for d in case["signals"]:
        clock.now = BASE + timedelta(minutes=d["minute"], seconds=5)    # received 5 s after it happened
        sig = make_signal(d)
        ids[d["id"]] = sig.signal_id
        decisions[d["id"]] = corr.ingest(sig)
    corr.where_now = {k: (c.case_id if (c := corr.case_of(v)) else "context") for k, v in ids.items()}   # after all joins
    return corr, decisions


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_correlation_case(path):
    case = json.loads(path.read_text())
    corr, dec = run_case(case)
    exp = case["expect"]
    where = corr.where_now

    # every expected group shares one case, and different groups are different cases
    assert {i for i in where if where[i] == "context"} == set(exp.get("context", [])), "wrong signals kept as context only"
    where = {i: c for i, c in where.items() if c != "context"}
    got = {frozenset(i for i in where if where[i] == cid) for cid in set(where.values())}
    want = {frozenset(g) for g in exp["groups"]}
    assert got == want, f"{case['description']}\n  got {sorted(map(sorted, got))}"

    for sid, svc in exp.get("roots", {}).items():
        assert corr.cases[where[sid]].root_service == svc
    flagged = {i for i in where if corr.cases[where[i]].needs_review}
    assert flagged == _same_case(where, exp.get("needs_review", [])), "wrong cases flagged for review"
    for sid in exp.get("all_clear", []):
        assert corr.cases[where[sid]].all_clear
    for sid, n in exp.get("unique_signals", {}).items():
        assert len(corr.cases[where[sid]].signals) == n
    for sid in exp.get("duplicates", []):
        assert dec[sid].action == "duplicate"
    for sid in exp.get("repeats", []):
        assert dec[sid].action == "repeat"
    # every placement carries a human-readable reason
    assert all(d.reason for d in dec.values())


def _same_case(where, ids):
    return {i for i in where if any(where[i] == where[j] for j in ids)}


def test_every_case_file_is_documented():
    for p in CASES:
        c = json.loads(p.read_text())
        assert c.get("description") and c.get("checks") and c["expect"].get("groups"), p.name


# ---- metrics and hints -------------------------------------------------------------------------------

def test_time_to_correlate_is_recorded_per_case():
    corr, dec = run_case(json.loads((CASES[0].parent / "c03_db_cascade.json").read_text()))
    [(cid, secs)] = corr.time_to_correlate().items()
    assert secs == 180            # first signal received at 0:05, last one placed at 3:05


def test_root_is_the_upstream_service():
    corr, dec = run_case({"signals": [
        {"id": "a", "minute": 0, "service": "storefront", "title": "x", "resource": "web-01"},
        {"id": "b", "minute": 0, "service": "orders-api", "title": "y", "resource": "orders-01"}]})
    # storefront depends on orders-api, so they merge and orders-api is upstream
    assert dec["a"].case_id == dec["b"].case_id and corr.cases[dec["a"].case_id].root_service == "orders-api"


def test_condition_key_ignores_incident_id_but_not_resource():
    s1 = make_signal({"id": "1", "minute": 0, "service": "storefront", "title": "CPU", "resource": "web-01", "metric": "cpu"})
    s2 = make_signal({"id": "2", "minute": 1, "service": "storefront", "title": "CPU", "resource": "web-01", "metric": "cpu"})
    s3 = make_signal({"id": "3", "minute": 1, "service": "storefront", "title": "CPU", "resource": "web-02", "metric": "cpu"})
    assert condition_key(s1) == condition_key(s2) != condition_key(s3)


# ---- the agent (uncertain signals only), with a scripted LLM --------------------------------------------

class ScriptedLLM:
    """Replays tool calls. "{open}" in an argument is filled from the latest list_open_cases result,
    the same way a real model would read the case id from the tool output."""
    def __init__(self, steps): self.steps, self.i, self.bound = steps, 0, None
    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self
    def invoke(self, msgs):
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        if step is None:
            return AIMessage("done")
        name, args = step
        listed = [m.content for m in msgs if isinstance(m, ToolMessage) and m.content.startswith("[")]
        open_id = json.loads(listed[-1])[0]["case_id"] if listed else ""
        args = {k: (v.format(open=open_id) if isinstance(v, str) else v) for k, v in args.items()}
        return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"t{self.i}"}])


UNKNOWN_AFTER_SHOP = {"signals": [
    {"id": "shop", "minute": 0, "service": "storefront", "title": "Uptime check storefront-home failing", "resource": "34.93.10.20"},
    {"id": "mystery", "minute": 1, "service": "unknown", "title": "HTTP 5xx spike on 34.93.10.20", "resource": "34.93.10.20"}]}


def _agent_run(steps):
    llm = ScriptedLLM(steps)
    corr, dec = run_case(UNKNOWN_AFTER_SHOP, agent=CorrelationAgent(llm))
    return corr, dec, llm


def test_agent_can_merge_an_uncertain_signal_with_a_reason():
    corr, dec, llm = _agent_run([("list_open_cases", {}),
                                 ("merge_into_case", {"case_id": "{open}", "reason": "same public IP as the failing uptime check"})])
    assert set(llm.bound) == {"list_open_cases", "dependency_lookup", "status_feed_lookup", "merge_into_case", "open_new_case"}
    assert dec["mystery"].action == "merged" and dec["mystery"].by == "agent"
    assert dec["mystery"].case_id == dec["shop"].case_id
    assert "same public IP" in corr.cases[dec["shop"].case_id].rationale[-1]


def test_agent_cannot_merge_into_a_case_that_does_not_exist():
    corr, dec, _ = _agent_run([("merge_into_case", {"case_id": "case-made-up", "reason": "trust me"}), None])
    assert dec["mystery"].action == "new_case" and dec["mystery"].case_id != dec["shop"].case_id
    assert corr.cases[dec["mystery"].case_id].needs_review          # no valid decision -> a person looks at it


def test_agent_can_keep_a_signal_separate():
    corr, dec, _ = _agent_run([("open_new_case", {"reason": "different symptom, no shared resource"})])
    case = corr.cases[dec["mystery"].case_id]
    assert dec["mystery"].case_id != dec["shop"].case_id and not case.needs_review
