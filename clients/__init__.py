"""One TOML file per client (an environment the copilot watches). With OpenTelemetry as the single input, a client
is just: where its telemetry lands, its service map, its routing thresholds, and whether actions may run."""
import os, pathlib, tomllib
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass
class Client:
    name: str
    telemetry: str = "runs/otel/telemetry.jsonl"     # the OTel Collector's file exporter output (otel/collector.yaml)
    services: str | None = None                      # service map (default: config/services.toml)
    routing: dict = field(default_factory=dict)      # thresholds for auto / approval / escalate (copilot/routing.py)
    execution: str = "simulated"                     # off | dry-run | simulated (actions applied to the simulator)


def load_client(name: str) -> Client:
    path = pathlib.Path(__file__).parent / f"{name}.toml"
    if not path.exists():
        raise SystemExit(f"no client config {path}")
    c = Client(**tomllib.loads(path.read_text()))
    if c.execution not in ("off", "dry-run", "simulated"):
        raise SystemExit(f"{path}: execution must be off, dry-run or simulated")
    if c.services:
        os.environ["COPILOT_SERVICES"] = str(ROOT / c.services)    # the copilot's service lookups read this
    return c
