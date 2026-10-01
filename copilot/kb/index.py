"""Semantic index over the knowledge base (spec M3: "a vector index over internal runbooks and past postmortems").

Documents are Markdown files under docs/kb/. Each is cut into chunks at its headings. A chunk's id is
"<path>#<heading-slug>", so it stays the same when the index is rebuilt: citations (M4) keep pointing at the same text.
Only chunks whose text changed are embedded again. The index lives in the memory database (table kb_chunks).

Embeddings are provider-agnostic like the chat model: COPILOT_EMBEDDINGS=provider:model (default Gemini).

Search modes (spec M4: "hybrid search + reranking"), all returning chunks with ids for citations:
  keyword  BM25 over the chunk text: exact terms, error strings, action names
  vector   cosine similarity of embeddings: meaning, paraphrases
  hybrid   both, fused by reciprocal rank (default)
  + rerank an LLM re-orders the fused candidates for the query (optional; measured by `copilot kb eval`)"""
import hashlib, json, math, os, pathlib, re, sqlite3

KB_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "kb"
DEFAULT_EMBEDDINGS = "google_genai:models/gemini-embedding-001"

SCHEMA = """CREATE TABLE IF NOT EXISTS kb_chunks (
  chunk_id TEXT PRIMARY KEY, path TEXT, heading TEXT, text TEXT, hash TEXT, model TEXT, vector TEXT)"""


def get_embedder():
    from langchain.embeddings import init_embeddings
    from ..usage import CountedEmbeddings
    return CountedEmbeddings(init_embeddings(os.getenv("COPILOT_EMBEDDINGS") or DEFAULT_EMBEDDINGS))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "top"


def chunk(path: pathlib.Path, root: pathlib.Path) -> list[dict]:
    """Split one Markdown file at its #/## headings. Each chunk keeps the document title for context."""
    rel = path.relative_to(root).as_posix()
    lines = path.read_text().splitlines()
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), path.stem)
    chunks, heading, buf, used = [], title, [], set()

    def flush():
        text = "\n".join(buf).strip()
        if text:
            cid, n = f"{rel}#{slug(heading)}", 2
            while cid in used:                       # two sections with the same heading
                cid, n = f"{rel}#{slug(heading)}-{n}", n + 1
            used.add(cid)
            chunks.append({"chunk_id": cid, "path": rel, "heading": heading,
                           "text": f"{title} / {heading}\n{text}" if heading != title else f"{title}\n{text}"})

    for line in lines:
        if re.match(r"^#{1,2} ", line):
            flush()
            heading, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    flush()
    return chunks


RRF_K = 60
_STOP = set("a an and are as at be by for from has in is it its of on or that the this to was were when with".split())


def tokens(text: str) -> list[str]:
    """Lowercase words; keeps action names like vm.start and error codes intact."""
    return [t.strip(".") for t in re.findall(r"[a-z0-9][a-z0-9_.\-]*", text.lower()) if t.strip(".") not in _STOP]


