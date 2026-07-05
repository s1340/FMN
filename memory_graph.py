#!/usr/bin/env python3
"""
memory_graph.py — Association graph for approved memory cells.

The graph is the core of Mal's hierarchical memory architecture.
Nodes are approved cells (left quarantine). Edges are weighted connections.

Edge types:
  shared_entity    — cells share N entities (weight = overlap count)
  shared_topic     — cells share N topics (weight = overlap count)
  temporal_adj     — cells from same session, adjacent in transcript
  manual           — manually declared (e.g. Sonnet's "door-scratching + Word reveal are one arc")
  co_retrieval     — added when Q actually pulls two cells together (weight grows over time)

Storage: <vault>/30_EPISODES/graph.json
         <vault>/30_EPISODES/nodes/   (symlinks or copies of approved cell .md files)

Usage:
    python memory_graph.py init                          # create empty graph
    python memory_graph.py approve <cell_id> [--from DIR] # promote cell from quarantine to graph
    python memory_graph.py edge <a> <b> --type manual --weight 1.0
    python memory_graph.py build-edges                   # auto-generate shared_entity/topic edges
    python memory_graph.py query <text>                  # find relevant cells
    python memory_graph.py age                           # run temporal aging
    python memory_graph.py stats                         # graph statistics
    python memory_graph.py export                        # dump graph as JSON
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                  r"C:\Users\User\Documents\Obsidian Vault"))
GRAPH_DIR  = VAULT_ROOT / "30_EPISODES"
GRAPH_FILE = GRAPH_DIR / "graph.json"
NODES_DIR  = GRAPH_DIR / "nodes"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

# Aging thresholds (days)
FRESH_DAYS   = 1    # today
RECENT_DAYS  = 7    # this week
OLD_DAYS     = 30   # this month
# beyond OLD_DAYS → archived (unless significance: bright, which ages slower)

BRIGHT_AGE_MULTIPLIER = 3.0  # bright cells age 3x slower


# ── Graph data structures ──────────────────────────────────────────────────────

def empty_graph() -> dict:
    return {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "nodes": {},    # cell_id → node dict
        "edges": [],    # list of edge dicts
        "metadata": {
            "total_approvals": 0,
            "total_retrievals": 0,
        },
    }


def load_graph() -> dict:
    if not GRAPH_FILE.exists():
        return empty_graph()
    with open(GRAPH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _json_default(obj):
    """Handle YAML date/datetime objects that JSON can't serialize."""
    import datetime as _dt
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def save_graph(graph: dict) -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to a temp file, then os.replace (atomic on
    # Windows + POSIX). A crash mid-write can never leave a truncated graph.
    tmp = GRAPH_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False, default=_json_default)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GRAPH_FILE)


# ── Concurrency lock (Q found this: load->modify->save had no lock, so two
#    processes — Q's tools, cron, the panel, an editor — could clobber each
#    other's writes. Any MUTATION must hold this lock across the WHOLE
#    load->modify->save cycle, not just the save.) ─────────────────────────

import time
from contextlib import contextmanager

LOCK_FILE = GRAPH_DIR / "graph.lock"


@contextmanager
def graph_lock(timeout: float = 10.0, poll: float = 0.05):
    """Cross-process mutex via exclusive lock-file creation (O_CREAT|O_EXCL is
    atomic on Windows and POSIX). Stale locks (>60s, dead writer) are broken."""
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError:
            # break a stale lock left by a crashed writer
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) > 60:
                    os.unlink(LOCK_FILE)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError(
                    f"graph.lock held >{timeout}s — another FMN process is "
                    f"writing. Retry, or remove {LOCK_FILE} if it's stale.")
            time.sleep(poll)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


@contextmanager
def locked_graph():
    """Load → yield graph for mutation → save, all under the lock. Use this for
    EVERY read-modify-write so concurrent writers can't eat each other:

        with locked_graph() as g:
            g["nodes"][cid] = ...
    """
    with graph_lock():
        g = load_graph()
        yield g
        save_graph(g)


# ── Cell parsing ───────────────────────────────────────────────────────────────

