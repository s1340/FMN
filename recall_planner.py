#!/usr/bin/env python3
"""
recall_planner.py — Complexity-aware recall + BM25 channel for query_graph.

Two steals from TiMem (arXiv:2601.02845), taken on FMN's terms:

1. COMPLEXITY-AWARE RECALL. TiMem classifies each query simple/hybrid/complex
   and scales retrieval depth accordingly — a "when did X happen" question
   should not pay for (or drown in) a full-depth sweep, and a "who is she to
   me" question should not be answered from three keyword hits. TiMem uses an
   LLM planner per query; FMN explicitly does NOT (design decision 2026-07-03:
   no LLM call on the conversational hot path — ever). The classifier here is
   a transparent heuristic: marker words, resolved in precedence order
   complex > hybrid > simple. It will sometimes be wrong, cheaply and
   inspectably; the depths only differ in breadth, never in whether the best
   direct hits surface.

2. BM25. The old brief-matching channel ("+0.5 per >4-char word found in the
   brief") had no notion of term rarity — "memory" matched everything, and a
   distinctive word matched no harder than a common one. BM25 is the same
   idea grown up: IDF-weighted, length-normalized, corpus-calibrated. It
   scores against IN-GRAPH text only (brief + episode + topics + entities) —
   no file IO on the query path; verbatim-chunk vocabulary is the embedding
   channel's job (chunk head is embedded), and THE ONE LAW means the chunk
   is read via expand, not searched by substring.

Used by memory_graph.query_graph; standalone CLI for inspection:

    python recall_planner.py classify "what did we decide about the cron?"
    python recall_planner.py bm25 "door scratching"      # top cells, debug
"""

import math
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Query complexity (heuristic, no LLM, transparent) ──────────────────────────

# Personalized reasoning: the answer lives in preferences, personality, the
# relationship — TiMem's "complex". Full depth: constellations + profile.
COMPLEX_MARKERS = [
    "who am i", "who are you", "who is she", "who is he", "what am i",
    "relationship", "personality", "identity", "feel about", "feels about",
    "felt about", "think of me", "thinks of", "mean to", "means to",
    "between us", "about us", "prefer", "prefers", "preference", "habit",
    "usually", "always", "never", "tend to", "tends to", "value", "values",
    "love", "trust", "care about", "understand me", "know me", "know each",
]

# Multi-fact integration: enumerate / summarize / compare — TiMem's "hybrid".
HYBRID_MARKERS = [
    "all the", "every ", "everything", "how many", "how often", "list ",
    "summarize", "summary", "compare", "difference between", "both ",
    "over time", "history of", "changed", "evolution", "so far", "each time",
    "what happened with", "catch me up", "timeline",
]


def classify(text: str) -> tuple[str, str]:
    """(complexity, reason). Precedence: complex > hybrid > simple —
    a query that both enumerates and asks about the relationship is complex."""
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    for m in COMPLEX_MARKERS:
        if m in t:
            return "complex", f"marker '{m.strip()}'"
    for m in HYBRID_MARKERS:
        if m in t:
            return "hybrid", f"marker '{m.strip()}'"
    return "simple", "single-fact shape"


# Depth per class. Breadth only — scoring is identical across classes, so a
# misclassification costs a few extra/fewer neighbors, never the direct hit.
#   limit_mult      — multiplies the caller's result limit
#   expand_top      — how many top hits seed graph expansion
#   consult_profile — surface the living-portrait signpost (complex only)
PLANS = {
    "simple":  {"limit_mult": 1.0, "expand_top": 3, "consult_profile": False},
    "hybrid":  {"limit_mult": 1.5, "expand_top": 4, "consult_profile": False},
    "complex": {"limit_mult": 2.0, "expand_top": 5, "consult_profile": True},
}


def plan(text: str) -> dict:
    c, reason = classify(text)
    return {"complexity": c, "reason": reason, **PLANS[c]}


# ── BM25 (self-contained; corpus = in-graph cell text) ─────────────────────────

K1, B = 1.5, 0.75

_token_re = re.compile(r"[a-z0-9_]+")


def tokenize(s: str) -> list[str]:
    # Underscored slugs count as both forms: "model_size" must match a query
    # saying "model size" (the memory_eval Q3 lesson, relearned once already).
    toks = _token_re.findall(s.lower())
    out = []
    for t in toks:
        out.append(t)
        if "_" in t:
            out.extend(t.split("_"))
    return out


def node_text(node: dict) -> str:
    return " ".join([
        node.get("brief", ""), node.get("episode", ""),
        " ".join(node.get("topics", [])), " ".join(node.get("entities", [])),
    ])


def bm25_scores(query: str, nodes: dict) -> dict:
    """cell_id -> raw BM25 score (0 for no term overlap). Corpus built per
    call — at vault scale (hundreds of cells, in-memory strings) this is
    milliseconds and needs no index file to go stale."""
    q_terms = [t for t in set(tokenize(query)) if len(t) > 2]
    if not q_terms or not nodes:
        return {}

    docs = {cid: tokenize(node_text(n)) for cid, n in nodes.items()}
    N = len(docs)
    avgdl = sum(len(d) for d in docs.values()) / N

    df: dict[str, int] = {}
    for d in docs.values():
        for t in set(d):
            df[t] = df.get(t, 0) + 1

    scores: dict[str, float] = {}
    for cid, d in docs.items():
        if not d:
            continue
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in q_terms:
            n_t = df.get(t, 0)
            if not n_t or t not in tf:
                continue
            idf = math.log((N - n_t + 0.5) / (n_t + 0.5) + 1.0)
            f = tf[t]
            s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * len(d) / avgdl))
        if s > 0:
            scores[cid] = s
    return scores


# ── CLI (debug) ────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Recall planner / BM25 debug")
    ap.add_argument("command", choices=["classify", "bm25"])
    ap.add_argument("text", nargs="+")
    a = ap.parse_args()
    text = " ".join(a.text)

    if a.command == "classify":
        p = plan(text)
        print(f"{p['complexity']}  ({p['reason']})  "
              f"limit x{p['limit_mult']}, expand top-{p['expand_top']}"
              + (", consult profile" if p["consult_profile"] else ""))

    elif a.command == "bm25":
        sys.path.insert(0, str(Path(__file__).parent))
        from memory_graph import load_graph
        g = load_graph()
        scores = bm25_scores(text, g["nodes"])
        for cid, s in sorted(scores.items(), key=lambda x: -x[1])[:8]:
            brief = g["nodes"][cid].get("brief", "")[:70]
            print(f"  {s:6.2f}  {cid}  {brief}")


if __name__ == "__main__":
    main()
