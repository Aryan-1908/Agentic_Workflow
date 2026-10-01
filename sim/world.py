"""The simulated Acme Shop: its services, hosts and state, and the telemetry they emit each simulated minute.

    storefront (web-01) ──calls──▶ orders-api (orders-01) ──queries──▶ orders-db (db-01, PostgreSQL)
          └────────────calls──▶ payments-provider (external, has a public status page)
    reports-batch (batch-01) ──queries──▶ orders-db

Each tick emits, as OTLP (see otel/README.md for the contract):
  metrics  CPU and disk utilization of every running host
  traces   checkout requests through the call chain, failing where the state says they should
  logs     application errors, infrastructure events (VM stopped/started, deploys, firewall changes), alerts from a
           small monitoring rule engine (like Prometheus/Alertmanager exporting to OTel), status-page updates
Scenarios (sim/scenarios.py) change the state at chosen ticks; M6 actions will change it too."""
import hashlib, random, time
from dataclasses import dataclass, field

from . import otlp
from .otlp import ERROR, FATAL, INFO, SPAN_CLIENT, SPAN_SERVER, WARN

HOSTS = {"web-01": "storefront", "orders-01": "orders-api", "db-01": "orders-db", "batch-01": "reports-batch"}
REQUESTS_PER_TICK = 12
TICK_NS = 60 * 10**9


def _id(*parts, n=16) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:n]


@dataclass
class Rule:
    """A monitoring alert rule: fires after `for_ticks` consecutive bad ticks, resolves after 2 good ones."""
    name: str
    service: str
    host: str | None
    severity: str
    for_ticks: int = 2
    bad: int = 0
    good: int = 0
    firing: bool = False
    fired: int = 0


