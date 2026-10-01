"""The incident workflow (spec M5): "Expressed as a stateful, checkpointed workflow, so an incident can sit 'awaiting
approval' indefinitely without losing state."

Signals are taken in and correlated continuously (watch). When a case becomes an incident, one workflow runs for it:

    diagnose -> recommend -> route --auto-----> execute --> close
                                   --approval-> [pause: approval card] --approved--> execute --> close
                                                                       --rejected--> close
                                   --escalate-> escalate --> close
                                   --none-----> close

State is checkpointed in runs/workflow.sqlite under the case id: kill the process while a case waits for approval,
start it again, and `copilot approve <case>` continues from the pause. Every step goes into the case's trace, its
ticket, and case memory (the audit trail). Execution is recorded but not performed: in an observe-only project it
never is, and real execution is M6."""
import operator, os, pathlib, sqlite3, time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

from . import trace
from .correlation import Case
from .diagnosis import Diagnosis, GroundedDiagnosis, to_record
from .routing import Recommendation, RoutingPolicy, recommend, route

DEFAULT_PATH = pathlib.Path("runs") / "workflow.sqlite"
SETTLE = timedelta(seconds=int(os.getenv("COPILOT_SETTLE_SECONDS", "20")))   # quiet time before diagnosing
SETTLE_MAX = timedelta(minutes=2)                                            # never wait longer than this


def settled(case, now: datetime, quiet: timedelta = SETTLE, max_wait: timedelta = SETTLE_MAX) -> bool:
    """Diagnose a new incident only once it has stopped growing (spec order: correlate, then diagnose). Seen live
    29 Sep: diagnosing on the first signal alone gave thin evidence and results that varied run to run."""
    return now - case.last_placed_at >= quiet or now - case.opened_at >= max_wait


class State(TypedDict, total=False):
    case_id: str
    case: dict                  # the Case, so a resumed workflow needs nothing from the live correlator
    services: list[str]
    diagnosis: dict
    investigation: dict         # the Investigator's structured handoff to the Recommender
    recommendation: dict
    route: dict
    decision: dict              # {decision, by, reason, at}
    execution: dict
    status: str
    trace: Annotated[list[dict], operator.add]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _t(step: str, detail: str) -> list[dict]:
    return [{"at": _now(), "step": step, "detail": detail}]


