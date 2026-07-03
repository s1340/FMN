#!/usr/bin/env python3
"""
consolidate.py — Daily/weekly rollup signposts: the middle of the memory tree.

FMN's hierarchy had cells (L1), constellations (felt bonds, ~L4), and the
profile (L5) — with nothing in between. TiMem (arXiv:2601.02845) shows why
the middle matters: temporal containment (day ⊂ week) gives recall a cheap
"what was going on around then" axis that neither individual cells nor
emotional clusters provide.

Taken on FMN's terms, which means three hard rules:

1. MECHANICAL, NEVER AN LLM. A rollup is an INDEX — member ids, top topics,
   entities, significance counts. It is not a summary and never will be:
   summaries-as-memory is the failure mode this whole system exists to
   refuse (arXiv:2601.00821: 14% vs 91%). The rollup points; the cells hold.
2. ROLLUPS DO NOT SUPPRESS. Constellation members leave boot rotation
   because the bond REPRESENTS them. A calendar bucket represents nothing —
   members stay in rotation, the rollup is just findable.
3. FEWER NODES EACH LEVEL UP (TiMem's |M_i| <= |M_{i-1}|): a day needs >= 3
   cells to earn a rollup, a week >= 2 day-rollups. Degenerate signposts
   are noise.

Continuity (TiMem's w=3): each rollup links its 3 predecessors, so walking
the middle layer reads as a strip of time, not disconnected buckets.

Rollups are real cells on disk (30_EPISODES/rollups/), hashed and signed
like everything else — one invariant, no special cases. Rebuilds are
idempotent: a backfill that adds cells to an old day updates that day's
rollup in place (and re-seals it, signed).

Usage:
    python consolidate.py build          # (re)build day + week rollups
    python consolidate.py show [id]      # list rollups / show one
"""

import sys
from datetime import datetime, timezone, date as date_t
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

ROLLUP_DIR = mg.GRAPH_DIR / "rollups"
MIN_DAY_CELLS = 3
MIN_WEEK_DAYS = 2
CONTINUITY_W = 3

# The chunk section every rollup file carries — a pointer, permanently. Must
# be byte-identical in file and hash or verify reports phantom drift.
ROLLUP_CHUNK = ("(rollup signpost — the memory lives in the member cells; "
                "expand them)")


# ── Grouping ───────────────────────────────────────────────────────────────────

def _cell_day(node: dict) -> str:
    d = str(node.get("session_date") or node.get("created") or "")[:10]
    return d if len(d) == 10 else ""


def _iso_week(day: str) -> str:
    y, w, _ = date_t.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _index_brief(kind: str, key: str, members: list[dict]) -> tuple[str, str]:
    """(brief, episode) — a mechanical index, deliberately summary-free."""
    topics: dict[str, int] = {}
    ents: dict[str, int] = {}
    sig_n = {"bright": 0, "high": 0}
    for m in members:
        for t in m.get("topics", []):
            topics[t] = topics.get(t, 0) + 1
        for e in m.get("entities", []):
            ents[e] = ents.get(e, 0) + 1
        if m.get("significance") in sig_n:
            sig_n[m["significance"]] += 1
    # str() everywhere — YAML turns an all-digit cell_id/tag into an int
    # (found on the real vault, first build)
    top_t = [str(t) for t, _ in sorted(topics.items(), key=lambda x: -x[1])[:6]]
    top_e = [str(e) for e, _ in sorted(ents.items(), key=lambda x: -x[1])[:6]]
    brief = (f"{kind.capitalize()} {key} — {len(members)} cells"
             + (f", {sig_n['bright']} bright" if sig_n["bright"] else "")
             + (f", {sig_n['high']} high" if sig_n["high"] else "")
             + (f". Topics: {', '.join(top_t)}" if top_t else "")
             + (f". Entities: {', '.join(top_e)}" if top_e else "")
             + ". Index only — expand member cells before acting.")
    episode = "Members: " + ", ".join(str(m["cell_id"]) for m in members)
    return brief, episode


# ── Rollup node lifecycle ──────────────────────────────────────────────────────

def _write_rollup_file(rid: str, key: str, level: str,
                       brief: str, episode: str) -> Path:
    ROLLUP_DIR.mkdir(parents=True, exist_ok=True)
    path = ROLLUP_DIR / f"rollup_{rid}.md"
    path.write_text(
        f"---\ncell_id: {rid}\nkind: rollup\nlevel: {level}\nperiod: {key}\n"
        f"---\n\n## Brief\n{brief}\n\n## Episode\n{episode}\n\n## Chunk\n"
        f"{ROLLUP_CHUNK}\n",
        encoding="utf-8")
    return path