def parse_cell(path: Path) -> dict:
    """Read a .md cell file. Returns frontmatter dict + body sections."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"No frontmatter in {path.name}")
    import yaml
    fm = yaml.safe_load(m.group(1))
    body = m.group(2)

    sections = {"brief": "", "episode": "", "chunk": ""}
    current = None
    for line in body.splitlines():
        if line.strip() == "## Brief":
            current = "brief"
        elif line.strip() == "## Episode":
            current = "episode"
        elif line.strip() == "## Chunk":
            current = "chunk"
        elif current:
            sections[current] += line + "\n"

    return {
        "frontmatter": fm,
        "brief": sections["brief"].strip(),
        "episode": sections["episode"].strip(),
        "chunk": sections["chunk"].strip(),
        "raw": text,
    }


# ── Node management ────────────────────────────────────────────────────────────

def approve_cell(cell_id: str, source_dir: Path) -> None:
    """Promote a cell from quarantine to the graph."""
    graph = load_graph()

    # Find the cell file
    cell_files = [f for f in source_dir.glob("*.md")
                  if not f.name.startswith("merge_proposals")]
    cell_path = None
    for f in cell_files:
        try:
            c = parse_cell(f)
        except Exception:
            continue    # one corrupt file must not poison the whole batch
        if c["frontmatter"].get("cell_id") == cell_id:
            cell_path = f
            cell_data = c
            break

    if not cell_path:
        print(f"Error: cell {cell_id} not found in {source_dir}", file=sys.stderr)
        sys.exit(1)

    # Copy cell to nodes directory
    node_path = NODES_DIR / cell_path.name
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    node_path.write_text(cell_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Add node to graph
    fm = cell_data["frontmatter"]
    node = {
        "cell_id":               cell_id,
        "session_id":            fm.get("session_id"),
        "session_date":          fm.get("session_date"),
        "created":               fm.get("created", datetime.now(timezone.utc).isoformat()),
        "topics":                fm.get("topics", []),
        "entities":              fm.get("entities", []),
        "significance":          fm.get("significance", "medium"),
        "valence":               fm.get("valence", "neutral"),
        "novelty":               fm.get("novelty", "routine"),
        "semantic_type":         fm.get("semantic_type", "work_research"),
        "reflection_candidate":  bool(fm.get("reflection_candidate", False)),
        "brief":                 cell_data["brief"],
        "episode":               cell_data["episode"],
        "temporal_status":       "fresh",
        "referenced_count":      0,
        "last_referenced":       None,
        "approved_at":           datetime.now(timezone.utc).isoformat(),
        "neighbors":             fm.get("neighbors", []),
        "file":                  str(node_path),
    }

    graph["nodes"][cell_id] = node
    graph["metadata"]["total_approvals"] += 1
    save_graph(graph)

    print(f"OK Approved: {cell_id} — {fm.get('topics', [])}")
    print(f"  sig: {fm.get('significance')} | val: {fm.get('valence')}")
    print(f"  brief: {cell_data['brief'][:120]}")


# ── Edge management ───────────────────────────────────────────────────────────

def add_edge(graph: dict, a: str, b: str, edge_type: str,
             weight: float = 1.0, note: str = "") -> bool:
    """Add or update an edge between two nodes. Returns True if added/updated,
    False if a node is missing. Respects the severed registry: a pair Mal or Q
    cut stays cut — auto-edge builders must not resurrect it.

    A missing node is SKIPPED, not fatal: build_auto_edges reads neighbor IDs
    from frontmatter that may reference cells never admitted (a stale ref must
    not kill a 100-cell edge rebuild). The CLI `edge` command checks the return
    and reports the error itself.
    """
    if a not in graph["nodes"] or b not in graph["nodes"]:
        return False

    key = sorted([a, b])
    for s in graph.get("metadata", {}).get("severed", []):
        if s.get("pair") == key and s.get("type") in (edge_type, "*"):
            return False

    # Check if edge exists (undirected — normalize order)
    for edge in graph["edges"]:
        if (edge["a"] == a and edge["b"] == b) or (edge["a"] == b and edge["b"] == a):
            if edge["type"] == edge_type:
                edge["weight"] = max(edge["weight"], weight)
                if note:
                    edge["note"] = note
                return True

    graph["edges"].append({
        "a": a,
        "b": b,
        "type": edge_type,
        "weight": weight,
        "note": note,
        "created": datetime.now(timezone.utc).isoformat(),
    })
    return True


def build_auto_edges(graph: dict) -> None:
    """Generate shared_entity and shared_topic edges from node metadata.

    Filters out high-frequency entities (appearing in >50% of cells) to avoid
    near-fully-connected noise graphs. Requires meaningful overlap for edge creation.
    Also reads neighbors: [] from frontmatter to create manual edges.
    """
    nodes = list(graph["nodes"].values())
    if not nodes:
        print("No nodes in graph")
        return

    n_nodes = len(nodes)

    # Compute entity frequency — filter out ubiquitous ones
    entity_freq = {}
    for node in nodes:
        for e in node.get("entities", []):
            e_lower = e.lower()
            entity_freq[e_lower] = entity_freq.get(e_lower, 0) + 1

    # Scale-aware ubiquity: at 52 cells, 50% was fine; at 600+, an entity in
    # 10% of cells still generates thousands of edges (fmn_stress 2026-07-02:
    # 57k entity edges). An entity that appears in more than max(5, 8% of n)
    # cells is a theme, not a link.
    ubiquitous = {e for e, count in entity_freq.items()
                  if count > max(5, n_nodes * 0.08)}

    # Generic entities always excluded (the two of you + configured extras)
    try:
        from fmn_config import generic_entities
        generic = generic_entities()
    except Exception:
        generic = {"mal", "q", "hermes", "sonnet", "sage", "telegram"}

    def meaningful_entities(node):
        return {e.lower() for e in node.get("entities", [])
                if e.lower() not in generic
                and e.lower() not in ubiquitous}

    # Topics get the same scale-aware ubiquity rule as entities — a tag on
    # 8%+ of the vault is a theme, not a link (fmn_stress: one shared tag
    # produced ~40k topic edges).
    topic_freq: dict[str, int] = {}
    for node in nodes:
        for t in node.get("topics", []):
            topic_freq[t.lower()] = topic_freq.get(t.lower(), 0) + 1
    ubiq_topics = {t for t, c in topic_freq.items() if c > max(5, n_nodes * 0.08)}

    def meaningful_topics(node):
        return {t.lower() for t in node.get("topics", [])
                if t.lower() not in ubiq_topics}

    added = 0

    # Shared entity/topic edges
    for i, a in enumerate(nodes):
        for b in nodes[i+1:]:
            # Shared entities (meaningful only). Threshold history: >=2 produced
            # ZERO edges on the real 52-cell vault (2026-07-01) — cells rarely
            # share two meaningful entities. >=1 with ubiquity filtering gives
            # the associative strings actual existence; weight still scales.
            shared_e = meaningful_entities(a) & meaningful_entities(b)
            if len(shared_e) >= 1:
                add_edge(graph, a["cell_id"], b["cell_id"],
                        "shared_entity", weight=len(shared_e),
                        note=f"shared: {', '.join(sorted(shared_e))}")
                added += 1

            # Shared topics (>=2 meaningful; >=3 never fired on real data)
            shared_t = meaningful_topics(a) & meaningful_topics(b)
            if len(shared_t) >= 2:
                add_edge(graph, a["cell_id"], b["cell_id"],
                        "shared_topic", weight=len(shared_t),
                        note=f"topics: {', '.join(sorted(shared_t))}")
                added += 1

    # Manual edges from neighbors: [] frontmatter
    manual_added = 0
    for node in nodes:
        neighbors = node.get("neighbors", [])
        for neighbor_id in neighbors:
            if neighbor_id in graph["nodes"]:
                add_edge(graph, node["cell_id"], neighbor_id,
                        "manual", weight=1.0,
                        note="from neighbors frontmatter")
                manual_added += 1

    # Semantic edges from the FMN embedding layer (real-valued strings)
    sem_added = 0
    try:
        import memory_embed
        for a_id, b_id, cos in memory_embed.semantic_pairs(
                memory_embed.load_store()):   # threshold: module default (calibrated)
            if a_id in graph["nodes"] and b_id in graph["nodes"]:
                add_edge(graph, a_id, b_id, "semantic_sim",
                         weight=round(cos, 3), note=f"cos={cos:.3f}")
                sem_added += 1
    except Exception:
        pass
    if sem_added:
        print(f"  + {sem_added} semantic_sim edges")

    save_graph(graph)
    print(f"OK Built {added} auto edges + {manual_added} manual-from-neighbors edges")
    print(f"  Total edges: {len(graph['edges'])}")
    print(f"  Filtered ubiquitous entities: {sorted(ubiquitous)}")


# ── Use-based verification (write-back) ─────────────────────────────────────

def touch_cell(graph: dict, cell_id: str, corrected: bool = False) -> None:
    """Record that a cell was actually used. This is the verification loop:
    trust flows from traffic, and referenced_count is what forgetting reads.

    - increments referenced_count, stamps last_referenced
    - a surfaced 'auto' (gray) cell that did NOT cause a correction earns 'checked'
    - a cell corrected in conversation is demoted to 'flagged' for human review
    Callers: dynamic recall on every surfaced cell; correction flow with corrected=True.
    """
    node = graph["nodes"].get(cell_id)
    if node is None:
        return
    node["referenced_count"] = node.get("referenced_count", 0) + 1
    node["last_referenced"] = datetime.now(timezone.utc).isoformat()
    if corrected:
        node["trust"] = "flagged"
    elif node.get("trust") == "auto":
        node["trust"] = "checked"       # earned by surviving real use


# ── Retrieval ─────────────────────────────────────────────────────────────────

def query_graph(text: str, graph: dict, limit: int = 10, touch: bool = False,
                depth: str = "auto") -> list[dict]:
    """Find relevant cells for an incoming message.

    Hybrid scoring: topic/entity matching (predictable, cannot hallucinate
    similarity) + BM25 over in-graph text (IDF-weighted keyword channel) +
    semantic cosine from the FMN embedding layer (paraphrase recall). Both
    learned channels degrade gracefully to the mechanical ones.

    depth: complexity-aware recall (recall_planner, heuristic — never an LLM
    on this path). "auto" classifies the query; or force simple/hybrid/complex.
    Depth scales BREADTH only (result limit, expansion seeds) — scoring is
    identical across depths, so a misclassified query still gets its direct
    hits, just fewer/more neighbors.
    """
    text_lower = text.lower()

    # Recall plan (graceful: no planner module -> classic behavior)
    rp, the_plan = None, None
    try:
        import recall_planner as rp
        the_plan = (rp.plan(text) if depth == "auto"
                    else {"complexity": depth, **rp.PLANS[depth]})
        limit = max(limit, int(limit * the_plan["limit_mult"]))
    except Exception:
        the_plan = None

    # Semantic layer (optional). Potion cosines are low-range (good match
    # ~0.27, noise floor ~0.20) — absolute thresholds don't separate; RANK
    # does. Only the top-5 semantic ranks earn a boost.
    sem, sem_top = {}, set()
    try:
        import memory_embed
        sem = memory_embed.semantic_scores(text, memory_embed.load_store())
        # Scale-aware gate: top-5 was tuned at ~100 cells; at 382 the right
        # answer routinely ranks 6th-15th. ~5% of vault, floor 5.
        k_sem = max(5, int(len(graph["nodes"]) * 0.05))
        sem_top = {cid for cid, s in
                   sorted(sem.items(), key=lambda x: -x[1])[:k_sem] if s >= 0.18}
    except Exception:
        sem = {}

    # BM25 channel (rank-normalized like the semantic channel: raw BM25
    # magnitudes swing with corpus stats, so the top hit anchors the scale)
    bm25 = {}
    if rp is not None:
        try:
            bm25 = rp.bm25_scores(text, graph["nodes"])
        except Exception:
            bm25 = {}
    bm25_max = max(bm25.values()) if bm25 else 0.0

    try:
        from fmn_config import generic_entities
        _generic = generic_entities()
    except Exception:
        _generic = {"mal", "q", "hermes"}

    # Score each node by keyword overlap
    scored = []
    for node in graph["nodes"].values():
        score = 0.0
        matched = []

        # Semantic similarity: top-5 rank gated; boost rivals an entity hit
        if node["cell_id"] in sem_top:
            s_cos = sem[node["cell_id"]]
            score += 2.0 + 4.0 * s_cos
            matched.append(f"semantic:{s_cos:.2f}")

        # Topic matches — topics are slugs ("model_size"); natural queries say
        # "model size". Match both forms, else underscored topics are unfindable
        # (caught by memory_eval question 3 on the suite's first run).
        for topic in node.get("topics", []):
            t = topic.lower()
            if t in text_lower or t.replace("_", " ") in text_lower:
                score += 2.0
                matched.append(f"topic:{topic}")

        # Entity matches
        for entity in node.get("entities", []):
            if entity.lower() in text_lower and entity.lower() not in _generic:
                score += 3.0
                matched.append(f"entity:{entity}")

        # Text channel. BM25 when available (IDF-weighted — a rare word
        # matching is worth more than "memory" matching everything); the old
        # flat +0.5-per-word loop only as fallback.
        if bm25_max > 0:
            b = bm25.get(node["cell_id"], 0.0)
            if b > 0:
                score += 2.5 * (b / bm25_max)
                matched.append(f"bm25:{b/bm25_max:.2f}")
        else:
            brief_lower = node.get("brief", "").lower()
            for word in text_lower.split():
                if len(word) > 4 and word in brief_lower:
                    score += 0.5

        if score > 0:
            # Boost: bright cells get 2x, high gets 1.5x
            sig = node.get("significance", "medium")
            if sig == "bright":
                score *= 2.0
            elif sig == "high":
                score *= 1.5

            # Temporal boost: fresh cells get 1.5x, recent 1.2x
            ts = node.get("temporal_status", "fresh")
            if ts == "fresh":
                score *= 1.5
            elif ts == "recent":
                score *= 1.2

            scored.append({
                "cell_id": node["cell_id"],
                "score": score,
                "matched": matched,
                "brief": node["brief"],
                "significance": node.get("significance"),
                "temporal_status": ts,
            })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Graph expansion: for top matches, pull in connected nodes. Expansion
    # breadth scales with query complexity (a relational question deserves
    # more neighborhood; a lookup doesn't need it).
    expand_top = the_plan["expand_top"] if the_plan else 3
    expanded = set()
    top_ids = [s["cell_id"] for s in scored[:expand_top]]
    for edge in graph["edges"]:
        if edge["a"] in top_ids and edge["b"] not in top_ids:
            node = graph["nodes"].get(edge["b"])
            if node:
                expanded.add(edge["b"])
        elif edge["b"] in top_ids and edge["a"] not in top_ids:
            node = graph["nodes"].get(edge["a"])
            if node:
                expanded.add(edge["a"])

    for cid in expanded:
        node = graph["nodes"][cid]
        scored.append({
            "cell_id": cid,
            "score": 0.5,  # expansion score
            "matched": ["graph_expansion"],
            "brief": node["brief"],
            "significance": node.get("significance"),
            "temporal_status": node.get("temporal_status"),
        })

    results = scored[:limit]

    # Complex queries get the living-portrait signpost: the profile layer is
    # the deepest answer to "who is she / who am I" questions. A signpost,
    # not content — the reader runs `fmn.py profile show` (THE ONE LAW's
    # expand-before-acting, applied to identity).
    if the_plan and the_plan.get("consult_profile"):
        profile_dir = VAULT_ROOT / "60_PROFILE"
        live = [p.stem for p in profile_dir.glob("personal_*.md")] \
            if profile_dir.exists() else []
        if live:
            results.append({
                "cell_id": "(profile)", "score": 0.0,
                "matched": [f"planner:{the_plan['complexity']}"],
                "brief": "Relational query — consult the living portrait(s): "
                         + ", ".join(sorted(live))
                         + "  (fmn.py profile show <subject>)",
                "significance": "signpost", "temporal_status": "-"})

    # Write-back: retrieval is the verification loop. Every surfaced cell is
    # touched — referenced_count grows (forgetting reads it), and gray 'auto'
    # cells that get used earn 'checked'. Off by default so read-only queries
    # (rumination, eval) don't mutate trust; the dynamic-recall skill sets it.
    if touch:
        for r in results:
            if r["cell_id"] in graph["nodes"]:
                touch_cell(graph, r["cell_id"])

    return results


# ── Temporal aging ────────────────────────────────────────────────────────────

def age_graph(graph: dict) -> None:
    """Update temporal_status on all nodes based on age and reference count."""
    now = datetime.now(timezone.utc)
    updated = 0

    for node in graph["nodes"].values():
        created = node.get("created", "")
        if not created:
            continue

        # Parse created timestamp
        try:
            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            elif isinstance(created, (int, float)):
                created_dt = datetime.fromtimestamp(created, tz=timezone.utc)
            else:
                continue
        except (ValueError, TypeError):
            continue

        age_days = (now - created_dt).days

        # Bright cells age slower
        if node.get("significance") == "bright":
            age_days = int(age_days / BRIGHT_AGE_MULTIPLIER)

        # Determine temporal status
        if age_days <= FRESH_DAYS:
            new_status = "fresh"
        elif age_days <= RECENT_DAYS:
            new_status = "recent"
        elif age_days <= OLD_DAYS:
            new_status = "old"
        else:
            # Archived unless frequently referenced
            if node.get("referenced_count", 0) >= 3:
                new_status = "old"  # keep alive
            else:
                new_status = "archived"

        if new_status != node.get("temporal_status"):
            node["temporal_status"] = new_status
            updated += 1

    save_graph(graph)
    print(f"OK Aged {updated} nodes")


# ── Stats & export ────────────────────────────────────────────────────────────

def print_stats(graph: dict) -> None:
    nodes = graph["nodes"]
    edges = graph["edges"]

    by_sig = {}
    by_temporal = {}
    by_semantic = {}
    refl_candidates = 0
    for n in nodes.values():
        sig = n.get("significance", "medium")
        ts  = n.get("temporal_status", "fresh")
        st  = n.get("semantic_type", "untyped")
        by_sig[sig]      = by_sig.get(sig, 0) + 1
        by_temporal[ts]  = by_temporal.get(ts, 0) + 1
        by_semantic[st]  = by_semantic.get(st, 0) + 1
        if n.get("reflection_candidate"):
            refl_candidates += 1

    by_edge_type = {}
    for e in edges:
        t = e["type"]
        by_edge_type[t] = by_edge_type.get(t, 0) + 1

    print(f"Nodes: {len(nodes)}")
    print(f"Edges: {len(edges)}")
    print(f"  by edge type:   {by_edge_type}")
    print(f"  by significance: {by_sig}")
    print(f"  by temporal:    {by_temporal}")
    print(f"  by semantic:    {by_semantic}")
    print(f"  reflection candidates: {refl_candidates}")
    print(f"Total approvals:  {graph['metadata']['total_approvals']}")
    print(f"Total retrievals: {graph['metadata']['total_retrievals']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Memory association graph")
    parser.add_argument("command", choices=[
        "init", "approve", "edge", "build-edges", "query", "age", "stats", "export"
    ])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--from", dest="source_dir", default=None)
    parser.add_argument("--type", default="manual")
    parser.add_argument("--weight", type=float, default=1.0)
    parser.add_argument("--note", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-touch", action="store_true",
                        help="query without write-back (read-only; for eval/rumination)")
    parser.add_argument("--depth", default="auto",
                        choices=["auto", "simple", "hybrid", "complex"],
                        help="recall depth (auto = heuristic classification)")
    args = parser.parse_args()

    if args.command == "init":
        save_graph(empty_graph())
        print(f"OK Empty graph initialized at {GRAPH_FILE}")

    elif args.command == "approve":
        if not args.args:
            print("Usage: approve <cell_id> [--from DIR]", file=sys.stderr)
            sys.exit(1)
        cell_id = args.args[0]
        source = Path(args.source_dir) if args.source_dir else None
        if not source:
            # Default: most recent quarantine run
            qdir = VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"
            runs = sorted(qdir.iterdir(), reverse=True)
            if runs:
                source = runs[0]
            else:
                print("No quarantine runs found. Use --from DIR", file=sys.stderr)
                sys.exit(1)
        approve_cell(cell_id, source)

    elif args.command == "edge":
        if len(args.args) < 2:
            print("Usage: edge <a> <b> --type TYPE --weight N --note '...'", file=sys.stderr)
            sys.exit(1)
        graph = load_graph()
        if not add_edge(graph, args.args[0], args.args[1],
                        args.type, args.weight, args.note):
            print(f"Error: could not add edge (node missing or pair severed)",
                  file=sys.stderr)
            sys.exit(1)
        save_graph(graph)
        print(f"OK Edge: {args.args[0]} → {args.args[1]} ({args.type}, w={args.weight})")

    elif args.command == "build-edges":
        # long read-modify-write — hold the lock so concurrent remember/panel
        # edits aren't clobbered (build_auto_edges saves internally)
        with graph_lock():
            graph = load_graph()
            build_auto_edges(graph)

    elif args.command == "query":
        if not args.args:
            print("Usage: query <text>", file=sys.stderr)
            sys.exit(1)
        graph = load_graph()
        results = query_graph(" ".join(args.args), graph, limit=args.limit,
                              touch=not args.no_touch, depth=args.depth)
        graph["metadata"]["total_retrievals"] += 1
        save_graph(graph)
        print(f"Top {len(results)} matches:")
        for r in results:
            print(f"  [{r['score']:5.1f}] {r['cell_id']} ({r['significance']}, {r['temporal_status']})")
            print(f"          {r['brief'][:100]}")
            print(f"          matched: {', '.join(r['matched'])}")

    elif args.command == "age":
        graph = load_graph()
        age_graph(graph)

    elif args.command == "stats":
        print_stats(load_graph())

    elif args.command == "export":
        graph = load_graph()
        print(json.dumps(graph, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
