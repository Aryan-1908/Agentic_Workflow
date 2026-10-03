"""Capacity / saturation probe set.

Separate from the M8 eval because it investigates one failure shape rather than covering the spec:
**nothing is broken, something is full**, and the pressure surfaces somewhere other than its cause.

The M8 scenarios are all "something broke" — a VM stopped, a process died, a disk filled, a release
regressed. For those, "the most upstream service in the dependency graph" is the right root-cause
rule. For saturation it can be wrong: a saturated pool makes its healthy downstream dependency look
like the culprit, and the graph will point straight at it.

Kept out of ground_truth.json so it does not inflate the spec's 15-20 incident count.
"""
import json
import pathlib

GROUND_TRUTH = pathlib.Path(__file__).with_name("ground_truth.json")

CAPACITY_SCENARIOS = {
    "pool_exhaustion",            # orders-api out of workers, database silent
    "pool_exhaustion_db_noisy",   # ... and the database looks slow too (the hard one)
    "storefront_saturated",       # tier-1 edge service, own thread pool full
    "batch_saturated",            # same shape, tier-3 internal service
    "db_connections_full",        # the data service itself is out of connection slots
    "cpu_vs_capacity",            # high CPU AND a saturated pool: genuinely ambiguous
}


def ground_truth() -> list[dict]:
    return json.loads(GROUND_TRUTH.read_text())["scenarios"]
