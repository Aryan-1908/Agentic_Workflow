"""Loads the service map (config/services*.toml): which GCP resources belong to which service, and their dependencies."""
import fnmatch, os, pathlib, tomllib
from dataclasses import dataclass, field
from functools import lru_cache

CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


@dataclass
class Service:
    name: str
    tier: int
    description: str = ""
    resources: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    external: bool = False
    status_url: str = ""
    data: bool = False           # stores data (databases): file-changing actions are never allowed on it


def services(path: str | pathlib.Path | None = None) -> dict[str, Service]:
    """The service map: `path`, else $COPILOT_SERVICES (set from the client's `services`), else the Acme demo map."""
    return _load(pathlib.Path(path or os.getenv("COPILOT_SERVICES") or CONFIG_DIR / "services.toml").resolve())


@lru_cache
def _load(path: pathlib.Path) -> dict[str, Service]:
    raw = tomllib.loads(path.read_text())
    out = {n: Service(name=n, **v) for n, v in raw.get("services", {}).items()}
    out |= {n: Service(name=n, external=True, **v) for n, v in raw.get("external", {}).items()}
    for svc in out.values():   # e.g. COPILOT_STATUS_URL_PAYMENTS_PROVIDER, for status pages whose address is only known at runtime
        svc.status_url = os.getenv(f"COPILOT_STATUS_URL_{svc.name.upper().replace('-', '_')}", svc.status_url)
    return out


# Dependencies learned from traces at runtime (caller -> callee), on top of the ones written in the service map.
_observed: dict[str, set[str]] = {}


def observe_edge(caller: str, callee: str):
    if caller and callee and caller != callee:
        _observed.setdefault(caller, set()).add(callee)


def observed_edges() -> dict[str, set[str]]:
    return {k: set(v) for k, v in _observed.items()}


def reset_observed():
    _observed.clear()


def neighbors(service: str) -> list[str]:
    """What a service depends on: written in the service map, or seen in traces."""
    svc = services().get(service)
    return sorted(set(svc.depends_on if svc else []) | _observed.get(service, set()))


def depends_on(a: str, b: str) -> bool:
    """True if service a needs b, directly or through other services (orders-api -> orders-db)."""
    seen, todo = set(), [a]
    while todo:
        cur = todo.pop()
        for dep in neighbors(cur):
            if dep == b:
                return True
            if dep not in seen:        # cycle-safe
                seen.add(dep)
                todo.append(dep)
    return False


def dependency_path(a: str, b: str) -> list[str] | None:
    """Shortest a -> ... -> b chain, for human-readable reasons. None if a doesn't depend on b."""
    prev, todo = {a: None}, [a]
    while todo:
        cur = todo.pop(0)
        if cur == b:
            path = []
            while cur is not None:
                path.append(cur)
                cur = prev[cur]
            return path[::-1]
        for dep in neighbors(cur):
            if dep not in prev:
                prev[dep] = cur
                todo.append(dep)
    return None


def service_for_resource(name: str | None) -> str | None:
    """'web-01' -> 'storefront'. None if no service claims it."""
    if not name:
        return None
    for svc in services().values():
        if any(fnmatch.fnmatch(name, pat) for pat in svc.resources):
            return svc.name
    return None
