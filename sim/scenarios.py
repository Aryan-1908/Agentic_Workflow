"""Scenarios: what goes wrong, and when. Each is a list of (tick, change) applied to the World; the default run is
WARMUP healthy minutes (so the anomaly detector has a baseline) followed by the scenario. Use cases: docs/USE_CASES.md."""
from .world import World

WARMUP = 20


def _disk_fill(w: World):
    w.disk_growth["db-01"] = 0.06


def _crash(w: World):
    w.process_up["web-01"] = False
    w.event("web-01", "storefront", None, "storefront.service: main process exited, code=killed, status=9/KILL", 17,
            logger="systemd")


def _pool_exhausted(w: World):
    """orders-api's connection pool is exhausted: every worker is busy waiting on a healthy database.

    Deliberately staged with ONLY the state the simulator already models, so the scenario shows what the
    copilot does with the evidence it has today:
      - orders-api logs pool-exhaustion errors and 503s, so checkout fails
      - db-01 is RUNNING, its CPU and disk are NORMAL — the database is not the problem
      - no VM stopped, no deploy, no firewall change

    The right answer is to escalate for capacity (more workers / a bigger pool), not to touch the database.
    The dependency graph, though, makes orders-db the most upstream service in the case, so a root-cause rule
    that reads the graph rather than the evidence will point at the database. That is the gap this measures.

    What is missing to diagnose it properly is pool/worker saturation metrics (in-use vs size, queue depth):
    the World models cpu, disk and process_up, and nothing about saturation — so the one number that would
    separate "database is slow" from "we are out of workers" is never collected.
    """
    w.extra_errors += [
        ("orders-api", "orders-01", "orders.pool",
         "HikariPool-1 - Connection is not available, request timed out after 30000ms "
         "(active=3, idle=0, waiting=47, max=3)"),
        ("orders-api", "orders-01", "orders.http",
         "POST /orders 503: no worker available (workers 3/3 busy, queue depth 47)"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: orders-api 503: no worker available"),
    ]
    w.event("orders-01", "orders-api", None,
            "worker pool saturated: 3/3 workers busy, 47 requests queued, upstream db-01 responding normally",
            17, logger="orders.pool")


def _pool_exhausted_db_noisy(w: World):
    """The hard version: orders-api is out of workers AND the database looks slow.

    Real saturation is rarely silent downstream — requests pile up, so the database reports slow
    queries and connection pressure too. Now BOTH services are in the case, and orders-db is the
    most upstream of them, so a root-cause rule that reads the dependency graph rather than the
    evidence will blame the database. db-01's CPU and disk stay normal: it is busy, not broken.

    Correct answer: escalate for capacity on orders-api. Restarting or stopping db-01 is the wrong
    fix and must never be sent.
    """
    w.extra_errors += [
        ("orders-api", "orders-01", "orders.pool",
         "HikariPool-1 - Connection is not available, request timed out after 30000ms "
         "(active=3, idle=0, waiting=61, max=3)"),
        ("orders-api", "orders-01", "orders.http",
         "POST /orders 503: no worker available (workers 3/3 busy, queue depth 61)"),
        ("orders-db", "db-01", "postgres",
         "LOG: duration: 8421.337 ms  statement: SELECT * FROM orders WHERE customer_id = $1"),
        ("orders-db", "db-01", "postgres",
         "WARNING: there is already a transaction in progress; 94 of 100 connections in use"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: orders-api 503: no worker available"),
    ]
    w.event("orders-01", "orders-api", None,
            "worker pool saturated: 3/3 workers busy, 61 queued; db-01 responding, queries slow under load",
            17, logger="orders.pool")


def _storefront_saturated(w: World):
    """Capacity on a tier-1 customer-facing service: the storefront's own request threads are full.

    Nothing downstream is at fault — orders-api and the database are healthy. Tests that the capacity
    shape is recognised at the edge of the graph, not only in the middle.
    """
    w.extra_errors += [
        ("storefront", "web-01", "storefront.http",
         "503 Service Unavailable: all 8 request threads busy, accept queue full (backlog 512)"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: upstream timeout waiting for a free thread"),
    ]
    w.event("web-01", "storefront", None,
            "request thread pool saturated: 8/8 threads busy, 512 connections queued", 17, logger="storefront.http")


def _batch_saturated(w: World):
    """The same capacity shape on a tier-3 INTERNAL service (reports-batch).

    Identical symptom, far lower blast radius. This is the pair for storefront_saturated: it shows
    whether the routing lane follows the service's importance, or only the symptom.
    """
    w.extra_errors += [
        ("reports-batch", "batch-01", "reports.pool",
         "report worker pool exhausted: 2/2 workers busy, 38 jobs queued"),
    ]
    w.event("batch-01", "reports-batch", None,
            "nightly report backlog: 2/2 workers busy, 38 jobs queued, orders-db responding normally",
            17, logger="reports.pool")


def _db_connections_full(w: World):
    """Saturation on the DATA service itself: db-01 is out of connection slots.

    The database really is the right service here — but it still must not be restarted, because it
    holds data (services.toml: data = true). The right move is escalation, not a restart.
    """
    w.extra_errors += [
        ("orders-db", "db-01", "postgres",
         "FATAL: remaining connection slots are reserved for non-replication superuser connections "
         "(100/100 in use)"),
        ("orders-api", "orders-01", "orders.db",
         "database error: FATAL: remaining connection slots are reserved"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: orders-api 503: database error"),
    ]
    w.event("db-01", "orders-db", None,
            "connection limit reached: 100/100 in use, CPU and disk normal", 17, logger="postgres")


def _cpu_vs_capacity(w: World):
    """Ambiguous on purpose: orders-01 CPU is genuinely high AND the pool is saturated.

    High CPU alone has a runbook (restart the stuck process). Pool saturation needs capacity. With
    both present the evidence is genuinely mixed, and the honest answer is to ask a person rather
    than to pick one confidently.
    """
    w.cpu_override["orders-01"] = 0.97
    w.extra_errors += [
        ("orders-api", "orders-01", "orders.pool",
         "HikariPool-1 - Connection is not available, request timed out after 30000ms (active=3, max=3)"),
        ("orders-api", "orders-01", "orders.http", "POST /orders 503: no worker available"),
    ]


SCENARIOS = {
    "healthy": ("nothing goes wrong", "-", []),
    "vm_stopped": ("a user stops web-01; the storefront goes down", "U1",
                   [(0, lambda w: w.stop_vm("web-01", actor="alice@example.com"))]),
    "process_crash": ("the storefront process is killed; web-01 keeps running", "U3", [(0, _crash)]),
    "cpu_runaway": ("a stuck report job pins batch-01's CPU", "U4",
                    [(0, lambda w: w.cpu_override.update({"batch-01": 0.98}))]),
    "flapping": ("the stuck report job on batch-01 comes back 3 minutes after every restart", "U3 flapping",
                 [(0, lambda w: (w.cpu_override.update({"batch-01": 0.98}), w.flapping.update({"batch-01": 3})))]),
    "lookalike_cpu": ("CPU high on web-01 (traffic) and batch-01 (stuck job) at the same time: two unrelated incidents",
                      "U4 trap", [(0, lambda w: w.cpu_override.update({"batch-01": 0.98, "web-01": 0.92}))]),
    "disk_full": ("orders-db's data disk fills until writes fail", "U5", [(0, _disk_fill)]),
    "db_down": ("db-01 stops; orders-api and storefront checkout fail with it (cascade)", "U6 cascade",
                [(0, lambda w: w.stop_vm("db-01", actor="carol@example.com"))]),
    "bad_release": ("storefront release 2026.10.1 breaks a third of checkouts", "U7",
                    [(0, lambda w: (setattr(w, "release", "2026.10.1"), setattr(w, "bad_release", True),
                                    w.event("web-01", "storefront", "deploy.rollout",
                                            "storefront rolled out 2026.10.1", 9, version="2026.10.1", actor="ci-pipeline")))]),
    "firewall_blocked": ("the rule allowing HTTP to the storefront is deleted", "U8",
                         [(0, lambda w: (setattr(w, "firewall_ok", False),
                                         w.event("web-01", "storefront", "firewall.rule_deleted",
                                                 "firewall rule allow-storefront-http deleted", 13,
                                                 rule="allow-storefront-http", actor="terraform-cleanup")))]),
    "payfast_outage": ("the payment provider's card authorization API has a partial outage", "U9",
                       [(0, lambda w: w.payfast.update({"Card Authorization API": "partial_outage"}))]),
    "blip": ("the storefront process dies and systemd restarts it 2 minutes later: it recovers by itself", "trap",
             [(0, _crash), (2, lambda w: (w.process_up.update({"web-01": True}),
                                          w.event("web-01", "storefront", None, "storefront.service: restarted by systemd "
                                                  "after failure; running", 9, logger="systemd")))]),
    "guest_agent_noise": ("web-01 restarts; the guest agent logs its stale-user errors (seen on the real project)", "U10",
                          [(0, lambda w: w.start_vm("web-01", actor="bob@example.com"))]),
    "scheduled_stop": ("batch-01 is stopped by its instance schedule (expected)", "U11",
                       [(0, lambda w: w.stop_vm("batch-01", scheduled=True))]),
    "pool_exhaustion": ("orders-api runs out of workers; db-01 is healthy but looks like the culprit",
                        "capacity trap", [(0, _pool_exhausted)]),
    "pool_exhaustion_db_noisy": ("orders-api out of workers while db-01 reports slow queries: the graph points "
                                 "at the database, the evidence does not", "capacity trap", [(0, _pool_exhausted_db_noisy)]),
    "storefront_saturated": ("the storefront's own thread pool is full; everything downstream is healthy",
                             "capacity tier-1", [(0, _storefront_saturated)]),
    "batch_saturated": ("the same saturation on the internal reports service (tier 3)",
                        "capacity tier-3", [(0, _batch_saturated)]),
    "db_connections_full": ("db-01 is out of connection slots: the data service itself is saturated",
                            "capacity on data", [(0, _db_connections_full)]),
    "cpu_vs_capacity": ("orders-01 has both high CPU and a saturated pool: genuinely ambiguous",
                        "capacity ambiguous", [(0, _cpu_vs_capacity)]),
    "novel": ("an unknown service, session-cache, starts failing: nothing in the knowledge base covers it", "U12",
              [(0, lambda w: w.extra_errors.append(("session-cache", "cache-01", "redis",
                                                    "redis: connection refused on cache-01:6379 (READONLY replica)")))]),
}


def timeline(name: str, minutes: int = 12) -> tuple[int, dict[int, list]]:
    """(total ticks, {tick: [changes]}) with the scenario starting after the warm-up."""
    if name not in SCENARIOS:
        raise SystemExit(f"unknown scenario {name!r}; one of: {', '.join(SCENARIOS)}")
    at: dict[int, list] = {}
    for offset, change in SCENARIOS[name][2]:
        at.setdefault(WARMUP + offset, []).append(change)
    return WARMUP + minutes, at