@dataclass
class World:
    seed: int = 7
    running: dict = field(default_factory=lambda: {h: True for h in HOSTS})
    process_up: dict = field(default_factory=lambda: {h: True for h in HOSTS})
    cpu: dict = field(default_factory=lambda: {"web-01": 0.32, "orders-01": 0.28, "db-01": 0.41, "batch-01": 0.18})
    disk: dict = field(default_factory=lambda: {"web-01": 0.46, "orders-01": 0.38, "db-01": 0.62, "batch-01": 0.51})
    disk_growth: dict = field(default_factory=dict)          # host -> utilization added per tick
    cpu_override: dict = field(default_factory=dict)         # host -> forced utilization (runaway process, traffic)
    release: str = "2026.09.3"
    bad_release: bool = False
    firewall_ok: bool = True
    payfast: dict = field(default_factory=lambda: {"Card Authorization API": "operational", "Webhooks": "operational"})
    extra_errors: list = field(default_factory=list)         # (service, host, logger, message) per tick: e.g. unknown services
    pending: list = field(default_factory=list)              # one-off log events to emit on the next tick
    flapping: dict = field(default_factory=dict)             # host -> minutes after a restart until the problem returns
    _flap_timer: dict = field(default_factory=dict)

    run: str = ""        # makes every id unique to this run; real telemetry never repeats ids across days

    def __post_init__(self):
        self.run = self.run or str(time.time_ns())
        self.rng = random.Random(self.seed)
        self.rules = [Rule("storefront uptime", "storefront", "web-01", "critical"),
                      Rule("storefront checkout error rate > 5%", "storefront", "web-01", "error"),
                      Rule("orders-api health", "orders-api", "orders-01", "critical"),
                      *[Rule("VM not reporting", svc, h, "critical", for_ticks=1) for h, svc in HOSTS.items()],
                      *[Rule("CPU > 85% for 5 min", svc, h, "warning", for_ticks=5) for h, svc in HOSTS.items()],
                      *[Rule("disk > 90%", svc, h, "error", for_ticks=1) for h, svc in HOSTS.items()]]

    # ---- state changes (used by scenarios, later by actions) --------------------------------------------
    def event(self, host: str | None, service: str, name: str, body: str, severity=INFO, **attrs):
        self.pending.append((host, service, name, body, severity, attrs))

    def stop_vm(self, host, actor="alice@example.com", scheduled=False):
        self.running[host] = False
        name = "vm.stopped_by_schedule" if scheduled else "vm.stopped"
        self.event(host, HOSTS[host], name, f"{host}: VM stopped" + (" by its instance schedule" if scheduled else f" by {actor}"),
                   actor="instance-schedule" if scheduled else actor)

    def start_vm(self, host, actor="bob@example.com", agent_noise=True):
        self.running[host] = True
        self.process_up[host] = True
        self.event(host, HOSTS[host], "vm.started", f"{host}: VM started by {actor}", actor=actor)
        if agent_noise:     # the guest agent's stale-user errors seen on the real project
            for user in ("sachin_r", "neha_c", "priya_k"):
                self.pending.append((host, HOSTS[host], None, "error setting initial metadatasshkey configuration: "
                                     f"failed to remove user {user} from google-sudoers", ERROR, {"logger": "GCEGuestAgent"}))

    # ---- actions from the copilot (the simulated control API; see copilot/cloud.py) --------------------------
    def apply(self, request_id: str, action: str, params: dict, actor: str = "copilot-executor") -> str:
        """Apply one action to the shop and emit `remediation.applied` (with the request id) on the next tick.
        Returns "ok" or the reason it couldn't be applied, as a real API would."""
        vm, svc = params.get("vm"), params.get("service")
        result = "ok"
        if vm is not None and vm not in HOSTS:
            result = f"unknown VM {vm}"
        elif action == "vm.start":
            self.running[vm] = True
            self.process_up[vm] = True
        elif action == "vm.reset":
            self.running[vm], self.process_up[vm] = True, True
            self.cpu_override.pop(vm, None)
        elif action in ("service.restart", "mig.recreate_instance"):
            if not self.running[vm]:
                result = f"{vm} is not running"
            else:
                self.process_up[vm] = True
                self.cpu_override.pop(vm, None)          # a stuck process is gone after a restart
                if vm in self.flapping:                  # ... but this one comes back
                    self._flap_timer[vm] = self.flapping[vm]
        elif action == "logs.rotate":
            self.disk[vm] = max(0.2, self.disk[vm] - 0.35)
            self.disk_growth.pop(vm, None)
        elif action == "vm.resize":
            if vm in self.cpu_override:
                self.cpu_override[vm] = round(self.cpu_override[vm] / float(params.get("size", 2)), 3)
        elif action == "mig.rollback":
            if svc != "storefront" or not self.bad_release:
                result = f"no previous release to roll back to for {svc}"
            else:
                self.bad_release, self.release = False, "2026.09.3"
        elif action == "mig.resize":
            host = next((h for h, s in HOSTS.items() if s == svc), None)
            if host in self.cpu_override:
                self.cpu_override[host] = round(self.cpu_override[host] / 2, 3)
        elif action == "firewall.restore":
            if params.get("rule") != "allow-storefront-http":
                result = f"no deleted rule named {params.get('rule')}"
            else:
                self.firewall_ok = True
        else:
            result = f"action {action} is not supported by this environment"
        target = vm or svc or params.get("rule")
        self.event(vm if vm in HOSTS else None, HOSTS.get(vm, svc or "infrastructure"), "remediation.applied",
                   f"{action} on {target}: {result}", INFO if result == "ok" else WARN,
                   **{"request.id": request_id, "action": action, "target": target, "result": result, "actor": actor})
        return result

    # ---- one simulated minute ----------------------------------------------------------------------------
    def tick(self, t: int, ts_ns: int) -> otlp.Batch:
        b = otlp.Batch()
        res = {h: otlp.resource(svc, h) for h, svc in HOSTS.items()}
        for host in list(self._flap_timer):              # a flapping problem returns after its delay
            self._flap_timer[host] -= 1
            if self._flap_timer[host] <= 0:
                del self._flap_timer[host]
                self.cpu_override[host] = 0.98
        pay_res = otlp.resource("payments-provider", None, **{"provider.status_url": "https://status.payfast.example"})

        for host, service, name, body, sev, a in self.pending:
            r = res.get(host) or otlp.resource(service, host)
            scope = a.pop("logger", "infrastructure")
            b.log(r, scope, otlp.log_record(ts_ns, sev, body, _id(self.run, "ev", t, host, body), **({"event.name": name} if name else {}), **a))
        self.pending = []

        # metrics
        for h in HOSTS:
            if not self.running[h]:
                continue
            self.disk[h] = min(0.999, self.disk[h] + self.disk_growth.get(h, 0))
            cpu = self.cpu_override.get(h, self.cpu[h] + self.rng.uniform(-0.04, 0.04))
            b.metric(res[h], "hostmetrics", otlp.gauge("system.cpu.utilization", "1", [(ts_ns, round(cpu, 3), {})]))
            b.metric(res[h], "hostmetrics", otlp.gauge("system.filesystem.utilization", "1",
                                                        [(ts_ns, round(self.disk[h], 3), {"mountpoint": "/data" if h == "db-01" else "/"})]))

        # traces: checkout requests through the chain
        web_up = self.running["web-01"] and self.process_up["web-01"]
        reachable = web_up and self.firewall_ok
        db_ok = self.running["db-01"] and self.process_up["db-01"]
        db_write_ok = db_ok and self.disk["db-01"] < 0.98
        orders_ok = self.running["orders-01"] and self.process_up["orders-01"]
        pay_ok = self.payfast["Card Authorization API"] == "operational"
        failed_checkouts, errors = 0, {}
        for i in range(REQUESTS_PER_TICK if reachable else 0):
            tid, start = _id(self.run, "tr", t, i, n=32), ts_ns + i * 4 * 10**9
            root = _id(self.run, "sp", t, i, "root")
            err = None
            # storefront -> orders-api -> orders-db
            o_err = None if orders_ok else "connect ECONNREFUSED orders-01:8080"
            if orders_ok:
                srv, dbs = _id(self.run, "sp", t, i, "o"), _id(self.run, "sp", t, i, "db")
                db_err = None if db_write_ok else ("connection refused: db-01:5432" if not db_ok
                                                   else "could not extend file: No space left on device")
                b.span(res["orders-01"], "orders-api", otlp.span(tid, srv, _id(self.run, "sp", t, i, "oc"), "POST /orders", SPAN_SERVER,
                       start + 20_000_000, start + 140_000_000, db_err and f"database error: {db_err}"))
                b.span(res["orders-01"], "orders-api", otlp.span(tid, dbs, srv, "INSERT orders", SPAN_CLIENT,
                       start + 30_000_000, start + 120_000_000, db_err, **{"db.system": "postgresql", "server.address": "db-01"}))
                if db_err:
                    o_err = f"orders-api 503: {db_err}"
                    errors[("orders-api", "orders-01", "orders.db", f"database error: {db_err}")] = 1
            b.span(res["web-01"], "storefront", otlp.span(tid, _id(self.run, "sp", t, i, "oc"), root, "POST orders-api/orders", SPAN_CLIENT,
                   start + 10_000_000, start + 150_000_000, o_err, **{"peer.service": "orders-api"}))
            # storefront -> payments-provider
            p_err = None if pay_ok else "PayFast authorization unavailable (503)"
            b.span(res["web-01"], "storefront", otlp.span(tid, _id(self.run, "sp", t, i, "pay"), root, "POST payfast/authorize", SPAN_CLIENT,
                   start + 160_000_000, start + 260_000_000, p_err, **{"peer.service": "payments-provider"}))
            release_err = "KeyError: 'payment_client.api_key'" if self.bad_release and i % 3 == 0 else None
            err = o_err or p_err or release_err
            b.span(res["web-01"], "storefront", otlp.span(tid, root, None, "POST /checkout", SPAN_SERVER, start, start + 300_000_000,
                   err and f"checkout failed: {err}", **{"http.route": "/checkout", "service.version": self.release}))
            if err:
                failed_checkouts += 1
                errors[("storefront", "web-01", "storefront.checkout", f"checkout failed: {err}")] = 1

        for (svc, host, logger, msg) in list(errors) + self.extra_errors:
            r = res.get(host) or otlp.resource(svc, host)
            b.log(r, logger, otlp.log_record(ts_ns + 290 * 10**9 // 10, ERROR, msg, _id(self.run, "log", t, svc, msg)))

        # status page of the external provider
        for comp, status in self.payfast.items():
            if status != "operational":
                b.log(pay_res, "statuspage", otlp.log_record(ts_ns, WARN, f"PayFast status: {comp} {status.replace('_', ' ')}",
                      _id(self.run, "status", comp, status), **{"event.name": "status_page.component", "provider": "payments-provider",
                                                      "component": comp, "status": status}))

        # monitoring rules -> alerts
        checkout_error_rate = failed_checkouts / REQUESTS_PER_TICK if reachable else 0
        for rule in self.rules:
            h = rule.host
            bad = {"storefront uptime": not reachable,
                   "storefront checkout error rate > 5%": checkout_error_rate > 0.05,
                   "orders-api health": orders_ok and not db_ok,
                   "VM not reporting": not self.running.get(h, True),
                   "CPU > 85% for 5 min": self.running.get(h, False) and self.cpu_override.get(h, 0) > 0.85,
                   "disk > 90%": self.running.get(h, False) and self.disk.get(h, 0) > 0.9}[rule.name]
            self._evaluate(rule, bad, b, ts_ns, res.get(h) or otlp.resource(rule.service, h))
        return b

    def _evaluate(self, rule: Rule, bad: bool, b: otlp.Batch, ts_ns: int, r: dict):
        rule.bad, rule.good = (rule.bad + 1, 0) if bad else (0, rule.good + 1)
        state = None
        if not rule.firing and rule.bad >= rule.for_ticks:
            rule.firing, rule.fired, state = True, rule.fired + 1, "firing"
        elif rule.firing and rule.good >= 2:
            rule.firing, state = False, "resolved"
        if state:
            alert_id = _id(self.run, "alert", rule.name, rule.host, rule.fired, n=12)
            b.log(r, "alertmanager", otlp.log_record(
                ts_ns, (FATAL if rule.severity == "critical" else ERROR) if state == "firing" else INFO,
                f"[{state.upper()}] {rule.name}" + (f" on {rule.host}" if rule.host else ""), _id("alert", alert_id, state),
                **{"event.name": "alert", "alert.id": alert_id, "alert.name": rule.name, "alert.state": state,
                   "alert.severity": rule.severity}))
