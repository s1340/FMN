#!/usr/bin/env python3
"""
memory_embed.py — Semantic embedding layer for FMN (Forget-me-not).

Closes the paraphrase gap: keyword retrieval cannot connect "your cron went
feral" to a cell about a rogue stir instance. One 256-d static embedding per
cell (model2vec potion-base-8M — 30MB, no torch, CPU, milliseconds) gives:

  - semantic retrieval  (hybrid with keyword scoring in query_graph —
                         keyword stays because it is PREDICTABLE and cannot
                         hallucinate similarity; embeddings add recall)
  - semantic_sim edges  (real weights for the associative strings)
  - rumination pairing  (candidate pairs beyond literal entity overlap)

Embedding text = brief + episode + head of chunk. The chunk head matters:
briefs paraphrase away exact vocabulary ("cron" lives in the transcript,
not the summary), and paraphrase-robustness is the whole point.

Store: <vault>/30_EPISODES/embeddings.json  {cell_id: [floats]}
Graceful degradation: if model2vec is not installed, every entry point
returns None/empty and callers fall back to keyword-only behavior.

Usage:
    python memory_embed.py build            # embed all cells missing vectors
    python memory_embed.py build --force    # re-embed everything
    python memory_embed.py status
    python memory_embed.py query "text"     # debug: top cells by cosine
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


MODEL_NAME = "minishlab/potion-base-8M"
EMBED_FILE = mg.GRAPH_DIR / "embeddings.json"
CHUNK_HEAD_CHARS = 600


# ── Model (lazy, optional) ───────────────────────────────────────────────────

_model = None
_model_failed = False


def get_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    try:
        from model2vec import StaticModel
        _model = StaticModel.from_pretrained(MODEL_NAME)
    except Exception as e:
        _model_failed = True
        print(f"[memory_embed] embeddings unavailable ({e.__class__.__name__}) "
              f"— falling back to keyword-only", file=sys.stderr)
    return _model


def available() -> bool:
    return get_model() is not None


# ── Store ────────────────────────────────────────────────────────────────────

def load_store() -> dict:
    if EMBED_FILE.exists():
        return json.loads(EMBED_FILE.read_text(encoding="utf-8"))
    return {}


def save_store(store: dict) -> None:
    EMBED_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMBED_FILE.write_text(json.dumps(store), encoding="utf-8")


# ── Embedding ────────────────────────────────────────────────────────────────

def cell_embed_text(node: dict) -> str:
    parts = [node.get("brief", ""), node.get("episode", "")]
    path = Path(node.get("file", ""))
    if path.exists():
        try:
            parts.append(mg.parse_cell(path)["chunk"][:CHUNK_HEAD_CHARS])
        except Exception:
            pass
    return "\n".join(p for p in parts if p)


def embed_texts(texts: list[str]):
    model = get_model()
    if model is None:
        return None
    import numpy as np
    vecs = model.encode(texts)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms                      # unit vectors; cosine = dot


def embed_cells(graph: dict, force: bool = False) -> int:
    """Embed cells missing vectors (or all with force). Returns count embedded."""
    if not available():
        return 0
    store = load_store()
    todo = [n for n in graph["nodes"].values()
            if force or n["cell_id"] not in store]
    if not todo:
        return 0
    vecs = embed_texts([cell_embed_text(n) for n in todo])
    for node, vec in zip(todo, vecs):
        store[node["cell_id"]] = [round(float(x), 6) for x in vec]
    save_store(store)
    return len(todo)


# ── Query-side scoring ───────────────────────────────────────────────────────

def semantic_scores(query: str, store: dict) -> dict:
    """cell_id -> cosine similarity with the query. {} if unavailable."""
    if not store or not available():
        return {}
    import numpy as np
    qv = embed_texts([query])
    if qv is None:
        return {}
    qv = qv[0]
    ids = list(store.keys())
    mat = np.array([store[c] for c in ids], dtype=np.float32)
    sims = mat @ qv
    return {c: float(s) for c, s in zip(ids, sims)}


def semantic_pairs(store: dict, threshold: float = 0.75,
                   top_k: int = 5) -> list[tuple]:
    """(cell_a, cell_b, cosine) — each cell's strongest strings only.

    PER-CELL TOP-K, not a global threshold sweep. A global threshold breaks
    down when content is self-similar (fmn_stress 2026-07-02: 9.7% of pairs
    above 0.75 on templated cells = 17k edges). Mal's original design was
    "a certain LIMITED number of strings" per cell — this is that: each cell
    keeps at most top_k semantic edges, floor 0.6, and an edge exists if
    EITHER endpoint nominates it. Edge count grows O(n·k), never O(n²).
    (0.75 remains the measured p98 on real diverse cells, 2026-07-01.)
    """
    if not store or not available():
        return []
    import numpy as np
    floor = min(threshold, 0.6)
    ids = list(store.keys())
    mat = np.array([store[c] for c in ids], dtype=np.float32)
    sims = mat @ mat.T
    np.fill_diagonal(sims, -1.0)
    keep: set[tuple[int, int]] = set()
    for i in range(len(ids)):
        order = np.argsort(-sims[i])[:top_k]
        for j in order:
            s = sims[i, j]
            if s >= threshold or (s >= floor and j in set(np.argsort(-sims[i])[:2])):
                keep.add((min(i, int(j)), max(i, int(j))))
    return [(ids[i], ids[j], float(sims[i, j])) for i, j in sorted(keep)]


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="FMN embedding layer")
    ap.add_argument("command", choices=["build", "status", "query"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    graph = mg.load_graph()

    if args.command == "build":
        n = embed_cells(graph, force=args.force)
        store = load_store()
        print(f"Embedded {n} cells  |  store: {len(store)} vectors "
              f"({EMBED_FILE.stat().st_size // 1024 if EMBED_FILE.exists() else 0} KB)")

    elif args.command == "status":
        store = load_store()
        missing = [c for c in graph["nodes"] if c not in store]
        print(f"available: {available()}  |  vectors: {len(store)}  |  "
              f"cells missing: {len(missing)}")
        if missing:
            print(f"  missing: {missing[:10]}{' ...' if len(missing) > 10 else ''}")

    elif args.command == "query":
        if not args.text:
            print("Usage: query \"text\"", file=sys.stderr)
            sys.exit(1)
        scores = semantic_scores(args.text, load_store())
        top = sorted(scores.items(), key=lambda x: -x[1])[:8]
        for cid, s in top:
            brief = graph["nodes"].get(cid, {}).get("brief", "")[:70]
            print(f"  {s:+.3f}  {cid}  {brief}")


if __name__ == "__main__":
    main()