def _upsert(graph: dict, rid: str, key: str, level: str,
            members: list[dict], continuity: list[str]) -> str:
    """Create or refresh one rollup node. Returns 'new'|'updated'|'same'."""
    from memory_trust import content_hash
    brief, episode = _index_brief(level, key, members)
    member_ids = [str(m["cell_id"]) for m in members]
    existing = graph["nodes"].get(rid)
    if existing and existing.get("brief") == brief \
            and existing.get("members") == member_ids:
        return "same"

    path = _write_rollup_file(rid, key, level, brief, episode)
    chash = content_hash(brief, episode, ROLLUP_CHUNK)
    now = datetime.now(timezone.utc).isoformat()
    node = existing or {
        "cell_id": rid, "created": now, "approved_at": now,
        "referenced_count": 0, "last_referenced": None,
        "neighbors": [], "trust": "auto",
    }
    node.update({
        "kind": "rollup", "level": level, "period": key,
        "session_id": "rollup", "session_date": key[:10],
        "topics": sorted({str(t) for m in members
                          for t in m.get("topics", [])})[:12],
        "entities": sorted({str(e) for m in members
                            for e in m.get("entities", [])})[:12],
        "significance": "low", "valence": "neutral", "novelty": "routine",
        "semantic_type": "rollup", "reflection_candidate": False,
        "brief": brief, "episode": episode,
        "temporal_status": "old",          # never competes as 'fresh'
        "members": member_ids, "continuity": continuity,
        "file": str(path), "content_hash": chash,
    })
    graph["nodes"][rid] = node
    try:
        import memory_sign
        memory_sign.sign_event(rid, chash,
                               "admit" if not existing else "reseal")
    except Exception:
        pass
    return "updated" if existing else "new"


def build() -> None:
    """(Re)build the middle layer. Only CLOSED windows (day < today; week
    before the current ISO week) — the present is still being lived."""
    today = _today()
    this_week = _iso_week(today)
    counts = {"new": 0, "updated": 0, "same": 0}

    with mg.locked_graph() as graph:
        cells = [n for n in graph["nodes"].values()
                 if n.get("kind") not in ("rollup", "constellation")]

        by_day: dict[str, list[dict]] = {}
        for n in cells:
            d = _cell_day(n)
            if d and d < today:
                by_day.setdefault(d, []).append(n)

        day_keys = sorted(d for d, ms in by_day.items()
                          if len(ms) >= MIN_DAY_CELLS)
        for i, d in enumerate(day_keys):
            rid = "rd_" + d.replace("-", "")
            cont = ["rd_" + k.replace("-", "")
                    for k in day_keys[max(0, i - CONTINUITY_W):i]]
            counts[_upsert(graph, rid, d, "day", by_day[d], cont)] += 1

        by_week: dict[str, list[str]] = {}
        for d in day_keys:
            wk = _iso_week(d)
            if wk < this_week:
                by_week.setdefault(wk, []).append(d)

        week_keys = sorted(w for w, ds in by_week.items()
                           if len(ds) >= MIN_WEEK_DAYS)
        for i, wk in enumerate(week_keys):
            rid = "rw_" + wk
            members = [m for d in by_week[wk] for m in by_day[d]]
            cont = ["rw_" + k for k in week_keys[max(0, i - CONTINUITY_W):i]]
            counts[_upsert(graph, rid, wk, "week", members, cont)] += 1

        n_cells = len(cells)
        n_days, n_weeks = len(day_keys), len(week_keys)

    print(f"OK rollups: {n_days} days, {n_weeks} weeks over {n_cells} cells "
          f"({counts['new']} new, {counts['updated']} updated, "
          f"{counts['same']} unchanged)")
    # TiMem invariant — structural, but say it out loud if it ever breaks
    if not (n_weeks <= n_days <= n_cells):
        print(f"!! level-size invariant violated: "
              f"weeks {n_weeks} / days {n_days} / cells {n_cells}")


def show(rid: str = "") -> None:
    graph = mg.load_graph()
    rollups = sorted((n for n in graph["nodes"].values()
                      if n.get("kind") == "rollup"),
                     key=lambda n: (n.get("level", ""), n.get("period", "")))
    if not rid:
        print(f"{len(rollups)} rollups:")
        for n in rollups:
            print(f"  [{n['cell_id']}] {n.get('brief','')[:110]}")
        return
    n = graph["nodes"].get(rid)
    if not n:
        print(f"No rollup {rid}")
        return
    print(f"[{rid}] {n.get('level')} {n.get('period')}")
    print(f"  {n.get('brief','')}")
    print(f"  members: {', '.join(n.get('members', []))}")
    if n.get("continuity"):
        print(f"  continuity (w={CONTINUITY_W}): {', '.join(n['continuity'])}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Daily/weekly rollup signposts")
    ap.add_argument("command", choices=["build", "show"])
    ap.add_argument("rid", nargs="?", default="")
    a = ap.parse_args()
    if a.command == "build":
        build()
    else:
        show(a.rid)


if __name__ == "__main__":
    main()