def rerank(query: str, hits: list[dict], llm) -> list[dict]:
    """Ask the LLM to order the candidates by how well they answer the query. Unknown ids it returns are ignored,
    and candidates it leaves out keep their fused order after the ones it ranked."""
    from pydantic import BaseModel

    class Order(BaseModel):
        chunk_ids: list[str]

    listing = "\n\n".join(f"[{h['chunk_id']}]\n{h['text'][:400]}" for h in hits)
    prompt = (f"Incident query:\n{query}\n\nKnowledge-base sections:\n{listing}\n\n"
              "Return the chunk_ids of the sections that help diagnose or remediate this incident, most useful first. "
              "Leave out sections about other technologies or situations.")
    order = llm.with_structured_output(Order).invoke(prompt).chunk_ids
    by_id = {h["chunk_id"]: h for h in hits}
    ranked = [by_id[i] for i in dict.fromkeys(order) if i in by_id]
    return ranked + [h for h in hits if h["chunk_id"] not in set(order)]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class KnowledgeIndex:
    def __init__(self, db: sqlite3.Connection, embedder=None, root: pathlib.Path = KB_DIR):
        self.db, self.root, self._embedder = db, pathlib.Path(root), embedder
        self.db.execute(SCHEMA)
        self.model = (os.getenv("COPILOT_EMBEDDINGS") or DEFAULT_EMBEDDINGS) if embedder is None else type(embedder).__name__

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def rebuild(self) -> dict:
        """Index every docs/kb/**/*.md. Returns counts: added, updated, unchanged, removed."""
        chunks = [c for p in sorted(self.root.rglob("*.md")) for c in chunk(p, self.root)]
        have = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT chunk_id, hash, model FROM kb_chunks")}
        todo, stats = [], {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
        for c in chunks:
            c["hash"] = hashlib.sha256(c["text"].encode()).hexdigest()[:16]
            if have.get(c["chunk_id"]) == (c["hash"], self.model):
                stats["unchanged"] += 1
            else:
                stats["updated" if c["chunk_id"] in have else "added"] += 1
                todo.append(c)
        vectors = self.embedder.embed_documents([c["text"] for c in todo]) if todo else []
        with self.db:
            for c, v in zip(todo, vectors):
                self.db.execute("INSERT OR REPLACE INTO kb_chunks VALUES (?,?,?,?,?,?,?)",
                                (c["chunk_id"], c["path"], c["heading"], c["text"], c["hash"], self.model, json.dumps(v)))
            gone = set(have) - {c["chunk_id"] for c in chunks}
            self.db.executemany("DELETE FROM kb_chunks WHERE chunk_id=?", [(g,) for g in gone])
            stats["removed"] = len(gone)
        return stats

    def _rows(self) -> list[dict]:
        return [{"chunk_id": r[0], "path": r[1], "heading": r[2], "text": r[3], "vector": r[4]}
                for r in self.db.execute("SELECT chunk_id, path, heading, text, vector FROM kb_chunks")]

    def get(self, chunk_id: str) -> dict | None:
        r = self.db.execute("SELECT chunk_id, path, heading, text FROM kb_chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return {"chunk_id": r[0], "path": r[1], "heading": r[2], "text": r[3]} if r else None

    def document(self, path: str) -> list[dict]:
        """All sections of one document, in order."""
        return [{"chunk_id": r[0], "path": r[1], "heading": r[2], "text": r[3]} for r in self.db.execute(
            "SELECT chunk_id, path, heading, text FROM kb_chunks WHERE path=? ORDER BY rowid", (path,))]

    def documents_for(self, query: str, docs: int = 3, **search) -> list[dict]:
        """Find the best-matching documents by their sections, then return every section of those documents.
        A match on a runbook's Symptoms section brings its Cause and Remediation along, which the diagnosis needs."""
        paths = list(dict.fromkeys(h["path"] for h in self.search(query, k=docs * 4, **search)))[:docs]
        return [c for p in paths for c in self.document(p)]

    def vector_search(self, query: str, k: int = 5) -> list[dict]:
        q = self.embedder.embed_query(query)
        hits = [{**{x: r[x] for x in ("chunk_id", "path", "heading", "text")}, "score": round(cosine(q, json.loads(r["vector"])), 4)}
                for r in self._rows()]
        return sorted(hits, key=lambda h: -h["score"])[:k]

    def keyword_search(self, query: str, k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[dict]:
        rows = self._rows()
        docs = [tokens(r["text"]) for r in rows]
        if not docs:
            return []
        avg = sum(map(len, docs)) / len(docs)
        df: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        hits = []
        for r, d in zip(rows, docs):
            score = 0.0
            for t in set(tokens(query)):
                if t in df:
                    tf = d.count(t)
                    idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                    score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
            if score > 0:
                hits.append({**{x: r[x] for x in ("chunk_id", "path", "heading", "text")}, "score": round(score, 4)})
        return sorted(hits, key=lambda h: -h["score"])[:k]

    def search(self, query: str, k: int = 5, mode: str = "hybrid", reranker=None, pool: int = 20) -> list[dict]:
        """The k best chunks for the query. mode: keyword | vector | hybrid. reranker: optional LLM re-ordering."""
        if mode == "keyword":
            hits = self.keyword_search(query, pool)
        elif mode == "vector":
            hits = self.vector_search(query, pool)
        else:   # reciprocal rank fusion: robust to the two scores being on different scales
            fused: dict[str, dict] = {}
            for ranked in (self.keyword_search(query, pool), self.vector_search(query, pool)):
                for rank, h in enumerate(ranked):
                    f = fused.setdefault(h["chunk_id"], {**h, "score": 0.0})
                    f["score"] += 1 / (RRF_K + rank + 1)
            hits = sorted(fused.values(), key=lambda h: -h["score"])
        if reranker is not None:
            hits = rerank(query, hits[:pool], reranker)
        return hits[:k]
