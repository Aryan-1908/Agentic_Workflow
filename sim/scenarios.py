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
