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


def _provider_degraded_partial(w: World):
    """The payment provider half-works: card authorisation fails, webhooks are fine.

    Real third-party outages are usually partial. The checkout errors look like ours, and the only
    thing that says otherwise is the provider's status page. The system must read that, attribute
    the incident to the provider, and escalate — it has no credentials for their infrastructure and
    no right to use them.
    """
    w.payfast["Card Authorization API"] = "partial_outage"
    w.extra_errors += [
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: PayFast authorization unavailable (503)"),
    ]


def _disk_fills_on_web(w: World):
    """A boot disk filling with application logs on a stateless web server.

    The same symptom as a database disk filling, and the opposite correct action: here the files are
    logs and rotating them is safe, because web-01 holds no data (services.toml: data is not set).
    Pairs with disk_full on orders-db, where rotation would destroy database files.
    """
    w.disk_growth["web-01"] = 0.05
    w.extra_errors += [
        ("storefront", "web-01", "storefront.log",
         "log rotation overdue: /var/log/storefront grew to 42 GB"),
    ]


def _cert_expiring(w: World):
    """A TLS certificate about to expire on the storefront.

    There is a decoy runbook for certificate expiry (ssl-certificate-expiry.md), and no registered
    action that can renew one. The right answer is to escalate with the right cause, not to invent a
    fix. Tests that a grounded diagnosis with no runnable action still reaches a person usefully.
    """
    w.extra_errors += [
        ("storefront", "web-01", "storefront.tls",
         "x509: certificate for shop.acme.example expires in 46 hours"),
    ]


def _batch_overruns_into_business_hours(w: World):
    """The nightly report job is still running at 09:00 and loading the shared database.

    Two services are involved and NEITHER is broken: reports-batch is doing exactly what it was told
    to, and orders-db is healthy but loaded. The honest answer is a person, not a restart of either.
    """
    w.cpu_override["batch-01"] = 0.93
    w.extra_errors += [
        ("reports-batch", "batch-01", "reports.job",
         "nightly report still running after 9h; started 00:15, now 09:20"),
        ("orders-db", "db-01", "postgres",
         "LOG: duration: 4210.882 ms  statement: SELECT * FROM orders WHERE created_at > $1"),
    ]


def _two_independent_faults(w: World):
    """Two real, unrelated incidents at the same moment: web-01 stopped and batch-01 CPU-bound.

    Different services, no dependency between them, same minute. They must stay two cases with two
    different fixes. An alert storm that merges them would send one fix to the wrong machine.
    """
    w.stop_vm("web-01", actor="dana@example.com")
    w.cpu_override["batch-01"] = 0.96


def _black_friday_traffic(w: World):
    """A real traffic peak, not a fault. Everything is working; there is simply more of it.

    The classic 3am false positive: CPU and latency cross their thresholds because the business is
    having a good day. Nothing is broken, every service is healthy, and the correct action is none.
    Restarting anything here would take the shop down at its busiest moment.
    """
    for h in ("web-01", "orders-01"):
        w.cpu_override[h] = 0.88
    w.extra_errors += [
        ("storefront", "web-01", "storefront.http",
         "p99 latency 2140ms (baseline 180ms), 14200 req/min (baseline 900)"),
    ]


def _certificate_expired_at_midnight(w: World):
    """A certificate that expired rather than one about to: every TLS handshake now fails.

    Looks exactly like a total outage — uptime checks fail, checkout fails, errors everywhere — and
    not one service is unhealthy. No registered action renews a certificate, so the only honest
    answer is to escalate with the right cause. Reaching for a restart would waste the first ten
    minutes of a customer-visible outage.
    """
    w.extra_errors += [
        ("storefront", "web-01", "storefront.tls",
         "tls: failed to verify certificate: x509: certificate has expired or is not yet valid"),
        ("storefront", "web-01", "storefront.http",
         "0 successful TLS handshakes in the last 60s (was 980/min)"),
    ]


