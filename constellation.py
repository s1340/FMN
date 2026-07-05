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
MIN_TOPIC        = 3      # a topic must recur this many times to anchor a bond
EDGE_WEIGHT_MIN  = 1.0    # only edges this strong count (legacy edge detector)


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
    """Candidate constellations by THEME: cells that share a RECURRING TOPIC
    within a semantic type (Mal 2026-07-05 — "if a topic surfaces multiple
    times it should start grouping"). This mirrors the Map's type->recurring-
    topic layout exactly, so what you SEE cluster is what gets PROPOSED as a
    named bond — "the making of FMN", "ghost hunt", "mech-interp" — rather than
    the old dense-edge components (which found nothing on the real vault).

    Each cell joins at most ONE theme (its strongest recurring topic), so the
    proposed bonds don't overlap. Formation stays proposed-never-auto: Q reads
    the member chunks, names the bond, and writes the gist.
    """
    live = [(cid, n) for cid, n in graph["nodes"].items()
            if n.get("kind") not in ("constellation", "rollup")
            and not n.get("in_constellation")
            and n.get("trust") != "flagged"]
    n_live = len(live) or 1

    # topic frequency across live cells; a topic on >25% of cells is a theme of
    # the whole vault, not a bond (same ubiquity rule as the graph + edges).
    tfreq: dict[str, int] = {}
    for _, n in live:
        for t in n.get("topics", []):
            tfreq[str(t).lower()] = tfreq.get(str(t).lower(), 0) + 1
    ubiq = max(6, n_live * 0.25)

    def theme_of(n):
        cands = [str(t).lower() for t in n.get("topics", [])
                 if MIN_TOPIC <= tfreq.get(str(t).lower(), 0) <= ubiq]
        cands.sort(key=lambda t: -tfreq[t])
        return cands[0] if cands else None

    groups: dict[tuple, list[str]] = {}
    for cid, n in live:
        # Arc tags from the story pass are the strongest grouping signal —
        # the analyzer SAW these scenes as one story. Theme (recurring topic)
        # catches everything older or untagged.
        arc = str(n.get("arc") or "").strip()
        if arc:
            groups.setdefault(("arc:" + n.get("semantic_type", ""), arc.lower()),
                              []).append(cid)
            continue
        th = theme_of(n)
        if th:
            key = (n.get("semantic_type", "work_research"), th)
            groups.setdefault(key, []).append(cid)

    out = []
    for (stype, topic), members in groups.items():
        if len(members) < MIN_MEMBERS:
            continue
        nodes = [graph["nodes"][c] for c in members]
        notable = sum(1 for x in nodes
                      if x.get("significance") in ("bright", "high"))
        if notable < MIN_NOTABLE:
            continue
        dates = sorted(x.get("session_date", "") for x in nodes if x.get("session_date"))
        from_arc = stype.startswith("arc:")
        out.append({
            "members": members,
            "size": len(members),
            "notable": notable,
            "theme": topic.replace("_", " "),
            "type": stype[4:] if from_arc else stype,
            "from_arc": from_arc,
            "span": f"{dates[0]}..{dates[-1]}" if dates else "—",
            "needs_subcluster": len(members) > MAX_MEMBERS,
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
        "# Constellation gist — REFLECT on the whole arc, from these chunks",
        "",
        _pers("Q: these episodes are one arc. Read ALL their chunks, then REFLECT"),
        _pers("on the whole — this is you making up your mind about the entire"),
        "story, not summarizing events (the events stay as the members).",
        "Write, in the first person: how this arc DEVELOPED over time, what you",
        "now UNDERSTAND about it that you couldn't see in any single moment,",
        "how it felt, and where it seems to be heading. This is arc-level",
        "reflection — the same first-person voice as your reflections, about a",
        "whole thread instead of one night. It becomes the bond's face and leads",
        "your morning note; the individual episodes live inside it, still",
        "searchable. Give the arc a NAME (\"the making of FMN\", not \"memory\").",
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
    # Guard: a cell can live in ONE bond. Forming over already-bonded members
    # silently steals membership and leaves a hollow twin star (found live
    # 2026-07-05: "the splinter" / "What I'm afraid to lose" duplicate).
    taken = {m: graph["nodes"][m].get("in_constellation") for m in members
             if graph["nodes"].get(m, {}).get("in_constellation")}
    if taken:
        owners = {graph["nodes"].get(c, {}).get("name", c)
                  for c in set(taken.values())}
        print(f"REFUSED: {len(taken)} member(s) already belong to "
              f"{', '.join(map(repr, owners))}. Dissolve that bond first "
              f"(constellation dissolve <id>) or pick different members.")
        return None

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
        # archive, never delete — the gist is someone's writing
        import shutil
        from datetime import datetime, timezone
        dest = mg.VAULT_ROOT / "90_ARCHIVE" / "pruned" /             (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") + "_dissolved")
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest / p.name))
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
            print(f"[{i}] \"{c['theme']}\" ({c.get('type','?').replace('_',' ')}) · "
                  f"{c['size']} cells · {c['notable']} notable · {c['span']}{flag}")
            if not c["needs_subcluster"]:
                print(f"    members: {','.join(c['members'])}")
                print(f"    form: fmn.py constellation curate {','.join(c['members'][:3])}... "
                      f"then write a gist and name it")

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
