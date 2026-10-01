"""M3: case memory (survives restarts, recognises repeats) and the knowledge-base index (stable chunk ids)."""
import json, pathlib, sys
from datetime import timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_correlation import BASE, Clock, make_signal   # noqa: E402

from copilot.correlation import Correlator
from copilot.kb.index import KnowledgeIndex, chunk
from copilot.memory import Memory, restore, signature


def burst(prefix, minute, service="storefront", vm="web-01"):
    """What was seen live: a VM start (info) and a burst of the same guest-agent error."""
    return [dict(id=f"{prefix}-start", minute=minute, service=service, title=f"{vm}: VM started", resource=vm,
                 metric="vm_event/start", severity="info", source="cloud_audit", type="event"),
            *[dict(id=f"{prefix}-e{i}", minute=minute + 1, service=service, title=f"{vm} GCEGuestAgent: error setting initial metadatasshkey configuration: failed to remove user u{i} from google-sudoers",
                   resource=vm, metric="log/GCEGuestAgent", severity="error", source="cloud_logging", type="event")
              for i in range(3)]]


def run(signals, memory, corr=None, clock=None):
    clock = clock or Clock()
    corr = corr or Correlator(clock=clock)
    for d in signals:
        clock.now = BASE + timedelta(minutes=d["minute"], seconds=5)
        corr.ingest(make_signal(d))
        for c in corr.open_cases():
            memory.save_case(c)
    return corr


def test_cases_and_outcomes_survive_a_restart(tmp_path):
    db = tmp_path / "memory.sqlite"
    m = Memory(db)
    corr = run(burst("a", 0), m)
    [case] = corr.open_cases()
    m.record(case.case_id, "outcome", {"action": "none", "result": "known issue"})
    m.db.close()

    m2 = Memory(db)                                     # a new process
    again = m2.get(case.case_id)
    assert again is not None and len(again.signals) == 2 and again.services == ["storefront"]
    assert m2.events(case.case_id)[0]["result"] == "known issue"


