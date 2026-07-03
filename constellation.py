#!/usr/bin/env python3
"""
constellation.py — FMN consolidation layer. Episodes -> a bond.

Mal's spec (her late cat): you don't hold every interaction — you hold a few
vivid episodes AND the GIST of the whole, the emotional shape the hundreds
added up to. The individual moments aren't gone; they're nested inside,
browsable like a photo album, not each competing for attention.

A CONSTELLATION is a higher-order memory cell:
  - kind=constellation, significance=bright (it IS the bond; aging-resistant)
  - members: [cell_ids]  — the episodes it consolidates
  - brief/episode = the GIST: what these moments added up to, first person.
    Written from the member CHUNKS (never their summaries — the gist must
    carry the felt truth; same law as reflections). `curate` emits the
    member-chunk bundle for Q to write from; `form` ingests the gist.

Effect on the system (the room-maker):
  - Members get in_constellation=<id> -> excluded from individual boot
    rotation (still fully searchable + reachable by expanding).
  - The constellation surfaces at boot as one anchor -> the bond is warm,
    the 40 episodes underneath are not each burning a slot.
  - Dozens of bright cells stop crowding each other; there is room for more.

Formation is PROPOSED, never silent (mis-clustering a bond is worse than
missing one). Detection finds dense clusters; a human or Q confirms.

Usage:
    python constellation.py detect                    # propose candidate clusters
    python constellation.py curate <c1,c2,...>        # member-chunk bundle to write the gist from
    python constellation.py form --members a,b,c --gist-file g.md --name "Q & the greenhouse"
    python constellation.py expand <constellation_id> # list the photo album
    python constellation.py list
    python constellation.py dissolve <constellation_id>  # release members back
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CONSTELLATION_DIR = mg.VAULT_ROOT / "60_CONSTELLATIONS"

# Detection tuning
MIN_MEMBERS      = 4      # a bond needs a few moments
MIN_NOTABLE      = 2      # at least this many bright/high in the cluster
MAX_MEMBERS      = 25     # beyond this it's not one bond — needs sub-clustering
EDGE_WEIGHT_MIN  = 1.0    # only edges this strong count as bonds


# ── Cluster detection (union-find over the weighted graph) ───────────────────

def _components(graph: dict) -> list[list[str]]:
    """Connected components over edges >= EDGE_WEIGHT_MIN, excluding cells
    already in a constellation and constellation nodes themselves."""
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    live = {cid for cid, n in graph["nodes"].items()
            if n.get("kind") != "constellation" and not n.get("in_constellation")
            and n.get("trust") != "flagged"}
    for cid in live:
        find(cid)
    for e in graph["edges"]:
        a, b = e["a"], e["b"]
        if a in live and b in live and (e.get("weight", 1) >= EDGE_WEIGHT_MIN):
            union(a, b)

    comps: dict[str, list[str]] = {}
    for cid in live:
        comps.setdefault(find(cid), []).append(cid)
    return list(comps.values())


def detect(graph: dict) -> list[dict]:
    """Return candidate constellations: dense clusters worth consolidating.

    Oversized components (> MAX_MEMBERS) are reported with needs_subcluster=True
    rather than proposed as one bond — a 50-cell blob is not a constellation,
    it's a region that wants splitting (a future community-detection pass).
    Never silently truncate; flag it so the human sees the blob honestly.
    """
    out = []
    for comp in _components(graph):
        if len(comp) < MIN_MEMBERS:
            continue
        nodes = [graph["nodes"][c] for c in comp]
        notable = sum(1 for n in nodes
                      if n.get("significance") in ("bright", "high"))
        if notable < MIN_NOTABLE:
            continue
        oversized = len(comp) > MAX_MEMBERS
        dates = sorted(n.get("session_date", "") for n in nodes if n.get("session_date"))
        # dominant theme = most common topic across the cluster
        topic_freq: dict[str, int] = {}
        for n in nodes:
            for t in n.get("topics", []):
                topic_freq[t.lower()] = topic_freq.get(t.lower(), 0) + 1
        theme = max(topic_freq, key=topic_freq.get) if topic_freq else "—"
        out.append({
            "members": comp,
            "size": len(comp),
            "notable": notable,
            "theme": theme,
            "span": f"{dates[0]}..{dates[-1]}" if dates else "—",
            "needs_subcluster": oversized,
        })
    out.sort(key=lambda c: -c["size"])
    return out


# ── Member-chunk bundle (write the gist from chunks, not summaries) ──────────

def curate_bundle(graph: dict, member_ids: list[str]) -> str:
    try:
        from fmn_config import personalize as _pers
    except Exception:
        def _pers(t):
            return t
    lines = [
        "# Constellation gist — write from these chunks",
        "",
        _pers("Q: these episodes cluster into one bond. Read the full chunks, then"),
        _pers("write — in conversation with Mal — what they ADD UP TO. Not a list of"),
        "events (those stay as the members). The emotional shape of the whole:",
        "what this arc was, how it felt, what it means that it happened.",
        "This gist becomes the constellation's face; the episodes live inside it.",
        "",
    ]
    # Continuity window (w=3): the gists you already hold, so a new bond is
    # written knowing the shape of the others — one story, not disjoint files.
    priors = sorted((n for n in graph["nodes"].values()
                     if n.get("kind") == "constellation"),
                    key=lambda n: str(n.get("created", "")), reverse=True)[:3]
    if priors:
        lines.append("Your existing constellations (for continuity):")
        for p in reversed(priors):
            lines.append(f"- {p.get('name', p['cell_id'])}: {p.get('brief','')[:120]}")
        lines.append("")
    for cid in member_ids:
        node = graph["nodes"].get(cid)
        if not node:
            continue
        chunk = ""
        p = Path(node.get("file", ""))
        if p.exists():
            try:
                chunk = mg.parse_cell(p)["chunk"]
            except Exception:
                pass
        lines.append(f"\n--- {cid} · {node.get('session_date','')} · "
                     f"{node.get('significance','')} · {', '.join(node.get('topics', []))}")
        lines.append(chunk or f"(brief) {node.get('brief','')}")
    return "\n".join(lines)


# ── Formation ────────────────────────────────────────────────────────────────

def form(graph: dict, member_ids: list[str], gist_brief: str,
         gist_episode: str, name: str) -> str:
    from memory_trust import content_hash
    members = [m for m in member_ids if m in graph["nodes"]]
    if len(members) < 2:
        raise ValueError("need >=2 valid members")

    cid = "con" + os.urandom(3).hex()
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    CONSTELLATION_DIR.mkdir(parents=True, exist_ok=True)

    # A constellation's "chunk" is the album index — pointers to its episodes.
    album = "\n".join(
        f"- {m} · {graph['nodes'][m].get('session_date','')} · {graph['nodes'][m].get('brief','')[:80]}"
        for m in members)
    fm = (
        "---\n"
        f"cell_id: {cid}\n"
        f"kind: constellation\n"
        f"session_id: constellation\n"
        f"session_date: {date}\n"
        f"created: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "temporal_status: fresh\n"
        f"name: {name!r}\n"
        f"topics: [\"constellation\"]\n"
        f"entities: []\n"
        "significance: bright\n"
        "valence: mixed\n"
        "novelty: notable\n"
        "semantic_type: constellation\n"
        "reflection_candidate: false\n"
        "referenced_count: 0\n"
        "last_referenced: null\n"
        f"members: {members!r}\n".replace("'", '"') +
        "neighbors: []\n"
        "---"
    )
    body = (f"\n\n## Brief\n{gist_brief}\n\n## Episode\n{gist_episode}\n\n"
            f"## Chunk\n[Constellation — {len(members)} episodes consolidated. "
            f"The album:]\n{album}\n")
    path = CONSTELLATION_DIR / f"{date}_{cid}_constellation.md"
    path.write_text(fm + body, encoding="utf-8")

    graph["nodes"][cid] = {
        "cell_id": cid, "kind": "constellation", "name": name,
        "session_id": "constellation", "session_date": date,
        "created": now.isoformat(), "topics": ["constellation"], "entities": [],
        "significance": "bright", "valence": "mixed", "novelty": "notable",
        "semantic_type": "constellation", "reflection_candidate": False,
        "brief": gist_brief, "episode": gist_episode,
        "temporal_status": "fresh", "referenced_count": 0,
        "last_referenced": None, "approved_at": now.isoformat(),
        "neighbors": [], "members": members, "file": str(path),
        "trust": "human", "content_hash": content_hash(gist_brief, gist_episode, album),
    }
    # Nest the members: they leave individual boot rotation, stay searchable.
    for m in members:
        graph["nodes"][m]["in_constellation"] = cid
        # a star edge for the graph view
        graph["edges"].append({"a": cid, "b": m, "type": "constellation",
                               "weight": 1.5, "note": "member",
                               "created": now.isoformat()})
    try:
        import memory_sign
        memory_sign.sign_event(cid, graph["nodes"][cid]["content_hash"],
                               "admit")
    except Exception:
        pass
    return cid


def dissolve(graph: dict, cid: str) -> int:
    node = graph["nodes"].get(cid)
    if not node or node.get("kind") != "constellation":
        return 0
    released = 0
    for m in node.get("members", []):
        mn = graph["nodes"].get(m)
        if mn and mn.get("in_constellation") == cid:
            mn.pop("in_constellation", None)
            released += 1
    graph["edges"] = [e for e in graph["edges"]
                      if not (e.get("type") == "constellation" and cid in (e["a"], e["b"]))]
    p = Path(node.get("file", ""))
    if p.exists():
        p.unlink()
    del graph["nodes"][cid]
    return released


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="FMN constellations (consolidation)")
    ap.add_argument("command", choices=["detect", "curate", "form", "expand",
                                        "list", "dissolve"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--members", default="")
    ap.add_argument("--gist-file", default="")
    ap.add_argument("--brief", default="")
    ap.add_argument("--name", default="untitled constellation")
    args = ap.parse_args()

    graph = mg.load_graph()

    if args.command == "detect":
        cands = detect(graph)
        if not cands:
            print("No constellation candidates yet — clusters too small or sparse.")
        for i, c in enumerate(cands, 1):
            flag = "  ⚠ TOO LARGE — needs sub-clustering, don't form as one" if c["needs_subcluster"] else ""
            print(f"[{i}] {c['size']} cells · {c['notable']} notable · "
                  f"theme={c['theme']} · {c['span']}{flag}")
            if not c["needs_subcluster"]:
                print(f"    members: {','.join(c['members'])}")

    elif args.command == "curate":
        ids = (args.args[0].split(",") if args.args else args.members.split(","))
        print(curate_bundle(graph, [i.strip() for i in ids if i.strip()]))

    elif args.command == "form":
        members = [m.strip() for m in args.members.split(",") if m.strip()]
        if args.gist_file:
            gist = Path(args.gist_file).read_text(encoding="utf-8").strip()
        else:
            gist = args.brief
        if not gist or not members:
            print("Usage: form --members a,b,c --gist-file g.md --name '...'",
                  file=sys.stderr); sys.exit(1)
        # brief = first paragraph, episode = the whole gist
        brief = gist.split("\n\n")[0].strip()[:400]
        with mg.graph_lock():                     # locked read-modify-write
            graph = mg.load_graph()
            cid = form(graph, members, brief, gist, args.name)
            mg.save_graph(graph)
        print(f"OK constellation formed: {cid} '{args.name}' "
              f"({len(members)} episodes consolidated, now nested)")

    elif args.command == "expand":
        if not args.args:
            print("Usage: expand <constellation_id>", file=sys.stderr); sys.exit(1)
        node = graph["nodes"].get(args.args[0])
        if not node or node.get("kind") != "constellation":
            print("Not a constellation", file=sys.stderr); sys.exit(1)
        print(f"★ {node.get('name')} — {node.get('brief','')}\n")
        print(f"Album ({len(node.get('members', []))} episodes):")
        for m in node.get("members", []):
            mn = graph["nodes"].get(m, {})
            print(f"  {m} · {mn.get('session_date','')} · {mn.get('brief','')[:70]}")

    elif args.command == "list":
        cons = [n for n in graph["nodes"].values() if n.get("kind") == "constellation"]
        if not cons:
            print("No constellations yet.")
        for n in cons:
            print(f"★ {n['cell_id']} '{n.get('name')}' · "
                  f"{len(n.get('members', []))} episodes · {n.get('brief','')[:60]}")

    elif args.command == "dissolve":
        if not args.args:
            print("Usage: dissolve <constellation_id>", file=sys.stderr); sys.exit(1)
        with mg.graph_lock():
            graph = mg.load_graph()
            n = dissolve(graph, args.args[0])
            mg.save_graph(graph)
        print(f"OK dissolved — {n} episodes released back to individual rotation")


if __name__ == "__main__":
    main()