class Workflow:
    def __init__(self, memory, tickets, notifier, policy: RoutingPolicy | None = None, execution: str = "off",
                 diagnose_fn: Callable | None = None, path: str | pathlib.Path = DEFAULT_PATH,
                 investigator=None, recommender=None, executor=None):
        """execution: "off" (observe-only project: never act) or "dry-run" (record what would run; M6 makes it real).
        diagnose_fn(case) -> GroundedDiagnosis; only needed to start new workflows, not to approve or reject."""
        self.memory, self.tickets, self.notifier = memory, tickets, notifier
        self.policy, self.execution, self.diagnose_fn = policy or RoutingPolicy(), execution, diagnose_fn
        self.investigator, self.recommender = investigator, recommender      # M6 agents (step 1)
        self.executor = executor              # Action Execution agent (step 2), used when execution == "simulated"
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()          # create the checkpoint tables up front, so listing works on a fresh machine
        self.graph = self._build().compile(checkpointer=self.saver)

    # ---- nodes -------------------------------------------------------------------------------------------
    def _diagnose(self, s: State) -> State:
        """Investigator agent: evidence + similar past incidents + a grounded diagnosis (reused, or new via Gemini)."""
        case = Case.model_validate(s["case"])
        from .agents import Investigation
        if self.investigator is None and self.diagnose_fn is None:
            raise RuntimeError("no diagnosis model configured")
        try:
            if self.investigator is not None:
                inv = self.investigator.investigate(case)
            else:                                    # plain diagnosis function (tests, simple setups)
                inv = Investigation(case_id=s["case_id"], evidence="", diagnosis=self.diagnose_fn(case))
        except Exception as e:                       # failed after retries: escalate with the error, don't hang
            inv = Investigation(case_id=s["case_id"], evidence="", diagnosis=GroundedDiagnosis(
                diagnosis=Diagnosis(summary=f"investigation failed after retries: {type(e).__name__}: {e}",
                                    root_cause=None, steps=[], confidence=0.0),
                removed=[], grounded=False, sources=[]))
        g = inv.diagnosis
        rec = to_record(g)
        self.memory.record(s["case_id"], "diagnosis", rec)
        how = f"reused from {inv.reused_from}" if inv.reused_from else g.status
        return {"diagnosis": rec, "investigation": inv.model_dump(mode="json"),
                "trace": _t("investigate", f"{how}: {g.diagnosis.summary[:160]}"
                                           + (f" ({len(g.removed)} unsupported claim(s) removed)" if g.removed else ""))}

    def _recommend(self, s: State) -> State:
        """Recommender agent: action, target, blast radius, confidence, rollback (Gemini only if the target is unclear)."""
        case = Case.model_validate(s["case"])
        if self.recommender is not None:
            from .agents import Investigation
            rec = self.recommender.recommend(case, Investigation.model_validate(s["investigation"]))
        else:
            g = GroundedDiagnosis.model_validate({k: v for k, v in s["diagnosis"].items() if k != "status"})
            rec = recommend(case, g, self.memory)
        self.memory.record(s["case_id"], "recommendation", rec.model_dump())
        return {"recommendation": rec.model_dump(),
                "trace": _t("recommend", f"{rec.action} on {rec.target or '-'}"
                                         + (" (target chosen by the LLM)" if rec.target_by == "llm" else "")
                                         + f", blast radius {rec.blast_radius}, confidence {rec.confidence}")}

    def _route(self, s: State) -> State:
        rec = Recommendation.model_validate(s["recommendation"])
        r = route(rec, s["diagnosis"]["grounded"], self.policy)
        return {"route": r.model_dump(), "trace": _t("route", f"{r.lane}: {r.reason}")}

    def _approval(self, s: State) -> State:
        rec = s["recommendation"]
        card = {"case_id": s["case_id"], "services": s["services"], "summary": s["diagnosis"]["diagnosis"]["summary"],
                "action": rec["action"], "target": rec["target"], "blast_radius": rec["blast_radius"],
                "confidence": rec["confidence"], "rollback": rec["rollback"], "why": s["route"]["reason"],
                "citations": rec["citations"]}
        self._update_ticket(s, status="awaiting approval")
        self.notifier.send("approvers", s["case_id"], f"approval needed: {rec['action']} on {rec['target']} "
                                                      f"(blast {rec['blast_radius']}, confidence {rec['confidence']})")
        answer = interrupt(card)            # the workflow stops here until approve/reject resumes it
        decision = {**answer, "at": _now()}
        self.memory.record(s["case_id"], "approval", decision)
        return {"decision": decision,
                "trace": _t("approval", f"{decision['decision']} by {decision['by']}"
                                        + (f": {decision['reason']}" if decision.get("reason") else ""))}

    def _execute(self, s: State) -> State:
        rec = s["recommendation"]
        if self.execution == "simulated" and self.executor is not None:
            # the latest version of the case (watch keeps adding signals to it in memory), not the snapshot from the
            # start: verification must know every alert that is open now
            case = self.memory.get(s["case_id"]) or Case.model_validate(s["case"])
            steps = s["diagnosis"]["diagnosis"]["steps"]
            decision = s.get("decision") or {}
            r = self.executor.run(Recommendation.model_validate(rec), case, lane=s["route"]["lane"],
                                  grounded=s["diagnosis"]["grounded"], grounded_actions={x["action"] for x in steps},
                                  approved_by=decision.get("by") if decision.get("decision") == "approve" else None)
            result = r.model_dump()
            self.memory.record(s["case_id"], "execution", result)
            return {"execution": result,
                    "trace": _t("execute", f"{rec['action']} on {rec['target']}: {r.result}: {r.detail}"
                                           + (f" [{r.agent} agent]" if r.agent else "")
                                           + (f" (LLM: {r.llm_decision})" if r.llm_decision else ""))}
        if self.execution == "off":
            result = {"executed": False, "result": "not executed: observe-only project", "action": rec["action"]}
        else:
            result = {"executed": False, "result": "dry run: execution arrives in M6", "action": rec["action"]}
        self.memory.record(s["case_id"], "execution", result)
        return {"execution": result, "trace": _t("execute", f"{rec['action']} on {rec['target']}: {result['result']}")}

    def _escalate(self, s: State) -> State:
        ex = s.get("execution") or {}
        why = f"{ex['action']} {ex['result']}: {ex['detail']}" if ex.get("escalate") else s["route"]["reason"]
        self.notifier.send("on-call", s["case_id"], f"escalated: {why}. {s['diagnosis']['diagnosis']['summary'][:200]}")
        return {"trace": _t("escalate", f"on-call notified: {why}")}

    def _close(self, s: State) -> State:
        lane = s["route"]["lane"]
        if lane == "approval" and s.get("decision", {}).get("decision") == "reject":
            status = f"closed: rejected by {s['decision']['by']}"
        elif lane == "escalate":
            status = "escalated to on-call"
        elif lane == "none":
            status = "closed: no action needed"
        elif s["execution"].get("executed") is not None and "detail" in s["execution"]:     # the Execution agent ran
            ex = s["execution"]
            status = (f"closed: resolved by {ex['action']} (verified from telemetry)" if ex["result"] == "resolved"
                      else "closed: recovered by itself (no action taken)" if ex["result"] == "recovered"
                      else f"escalated to on-call: {ex['action']} {ex['result']}")
        else:
            status = f"closed: {s['execution']['result']}"
        short = {"closed: no action needed": "no action needed", "escalated to on-call": "escalated"}.get(status)
        if s.get("decision", {}).get("decision") == "reject":
            short = "rejected"
        short = short or (s.get("execution") or {}).get("result", status)
        self.memory.record(s["case_id"], "outcome", {"action": s["recommendation"]["action"], "lane": lane,
                                                      "result": short, "status": status})
        if not status.startswith("escalated"):
            self.notifier.send("team", s["case_id"], status)
        final = {**s, "status": status, "trace": s.get("trace", []) + _t("close", status)}
        self._update_ticket(final, status=status)
        return {"status": status, "trace": _t("close", status)}

    def _update_ticket(self, s: dict, status: str):
        self.tickets.update(s["case_id"], {**s, "status": status})

    # ---- graph ---------------------------------------------------------------------------------------------
    def _traced(self, name: str, fn):
        """Every workflow step goes into the case's trace (M7): its outcome, how long it took, or the error."""
        def node(s: State) -> State:
            with trace.for_case(s["case_id"]):
                t0 = time.monotonic()
                try:
                    out = fn(s)
                except GraphInterrupt:
                    trace.event("approval", "waiting", "approval card sent; the workflow is paused until a decision")
                    raise
                except Exception as e:
                    trace.event(name, "error", f"{type(e).__name__}: {e}", ms=round((time.monotonic() - t0) * 1000))
                    raise
                ms = round((time.monotonic() - t0) * 1000)
                for t in out.get("trace", []):
                    status = "ok"
                    if t["step"] == "execute" and (out.get("execution") or {}).get("escalate"):
                        status = "failed"
                    elif t["step"] == "investigate" and "investigation failed" in t["detail"]:
                        status = "failed"
                    elif t["step"] in ("escalate",) or (t["step"] == "route" and "escalate" in t["detail"][:9]):
                        status = "escalated"
                    trace.event(t["step"], status, t["detail"], ms=ms)
                return out
        return node

    def _build(self) -> StateGraph:
        g = StateGraph(State)
        for name in ("diagnose", "recommend", "route", "approval", "execute", "escalate", "close"):
            g.add_node(name, self._traced(name, getattr(self, f"_{name}")))
        g.add_edge(START, "diagnose")
        g.add_edge("diagnose", "recommend")
        g.add_edge("recommend", "route")
        g.add_conditional_edges("route", lambda s: s["route"]["lane"],
                                {"auto": "execute", "approval": "approval", "escalate": "escalate", "none": "close"})
        g.add_conditional_edges("approval", lambda s: "execute" if s["decision"]["decision"] == "approve" else "close",
                                {"execute": "execute", "close": "close"})
        g.add_conditional_edges("execute", lambda s: "escalate" if s["execution"].get("escalate") else "close",
                                {"escalate": "escalate", "close": "close"})
        g.add_edge("escalate", "close")
        g.add_edge("close", END)
        return g

    # ---- API -----------------------------------------------------------------------------------------------
    def _cfg(self, case_id: str) -> dict:
        return {"configurable": {"thread_id": case_id}}

    def start(self, case_id: str, case: Case) -> dict:
        """Run the workflow for a new incident, up to its end or the approval pause. A finished case, or one waiting
        for approval, is left as it is (e.g. when history is replayed). One that stopped part-way (Ctrl+C, a failed
        LLM call) continues from its last completed step."""
        snap = self.graph.get_state(self._cfg(case_id))
        if snap.values:
            if snap.next and not self.waiting_card(case_id):
                return self.resume(case_id)                       # continue from the checkpoint
            return self.state(case_id)
        trace.event("start", "ok", f"workflow started for {len(case.signals)} signal(s) on {', '.join(case.services)}",
                    case_id=case_id)
        self.graph.invoke({"case_id": case_id, "case": case.model_dump(mode="json"), "services": case.services,
                           "trace": _t("start", f"incident {case_id} on {', '.join(case.services)}")},
                          self._cfg(case_id))
        return self.state(case_id)

    def decide(self, case_id: str, decision: str, by: str, reason: str = "") -> dict:
        """Approve or reject a case waiting at the approval pause (works in a new process, after a restart)."""
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        if not by.strip():
            raise ValueError("an approver name is required (audit trail)")
        if decision == "reject" and not reason.strip():
            raise ValueError("a rejection needs a reason")
        if not self.waiting_card(case_id):
            raise ValueError(f"{case_id} is not waiting for approval")
        self.graph.invoke(Command(resume={"decision": decision, "by": by.strip(), "reason": reason.strip()}),
                          self._cfg(case_id))
        return self.state(case_id)

    def unfinished(self) -> list[str]:
        """Cases whose workflow stopped part-way (not finished, not waiting for approval)."""
        return [c for c in self.cases() if self.graph.get_state(self._cfg(c)).next and not self.waiting_card(c)]

    def resume(self, case_id: str) -> dict:
        self.graph.invoke(None, self._cfg(case_id))
        return self.state(case_id)

    def state(self, case_id: str) -> dict:
        snap = self.graph.get_state(self._cfg(case_id))
        card = self.waiting_card(case_id)
        return {**snap.values, "waiting": card is not None, "card": card,
                "status": "awaiting approval" if card else snap.values.get("status", "running")}

    def waiting_card(self, case_id: str) -> dict | None:
        snap = self.graph.get_state(self._cfg(case_id))
        for task in snap.tasks:
            for i in task.interrupts:
                return i.value
        return None

    def cases(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")]
