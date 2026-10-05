"""Copilot console (after M8, decided 29 Sep): a local web page on Python's built-in HTTP server, no new packages.
It reads the same files the copilot writes (memory, workflows, traces, signal log, LLM usage) and offers what the CLI
offers: incidents with their timeline, approval cards with Approve / Reject (same audit trail as `copilot approve`),
the latest signals, today's Gemini usage, and "Ask the copilot".

    python -m copilot console              # http://127.0.0.1:8765

Local only: it listens on 127.0.0.1, refuses requests for another host name, and only accepts JSON posts from its
own page, so another web site open in the browser can't press its buttons. Each connection has its own thread (an
idle connection, e.g. from VS Code's port forwarder, must not block the page: seen 1 Oct), but the copilot's data is
used by one request at a time: an approval that runs and verifies an action makes the others wait until it is done."""
import json, pathlib, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import trace, usage

PAGE = pathlib.Path(__file__).with_name("console.html")
SIGNAL_LOG = pathlib.Path("runs") / "signals"
NOTIFICATIONS = pathlib.Path("runs") / "notifications.jsonl"
JAEGER = "http://localhost:16686"


def _tail(path: pathlib.Path, n: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


class ConsoleData:
    """Everything the page shows, as plain dicts (tested without HTTP)."""

    def __init__(self, memory, flow, index=None, llm_factory=None, signal_log: pathlib.Path = SIGNAL_LOG,
                 notifications: pathlib.Path = NOTIFICATIONS):
        self.memory, self.flow, self.index, self.llm_factory = memory, flow, index, llm_factory
        self.signal_log, self.notifications = signal_log, notifications
        self._llm = None

    # ---- reading -----------------------------------------------------------------------------------------
    def incidents(self, limit: int = 100) -> list[dict]:
        flows = set(self.flow.cases()) if self.flow else set()
        out = []
        for r in self.memory.db.execute("SELECT case_id, first_seen, last_seen, services, root_service, all_clear "
                                        "FROM cases ORDER BY first_seen DESC LIMIT ?", (limit,)):
            row = {"case_id": r["case_id"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                   "services": json.loads(r["services"]), "root": r["root_service"], "closed": bool(r["all_clear"]),
                   "status": "closed" if r["all_clear"] else "open (no workflow yet)", "waiting": False}
            if r["case_id"] in flows:
                st = self.flow.state(r["case_id"])
                rec, route = st.get("recommendation") or {}, st.get("route") or {}
                row.update(status=st["status"], waiting=st["waiting"], lane=route.get("lane"), action=rec.get("action"),
                           target=rec.get("target"), confidence=rec.get("confidence"))
            out.append(row)
        return out

    def incident(self, case_id: str) -> dict | None:
        case = self.memory.get(case_id)
        if case is None:
            return None
        st = self.flow.state(case_id) if self.flow and case_id in self.flow.cases() else {}
        diag = st.get("diagnosis") or {}
        return {
            "case_id": case_id, "services": case.services, "root": case.root_service,
            "first_seen": case.first_seen.isoformat(), "last_seen": case.last_seen.isoformat(),
            "status": st.get("status", "no workflow (context only, or still settling)"),
            "signals": [{"at": s.timestamp.isoformat(), "severity": s.severity.value, "type": s.signal_type.value,
                         "service": s.service, "state": s.state, "title": s.title} for s in case.signals],
            "diagnosis": {"status": diag.get("status"), **(diag.get("diagnosis") or {}),
                          "removed": diag.get("removed", []), "sources": diag.get("sources", [])} if diag else None,
            "reused_from": (st.get("investigation") or {}).get("reused_from"),
            "recommendation": st.get("recommendation"), "route": st.get("route"), "execution": st.get("execution"),
            "decision": st.get("decision"), "card": st.get("card"),
            "timeline": st.get("trace", []), "trace": trace.read(case_id),
            "history": [{k: e[k] for k in e} for e in self.memory.events(case_id)],
            "jaeger": {svc: f"{JAEGER}/search?service={svc}" for svc in case.services},
        }

    def signals(self, n: int = 60) -> list[dict]:
        logs = sorted(self.signal_log.glob("*.jsonl")) if self.signal_log.exists() else []
        rows = _tail(logs[-1], n) if logs else []
        return [{"kind": r.get("kind"), "at": r.get("timestamp"), "severity": r.get("severity"), "type": r.get("signal_type"),
                 "service": r.get("service"), "state": r.get("state"), "title": r.get("title") or r.get("reason")}
                for r in reversed(rows)]

    def usage(self) -> dict:
        return {"today": usage.today(), "spent": usage.spent(), "budget": usage.budget()}

    def notifications_tail(self, n: int = 20) -> list[dict]:
        return list(reversed(_tail(self.notifications, n)))

    # ---- acting ------------------------------------------------------------------------------------------
    def decide(self, case_id: str, decision: str, by: str, reason: str = "") -> dict:
        """Same path as `copilot approve` / `reject`: validated by the workflow (name required, reason for a reject)."""
        if self.flow is None:
            raise ValueError("no workflow configured")
        st = self.flow.decide(case_id, decision, by=by, reason=reason)
        return {"status": st["status"], "timeline": st.get("trace", [])[-4:]}

    def ask(self, question: str) -> dict:
        from .ask import ask
        if self._llm is None and self.llm_factory is not None:
            try:
                self._llm = self.llm_factory()
            except Exception:
                self._llm = None
        incidents = [{"case_id": i["case_id"], "text": self._incident_text(i)} for i in self.incidents(30)]
        return ask(question, self.index, incidents, self._llm).model_dump()

    def _incident_text(self, i: dict) -> str:
        d = self.incident(i["case_id"]) or {}
        diag = d.get("diagnosis") or {}
        rec = d.get("recommendation") or {}
        lines = [f"incident {i['case_id']} on {', '.join(i['services'])} (origin {i['root'] or 'unknown'}), "
                 f"first seen {i['first_seen'][:16]}, status: {i['status']}"]
        lines += [f"signal: {s['severity']} {s['service']}: {s['title']}" for s in (d.get("signals") or [])[:8]]
        if diag.get("summary"):
            lines.append(f"diagnosis: {diag['summary'][:400]}")
        if rec:
            lines.append(f"recommended: {rec['action']} on {rec.get('target') or '-'} (blast radius {rec['blast_radius']}, "
                         f"confidence {rec['confidence']})")
        ex = d.get("execution") or {}
        if ex.get("detail") or ex.get("result"):
            lines.append(f"execution: {ex.get('result')}: {str(ex.get('detail') or '')[:300]}")
        for e in d.get("history", []):
            if e["kind"] in ("approval", "outcome"):
                lines.append(f"{e['kind']} at {e['at'][:16]}: " + ", ".join(f"{k}={v}" for k, v in e.items()
                                                                          if k not in ("kind", "at") and v))
        return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    data: ConsoleData = None            # set by serve()
    port: int = 8765
    timeout = 10                        # drop a connection that sends nothing
    lock = threading.Lock()             # memory and workflow: one request at a time

    def log_message(self, *a):          # quiet: the page polls every few seconds
        pass

    def _send(self, code: int, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "local use only"})
        path = self.path.split("?")[0]
        d = self.data
        if path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        with self.lock:
            return self._get(path, d)

    def _get(self, path, d):
        try:
            if path == "/api/overview":
                waiting = [i for i in d.incidents() if i["waiting"]]
                return self._send(200, {"incidents": d.incidents(), "waiting": waiting, "usage": d.usage(),
                                        "notifications": d.notifications_tail(), "now": datetime.now().isoformat()})
            if path == "/api/signals":
                return self._send(200, d.signals())
            if path.startswith("/api/incident/"):
                inc = d.incident(path.rsplit("/", 1)[1])
                return self._send(200, inc) if inc else self._send(404, {"error": "no such incident"})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        # only the console's own page: JSON (a cross-site form can't send it without the browser asking first),
        # and from this origin if the browser says where it came from
        # (same origin = the address the page was loaded from, whatever the port: VS Code's port forwarding from WSL
        # gives the browser a different port, seen 1 Oct)
        origin = self.headers.get("Origin")
        if not self._local():
            return self._send(403, {"error": "local use only (open the console at localhost or 127.0.0.1)"})
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self._send(403, {"error": f"local use only: request from another page ({origin})"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, {"error": "local use only: JSON requests from the console page"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad JSON"})
        with self.lock:
            return self._post(body)

    def _post(self, body):
        try:
            if self.path == "/api/decide":
                return self._send(200, self.data.decide(str(body.get("case_id", "")), str(body.get("decision", "")),
                                                        str(body.get("by", "")), str(body.get("reason", ""))))
            if self.path == "/api/ask":
                return self._send(200, self.data.ask(str(body.get("question", ""))))
            return self._send(404, {"error": "not found"})
        except ValueError as e:                  # refused by the workflow: no name, no reason, not waiting
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def make_server(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    Handler.port = httpd.server_address[1]
    return httpd


def serve(data: ConsoleData, port: int = 8765):
    Handler.data = data
    httpd = make_server(port)
    print(f"copilot console on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
