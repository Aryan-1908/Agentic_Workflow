"""The telemetry simulator: realistic OTel data for the Acme Shop, sent to the OTel Collector.

    python -m sim list                         # the scenarios
    python -m sim run db_down                  # 20 healthy minutes + the incident, timestamps ending now (instant)
    python -m sim run db_down --live           # the same in real time, one minute per --tick seconds; in live mode
                                               # it also applies the copilot's approved actions (runs/sim/actions.jsonl)
    python -m sim run db_down --file runs/otel/telemetry.jsonl   # write straight to the file, without the Collector
"""
import argparse, json, pathlib, sys, time

from .otlp import CollectorSink, FileSink
from .scenarios import SCENARIOS, WARMUP, timeline
from .world import TICK_NS, World


class ActionInbox:
    """Reads the copilot's action requests (runs/sim/actions.jsonl) that arrived since the simulator started."""

    def __init__(self, path: str = "runs/sim/actions.jsonl"):
        self.path = pathlib.Path(path)
        self.offset = self.path.stat().st_size if self.path.exists() else 0      # ignore requests from earlier runs

    def new(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            f.seek(self.offset)
            lines = f.readlines()
            self.offset = f.tell()
        return [json.loads(l) for l in lines if l.strip()]


def main():
    ap = argparse.ArgumentParser(prog="python -m sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("scenario", choices=list(SCENARIOS))
    r.add_argument("--minutes", type=int, default=12, help="simulated minutes after the incident starts")
    r.add_argument("--live", action="store_true", help="emit in real time instead of all at once")
    r.add_argument("--tick", type=float, default=60, help="--live: real seconds per simulated minute")
    r.add_argument("--file", help="write OTLP JSON to this file instead of sending to the Collector")
    r.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.cmd == "list":
        for name, (desc, use_case, _) in SCENARIOS.items():
            print(f"{name:<18} {use_case:<11} {desc}")
        return

    sink = FileSink(args.file) if args.file else CollectorSink()
    world = World(seed=args.seed)
    total, changes = timeline(args.scenario, args.minutes)
    start_ns = time.time_ns() - total * TICK_NS if not args.live else None
    print(f"{args.scenario}: {WARMUP} healthy minutes, then {args.minutes} with the incident "
          f"({'real time' if args.live else 'timestamps ending now'}) -> {args.file or sink.endpoint}")
    inbox = ActionInbox()
    try:
        for t in range(total):
            for change in changes.get(t, []):
                change(world)
                print(f"  minute {t}: {SCENARIOS[args.scenario][0]}")
            if args.live:                  # actions the copilot sent (copilot/cloud.py): applied like a cloud API would
                for req in inbox.new():
                    result = world.apply(req["id"], req["action"], req.get("params", {}), req.get("actor", "copilot"))
                    print(f"  minute {t}: applied {req['action']} {req.get('params', {})}: {result}")
            ts = time.time_ns() if args.live else start_ns + t * TICK_NS
            sink.send(world.tick(t, ts).documents())
            if args.live:
                time.sleep(args.tick)
    except OSError as e:
        sys.exit(f"could not reach the OTel Collector at {sink.endpoint} ({e}). Start it with:\n"
                 "  tools/otelcol-contrib --config otel/collector.yaml")
    print(f"done: {total} minutes of telemetry")


if __name__ == "__main__":
    main()
