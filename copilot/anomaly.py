"""Anomaly detector (spec: "anomalies" as a signal source). Flags a metric that leaves its own recent baseline,
which catches problems no fixed alert threshold was set for.

A point is anomalous when |value - baseline mean| / baseline stdev >= z_threshold. An anomaly is raised only when
the last `consecutive` points are all anomalous, so a single blip is ignored. Output is an anomaly payload that
intake turns into an IncidentSignal (source_hint "anomaly")."""
import statistics
from datetime import datetime

MIN_BASELINE = 20        # points needed before judging anything
MIN_STDEV = 1e-6


def detect(series: list[tuple[datetime, float]], metric: str, resource: dict, service: str | None = None,
           z_threshold: float = 3.0, consecutive: int = 3, baseline_points: int = 60) -> dict | None:
    """series: (timestamp, value) oldest first. Returns an anomaly payload, or None."""
    if len(series) < MIN_BASELINE + consecutive:
        return None
    recent = series[-consecutive:]
    baseline = [v for _, v in series[-(consecutive + baseline_points):-consecutive]]
    mean = statistics.fmean(baseline)
    stdev = max(statistics.pstdev(baseline), MIN_STDEV, abs(mean) * 0.01)   # a flat line still needs a real move
    zs = [(v - mean) / stdev for _, v in recent]
    if not all(abs(z) >= z_threshold for z in zs) or len({z > 0 for z in zs}) != 1:   # same direction, every point
        return None
    ts, value = recent[-1]
    first_ts = recent[0][0]
    return {
        "id": f"{resource.get('name', 'resource')}:{metric.rsplit('/', 1)[-1]}:{first_ts:%Y%m%dT%H%M}",
        "detector": f"zscore-{baseline_points}pt",
        "metric": metric,
        "resource": resource,
        **({"service": service} if service else {}),
        "observed": round(value, 4),
        "expected": round(mean, 4),
        "zscore": round(zs[-1], 2),
        "detected_at": first_ts.isoformat(),
    }