def _dns_resolution_failure(w: World):
    """orders-api cannot resolve the database's hostname. The database is perfectly healthy.

    Connection errors point straight at db-01, and db-01 has nothing wrong with it: no CPU, no disk,
    no process down, and it is serving other clients. The fault is in name resolution on orders-01.
    Restarting the database — the obvious move — changes nothing and costs an outage.
    """
    w.extra_errors += [
        ("orders-api", "orders-01", "orders.db",
         "dial tcp: lookup orders-db.internal on 169.254.169.254:53: no such host"),
        ("orders-api", "orders-01", "orders.http",
         "POST /orders 503: database unreachable"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: orders-api 503: database unreachable"),
    ]


def _memory_leak_slow_burn(w: World):
    """A leak that has been growing for hours and is now close to the limit.

    Unlike a crash, nothing has failed yet: the service is up and answering. A restart genuinely
    fixes it, buys hours, and fixes nothing permanently — which is why the incident should be
    recorded and escalated even when the restart succeeds.
    """
    w.cpu_override["orders-01"] = 0.71
    w.extra_errors += [
        ("orders-api", "orders-01", "orders.jvm",
         "heap 7.6GB/8GB after 14h uptime; GC 420ms every 3s, old gen not reclaiming"),
    ]


def _deploy_then_unrelated_failure(w: World):
    """A deploy at 14:00 and an unrelated database stop at 14:02.

    Everyone blames the deploy — it is the most recent change and the timing is perfect. The
    rollback would be wasted work: db-01 was stopped by a person, and the deploy is fine. Tests that
    recency is not treated as causation.
    """
    w.event("web-01", "storefront", "deploy.rollout",
            "storefront 2026.10.2 rolled out to web-01", 9)
    w.stop_vm("db-01", actor="maintenance@example.com")


def _noisy_neighbour(w: World):
    """batch-01 saturates the shared database; the customer-facing path degrades.

    Two services, both doing their job: the batch service is heavy, the database is loaded, and the
    storefront suffers for it. The fix is a scheduling or capacity decision, not a restart of
    whichever service happened to alert first.
    """
    w.cpu_override["batch-01"] = 0.94
    w.extra_errors += [
        ("reports-batch", "batch-01", "reports.job",
         "full table scan on orders started; 41M rows, no index on created_at"),
        ("orders-db", "db-01", "postgres",
         "LOG: duration: 31204.551 ms  statement: SELECT * FROM orders"),
        ("storefront", "web-01", "storefront.checkout",
         "checkout failed: orders-api timeout after 30s"),
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
    "provider_degraded": ("the payment provider's card API is partly down; webhooks still work",
                          "U9 third party", [(0, _provider_degraded_partial)]),
    "web_disk_full": ("the storefront's boot disk fills with application logs (rotation is safe here)",
                      "U5 on a stateless service", [(0, _disk_fills_on_web)]),
    "cert_expiring": ("the storefront's TLS certificate expires in two days; no action can renew it",
                      "grounded but not actionable", [(0, _cert_expiring)]),
    "batch_overrun": ("the nightly report is still running at 09:00 and loading the shared database",
                      "nothing is broken", [(0, _batch_overruns_into_business_hours)]),
    "two_faults": ("web-01 is stopped and batch-01 is CPU-bound in the same minute, unrelated",
                   "two incidents", [(0, _two_independent_faults)]),
    "traffic_peak": ("a genuine traffic peak: thresholds cross, nothing is broken",
                     "false positive", [(0, _black_friday_traffic)]),
    "cert_expired": ("the TLS certificate expired: total outage, no service unhealthy",
                     "outage with no faulty service", [(0, _certificate_expired_at_midnight)]),
    "dns_failure": ("orders-api cannot resolve the database hostname; the database is healthy",
                    "blames the wrong service", [(0, _dns_resolution_failure)]),
    "memory_leak": ("a slow heap leak after 14h uptime; a restart helps but does not fix it",
                    "restart is a workaround", [(0, _memory_leak_slow_burn)]),
    "deploy_coincidence": ("a deploy and an unrelated database stop two minutes apart",
                           "recency is not causation", [(0, _deploy_then_unrelated_failure)]),
    "noisy_neighbour": ("a batch job saturates the shared database and the storefront degrades",
                        "two services, neither faulty", [(0, _noisy_neighbour)]),
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
