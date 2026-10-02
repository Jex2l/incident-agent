"""runbook_search MCP tool: semantic search over runbooks/*.md.

Indexes runbooks into SQLite with embeddings from Ollama's nomic-embed-text
model. If Ollama or that model isn't available, falls back to BM25 keyword
search transparently (same function signature, same result shape) - the
agent doesn't need to know which mode is active.
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import cap_list

ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS_DIR = Path(os.environ.get("RUNBOOKS_DIR", str(ROOT / "runbooks")))
DB_PATH = Path(os.environ.get("RUNBOOK_DB_PATH", str(RUNBOOKS_DIR / "runbooks.sqlite3")))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("RUNBOOK_EMBED_MODEL", "nomic-embed-text")
MAX_RESULTS = int(os.environ.get("RUNBOOK_SEARCH_MAX_RESULTS", "5"))
EXCERPT_CHARS = 400

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _parse_runbook(path: Path) -> dict:
    text = path.read_text()
    root_cause_label = None
    m = _FRONTMATTER_RE.match(text)
    body = text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip() == "root_cause_label":
                    root_cause_label = v.strip()
        body = text[m.end():]
    title_match = _TITLE_RE.search(body)
    title = title_match.group(1).strip() if title_match else path.stem
    return {"path": str(path.relative_to(ROOT)), "title": title, "root_cause_label": root_cause_label, "content": body.strip()}


def _embed(text: str) -> list[float] | None:
    try:
        resp = httpx.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def build_index(force: bool = False) -> dict:
    """(Re)build the SQLite index from runbooks/*.md. Tries embeddings via
    Ollama first; if unavailable, stores content only (BM25 fallback is
    computed at query time from this same content)."""
    if DB_PATH.exists() and not force:
        return {"status": "exists", "path": str(DB_PATH)}

    runbook_files = sorted(RUNBOOKS_DIR.glob("*.md"))
    docs = [_parse_runbook(p) for p in runbook_files]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS runbooks")
    conn.execute(
        """CREATE TABLE runbooks (
            id INTEGER PRIMARY KEY,
            path TEXT, title TEXT, root_cause_label TEXT, content TEXT, embedding TEXT
        )"""
    )
    embed_mode = False
    for doc in docs:
        embedding = _embed(doc["content"])
        if embedding is not None:
            embed_mode = True
        conn.execute(
            "INSERT INTO runbooks (path, title, root_cause_label, content, embedding) VALUES (?, ?, ?, ?, ?)",
            (doc["path"], doc["title"], doc["root_cause_label"], doc["content"],
             json.dumps(embedding) if embedding is not None else None),
        )
    conn.commit()
    conn.close()
    return {"status": "built", "path": str(DB_PATH), "doc_count": len(docs), "embed_mode": embed_mode}


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


def _bm25_search(query: str, rows: list[sqlite3.Row], top_k: int) -> list[tuple[sqlite3.Row, float]]:
    from rank_bm25 import BM25Okapi

    corpus = [r["content"].lower().split() for r in rows]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query.lower().split())
    ranked = sorted(zip(rows, scores), key=lambda rs: rs[1], reverse=True)
    return ranked[:top_k]


def _embedding_search(query: str, rows: list[sqlite3.Row], top_k: int) -> list[tuple[sqlite3.Row, float]] | None:
    query_embedding = _embed(query)
    if query_embedding is None:
        return None
    q = np.array(query_embedding, dtype=np.float32)
    scored = []
    for r in rows:
        if not r["embedding"]:
            continue
        v = np.array(json.loads(r["embedding"]), dtype=np.float32)
        scored.append((r, _cosine(q, v)))
    if not scored:
        return None
    return sorted(scored, key=lambda rs: rs[1], reverse=True)[:top_k]


def search_runbooks(query: str, top_k: int = 3) -> dict:
    build_index()  # lazy build on first use
    top_k = min(top_k, MAX_RESULTS)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM runbooks").fetchall()
    conn.close()

    if not rows:
        return {"query": query, "mode": "none", "results": []}

    ranked = _embedding_search(query, rows, top_k)
    mode = "embedding"
    if ranked is None:
        ranked = _bm25_search(query, rows, top_k)
        mode = "bm25"

    results = [
        {
            "title": r["title"],
            "path": r["path"],
            "root_cause_label": r["root_cause_label"],
            "score": round(float(score), 4),
            "excerpt": r["content"][:EXCERPT_CHARS],
        }
        for r, score in ranked
    ]
    return {"query": query, "mode": mode, "results": results}


def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("runbook_search")

    @mcp.tool()
    def search_runbooks_tool(query: str, top_k: int = 3) -> dict:
        """Search the runbook library for guidance relevant to a query
        (e.g. an alert name or symptom description). Uses embedding
        similarity when available, falls back to BM25 keyword search."""
        return search_runbooks(query, top_k)

    return mcp


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--search")
    args = parser.parse_args()
    if args.build_index:
        print(json.dumps(build_index(force=args.force), indent=2))
    elif args.search:
        print(json.dumps(search_runbooks(args.search), indent=2))
    else:
        build_server().run()