def test_replaying_the_same_history_does_not_duplicate_memory(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    first = run(burst("a", 0), m).open_cases()[0].case_id
    second = run(burst("a", 0), m).open_cases()[0].case_id      # watch --since again: new random case id
    assert first != second
    assert m.db.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
    assert m.save_case(run(burst("a", 0), m).open_cases()[0]) == first     # stored id is stable


def test_a_repeated_incident_finds_its_past_case_first(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    run(burst("mon", 0), m)                                              # Monday
    run([dict(id="cpu", minute=300, service="storefront", title="CPU > 85% on web-01", resource="web-01",
              metric="compute.googleapis.com/instance/cpu/utilization", severity="warning")], m)   # different kind
    run(burst("x", 600, service="reports-batch", vm="batch-01"), m)      # same error, other service
    today = run(burst("tue", 1440), m).open_cases()[0]                  # Tuesday: the same thing again
    hits = m.similar_cases(today)
    assert hits and hits[0]["first_seen"].startswith((BASE).date().isoformat()) and hits[0]["score"] == 1.0
    assert all("reports-batch" not in h["services"] for h in hits)     # same message, different service: not similar
    assert all("compute.googleapis.com/instance/cpu/utilization" not in str(h["signature"]) for h in hits)


def test_info_context_is_not_part_of_the_signature(tmp_path):
    corr = run(burst("a", 0), Memory(tmp_path / "m.sqlite"))
    assert signature(corr.open_cases()[0]) == ["storefront|cloud_logging|log/GCEGuestAgent"]


def test_history_and_outcome_stats_by_service(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    a = run(burst("a", 0), m).open_cases()[0].case_id
    b = run(burst("b", 600), m).open_cases()[0].case_id
    run(burst("c", 900, service="reports-batch", vm="batch-01"), m)
    m.record(a, "outcome", {"action": "vm.start", "result": "resolved"})
    m.record(b, "outcome", {"action": "vm.start", "result": "failed"})
    assert [h["case_id"] for h in m.history("storefront")] == [b, a]      # newest first
    assert m.outcome_stats("vm.start", "storefront") == {"resolved": 1, "failed": 1}
    assert m.outcome_stats("vm.start", "reports-batch") == {}
    with pytest.raises(ValueError):
        m.record(a, "gossip", {})


def test_an_incident_in_progress_continues_after_a_restart(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    sigs = [dict(id="db", minute=0, service="orders-db", title="VM db-01 stopped reporting", resource="db-01",
                 metric="compute.googleapis.com/instance/uptime", severity="critical"),
            dict(id="api", minute=4, service="orders-api", title="Uptime check orders-health failing",
                 resource="34.93.10.21", metric="monitoring.googleapis.com/uptime_check/check_passed", severity="critical")]
    before = run(sigs[:1], m).open_cases()[0].case_id
    clock = Clock()
    clock.now = BASE + timedelta(minutes=3)
    fresh = Correlator(clock=clock)                                         # the copilot restarted
    restore(fresh, m, window=timedelta(minutes=45))
    run(sigs[1:], m, corr=fresh, clock=clock)
    [case] = fresh.open_cases()
    assert case.case_id == before and case.services == ["orders-api", "orders-db"]


# ---- knowledge-base index --------------------------------------------------------------------------

class WordEmbedder:
    """Deterministic stand-in for an embedding model: a bag-of-words vector. Counts documents embedded."""
    VOCAB = ["vm", "stopped", "start", "schedule", "guest", "agent", "ssh", "users", "error", "disk", "firewall"]
    def __init__(self): self.embedded = 0
    def _v(self, t):
        words = t.lower().replace("-", " ").split()
        return [sum(w.startswith(v) for w in words) for v in self.VOCAB]
    def embed_documents(self, texts):
        self.embedded += len(texts)
        return [self._v(t) for t in texts]
    def embed_query(self, t): return self._v(t)


def kb(tmp_path):
    root = tmp_path / "kb"
    (root / "runbooks").mkdir(parents=True)
    for f in ("vm-stopped.md", "guest-agent-errors.md"):
        (root / "runbooks" / f).write_text((pathlib.Path(__file__).parent.parent / "docs/kb/runbooks" / f).read_text())
    return root


def test_chunk_ids_are_stable_and_unchanged_chunks_are_not_re_embedded(tmp_path):
    root = kb(tmp_path)
    emb = WordEmbedder()
    idx = KnowledgeIndex(Memory(tmp_path / "m.sqlite").db, embedder=emb, root=root)
    s1 = idx.rebuild()
    ids1 = [r[0] for r in idx.db.execute("SELECT chunk_id FROM kb_chunks ORDER BY chunk_id")]
    assert s1["added"] == len(ids1) and "runbooks/vm-stopped.md#remediation" in ids1

    assert idx.rebuild() == {"added": 0, "updated": 0, "unchanged": len(ids1), "removed": 0}
    doc = root / "runbooks" / "vm-stopped.md"
    doc.write_text(doc.read_text().replace("within\n  10 minutes", "within\n  15 minutes"))
    before = emb.embedded
    s3 = idx.rebuild()
    assert s3["updated"] == 1 and emb.embedded == before + 1           # only the changed section
    assert [r[0] for r in idx.db.execute("SELECT chunk_id FROM kb_chunks ORDER BY chunk_id")] == ids1


def test_search_finds_the_right_runbook_section(tmp_path):
    idx = KnowledgeIndex(Memory(tmp_path / "m.sqlite").db, embedder=WordEmbedder(), root=kb(tmp_path))
    idx.rebuild()
    top = idx.search("guest agent ssh users error after start", k=1)[0]
    assert top["path"] == "runbooks/guest-agent-errors.md"


def test_deleted_documents_leave_the_index(tmp_path):
    root = kb(tmp_path)
    idx = KnowledgeIndex(Memory(tmp_path / "m.sqlite").db, embedder=WordEmbedder(), root=root)
    idx.rebuild()
    (root / "runbooks" / "guest-agent-errors.md").unlink()
    assert idx.rebuild()["removed"] > 0
    assert all(r[0].startswith("runbooks/vm-stopped.md") for r in idx.db.execute("SELECT chunk_id FROM kb_chunks"))


def test_duplicate_headings_get_distinct_ids(tmp_path):
    p = tmp_path / "d.md"
    p.write_text("# T\n## Checks\na\n## Checks\nb\n")
    assert [c["chunk_id"] for c in chunk(p, tmp_path)] == ["d.md#checks", "d.md#checks-2"]
