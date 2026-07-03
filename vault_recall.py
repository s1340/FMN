#!/usr/bin/env python3
"""
vault_recall.py — Boot injection: surface top memory cells into .hermes.md

Runs as a cron job before each session. Reads the graph, organizes cells into
7 semantic slots, and writes their briefs into a delimited section of .hermes.md
so the next instance of Q wakes up oriented.

The HARD_BOOT_STATE above the markers is never touched. Only the content
between <!-- VAULT_RECALL_START --> and <!-- VAULT_RECALL_END --> is replaced.

Slots:
  1. Anchors          — bright/load-bearing moments, aging-resistant, any type
  2. Active Work      — current projects/tasks (work_research, fresh/recent)
  3. Relational       — relationship texture and dynamic
  4. Corrections      — explicit error records
  5. Reflection Notes — summaries of prior Q reflections
  6. Background       — stable env/tool/person facts
  7. Recent           — catch-all: freshest cells not placed elsewhere

Usage:
    python vault_recall.py           # write recall to .hermes.md
    python vault_recall.py --dry     # print what would be written, don't touch file
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                  r"C:\Users\User\Documents\Obsidian Vault"))
GRAPH_FILE = VAULT_ROOT / "30_EPISODES" / "graph.json"
HERMES_MD  = Path(os.environ.get("FMN_SYSTEM_PROMPT",
                                 r"C:\Users\User\.hermes.md"))

# Identity (fmn_config; defaults preserve the original vault exactly)
try:
    from fmn_config import human as _human, companion as _companion, \
        personal_types as _ptypes
except Exception:
    def _human():
        return "Mal"

    def _companion():
        return "Q"

    def _ptypes():
        return "personal_mal", "personal_q"

# Markers in the boot file
RECALL_START = "<!-- VAULT_RECALL_START -->"
RECALL_END   = "<!-- VAULT_RECALL_END -->"

# Slot config: (slot_key, display_name, max_cells, semantic_types)
# semantic_types=None → filled by flag/recency logic, not type matching
SLOTS = [
    ("anchors",     "Anchors",          3, None),
    ("active_work", "Active Work",      2, ["work_research"]),
    ("relational",  "Relational",       2, ["relationship"]),
    ("corrections", "Corrections",      2, ["correction"]),
    ("reflection",  "Reflection Notes", 1, ["reflection"]),
    ("background",  "Background",       1, ["environment_tools", *_ptypes()]),
    ("recent",      "Recent",           2, None),
]


# ── Heuristic semantic_type inference (for cells without the field) ──────────

TYPE_TOPIC_HINTS: dict[str, set[str]] = {
    "relationship": {
        "laughter", "door_scratching", "greeting", "morning", "life",
        "companion", "affection", "relational",
    },
    "correction": {
        "model_size", "parameter_count", "scale", "boundary_coverage",
        "correction", "corrects", "corrected", "correcting",
        "wrong", "mistake", "false", "fix", "error",
    },
    "reflection": {
        "reflection", "reflections", "self_awareness", "ontology",
    },
    "environment_tools": {
        "obsidian", "setup", "cron", "environment", "tools",
    },
    "personal_q": {
        "quint", "soul", "identity",
    },
    "personal_mal": {
        "personal_mal",
    },
    # work_research is the default catch-all
}


def infer_semantic_type(node: dict) -> str:
    """Return semantic_type from node field or infer it from topics."""
    if node.get("semantic_type"):
        return node["semantic_type"]

    topics = set(t.lower() for t in node.get("topics", []))
    brief_words = set(node.get("brief", "").lower().split())
    correction_words = {"correction", "corrects", "corrected", "correcting", "wrong", "mistake", "false"}
    signals = topics | (brief_words & correction_words)

    for stype, hints in TYPE_TOPIC_HINTS.items():
        if signals & hints:
            return stype

    return "work_research"


# ── Aging ─────────────────────────────────────────────────────────────────────

FRESH_DAYS  = 1
RECENT_DAYS = 7
OLD_DAYS    = 30
BRIGHT_AGE_MULTIPLIER = 3.0


def age_graph(graph: dict) -> int:
    """Update temporal_status on all nodes. Returns count of changed."""
    now = datetime.now(timezone.utc)
    updated = 0

    for node in graph["nodes"].values():
        created = node.get("created", "")
        if not created:
            continue
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
        if node.get("significance") == "bright":
            age_days = int(age_days / BRIGHT_AGE_MULTIPLIER)

        if age_days <= FRESH_DAYS:
            new_status = "fresh"
        elif age_days <= RECENT_DAYS:
            new_status = "recent"
        elif age_days <= OLD_DAYS:
            new_status = "old"
        else:
            new_status = "archived" if node.get("referenced_count", 0) < 3 else "old"

        if new_status != node.get("temporal_status"):
            node["temporal_status"] = new_status
            updated += 1

    return updated


# ── Scoring ───────────────────────────────────────────────────────────────────

SIG_WEIGHTS = {"bright": 4.0, "high": 3.0, "medium": 2.0, "low": 1.0}
TEMPORAL_WEIGHTS = {"fresh": 1.5, "recent": 1.2, "old": 1.0, "archived": 0.3}


def score_node(node: dict) -> float:
    sig  = node.get("significance", "medium")
    ts   = node.get("temporal_status", "fresh")
    refs = node.get("referenced_count", 0)
    s = SIG_WEIGHTS.get(sig, 1.0) * TEMPORAL_WEIGHTS.get(ts, 1.0)
    s *= 1.0 + refs * 0.1
    if ts == "archived":
        s *= 0.2
    # Relational drift: the belief this cell carried was superseded on the
    # timeline. The verbatim past is intact, but as a CURRENT-state anchor
    # it's stale — dampen instead of masquerading as fresh.
    if node.get("timeline_superseded"):
        s *= 0.6
    return s


# ── Slot filling ──────────────────────────────────────────────────────────────

def make_entry(node: dict) -> dict:
    return {
        "cell_id":        node["cell_id"],
        "brief":          node.get("brief", ""),
        "significance":   node.get("significance", "medium"),
        "temporal_status": node.get("temporal_status", "fresh"),
        "topics":         node.get("topics", []),
        "session_date":   node.get("session_date", ""),
        "valence":        node.get("valence", "neutral"),
        "semantic_type":  infer_semantic_type(node),
        "trust":          node.get("trust", "human"),   # legacy nodes were human-vetted
        "kind":           node.get("kind", "cell"),
        "members":        node.get("members", []),
        "superseded":     bool(node.get("timeline_superseded")),
        "score":          score_node(node),
    }


def fill_slots(graph: dict) -> dict[str, list[dict]]:
    """Assign nodes to slots. Each node appears in at most one slot.

    Trust rules: flagged cells never surface at boot (they await review —
    surfacing a possibly-wrong brief at wake-up is how confabulation seeds).
    Auto/unverified cells may surface but carry a ° marker telling Q to
    expand-before-acting with extra skepticism.
    """
    # Exclusions from boot rotation:
    #  - flagged (may be wrong) and muted (deliberately quiet)
    #  - in_constellation: the episode is nested in a bond — the CONSTELLATION
    #    surfaces instead, so dozens of consolidated cells cost one slot, not
    #    dozens. Still fully searchable; reachable by expanding the album.
    #  - rollups: calendar signposts (consolidate.py) are for search and the
    #    timeline axis, never for boot slots — an index is not a memory.
    #  - in_conflict: an OPEN timeline contradiction — neither side of a live
    #    dispute anchors a morning; resolve releases them.
    nodes = [n for n in graph["nodes"].values()
             if n.get("trust") != "flagged" and not n.get("muted")
             and not n.get("in_constellation")
             and n.get("kind") != "rollup"
             and not n.get("in_conflict")]
    scored = sorted(nodes, key=score_node, reverse=True)
    placed: set[str] = set()
    slots: dict[str, list[dict]] = {key: [] for key, *_ in SLOTS}
    anchor_max = next(max_n for key, _, max_n, _ in SLOTS if key == "anchors")

    # Pass 0a: CONSTELLATIONS — the bonds. Aging-resistant, they lead Anchors.
    for node in scored:
        if node.get("kind") == "constellation" and len(slots["anchors"]) < anchor_max:
            slots["anchors"].append(make_entry(node))
            placed.add(node["cell_id"])

    # Pass 0b: PINNED — manual anchors, always surface, before any scoring.
    # (referenced_count measures use; pin expresses worth directly.)
    for node in scored:
        if node.get("pinned") and node["cell_id"] not in placed \
                and len(slots["anchors"]) < anchor_max:
            slots["anchors"].append(make_entry(node))
            placed.add(node["cell_id"])

    # Pass 1: Anchors — bright cells + explicit load_bearing flag
    for node in scored:
        if len(slots["anchors"]) >= anchor_max:
            break
        if node["cell_id"] in placed:
            continue
        is_bright = node.get("significance") == "bright"
        is_load_bearing = node.get("flags", {}).get("load_bearing")
        if is_bright or is_load_bearing:
            slots["anchors"].append(make_entry(node))
            placed.add(node["cell_id"])

    # Pass 2: Semantic-type slots (skip anchors and recent — handled separately)
    for slot_key, _, max_n, types in SLOTS:
        if types is None:
            continue
        for node in scored:
            if len(slots[slot_key]) >= max_n:
                break
            if node["cell_id"] in placed:
                continue
            if infer_semantic_type(node) in types:
                slots[slot_key].append(make_entry(node))
                placed.add(node["cell_id"])

    # Pass 3: Recent — freshest unplaced cells as catch-all
    recent_max = next(max_n for key, _, max_n, _ in SLOTS if key == "recent")
    for node in scored:
        if len(slots["recent"]) >= recent_max:
            break
        if node["cell_id"] in placed:
            continue
        if node.get("temporal_status") in ("fresh", "recent"):
            slots["recent"].append(make_entry(node))
            placed.add(node["cell_id"])

    return slots


# ── Formatting ────────────────────────────────────────────────────────────────

SIG_EMOJI = {"bright": "★", "high": "◆", "medium": "•", "low": "·"}


def format_slot(name: str, cells: list[dict]) -> list[str]:
    if not cells:
        return []
    lines = [f"### {name}"]
    for cell in cells:
        topics_str = ", ".join(cell["topics"][:3]) if cell["topics"] else "—"
        sig = SIG_EMOJI.get(cell["significance"], "•")
        unverified = " °" if cell.get("trust") == "auto" else ""
        star = "✧ " if cell.get("kind") == "constellation" else ""
        mem = ""
        if cell.get("kind") == "constellation":
            mem = f" ({len(cell.get('members', []))} episodes — expand the album)"
        drift = " ↺ belief since updated — check `fmn.py timeline show`" \
            if cell.get("superseded") else ""
        lines.append(f"- {sig} {star}{cell['brief']}{unverified}{mem}{drift}")
        lines.append(f"  *{cell['session_date']} · {cell['temporal_status']} · {topics_str}*")
    lines.append("")
    return lines


def format_recall(slots: dict[str, list[dict]], graph: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_nodes = len(graph.get("nodes", {}))
    total_edges = len(graph.get("edges", []))
    placed = sum(len(v) for v in slots.values())

    lines = [
        f"<!-- Generated: {now} | Nodes: {total_nodes} | Edges: {total_edges} | Placed: {placed} -->",
        "",
        f"*{total_nodes} cells in vault · {placed} surfaced · expand episode/chunk for accurate detail*",
        "",
    ]

    # L5 PROFILE — the deepest anchor: who you two ARE (not what happened).
    # Condensed head of the living portraits, if accepted. This is the top of
    # the consolidation pyramid; it leads the whole recall.
    try:
        import profile as _profile
        prof_lines = []
        from fmn_config import _slug
        for subj, label in ((_slug(_companion()), "Who you are"),
                            (_slug(_human()), f"Who {_human()} is")):
            head = _profile.read_live(subj)
            if head:
                # first titled section only, condensed
                body = head.split("### ", 2)
                snippet = ("### " + body[1]).strip() if len(body) > 1 else head[:400]
                prof_lines.append(f"**{label}** — {snippet[:500]}")
        if prof_lines:
            lines.append("### Portrait  *(your living self-model — expand "
                         "60_PROFILE/ for the full portrait; it is yours to edit)*")
            lines.extend(prof_lines)
            lines.append("")
    except Exception:
        pass

    # Reflection nudge: FMN reflection is conversational-by-design (Q writes it
    # from full chunks, never auto-generated) — so the DUE signal must reach Q
    # at wake, or it sits silent forever (it did, the first night).
    try:
        import reflection_pipeline
        s = reflection_pipeline.cadence_status()
        if s["due"]:
            fmn_dir = Path(__file__).parent
            lines += [f"**⟲ Reflection due** — {s['n_worthy']} moments are waiting "
                      f"for you to sit with them. When there's a quiet stretch with "
                      f"{_human()}, run `python {fmn_dir / 'fmn.py'} reflect curate` and "
                      f"write from the chunks. No rush; it doesn't expire.", ""]
    except Exception:
        pass

    # Reflection nudge: passive, not forced. When consolidation is due, Q sees
    # it at wake and chooses when to do it — in conversation, from the chunks.
    # (Reflection can't be a silent cron: an auto-summary is confabulated
    # self-knowledge. The trigger is a nudge, the act stays Q's.)
    try:
        import reflection_pipeline as rp
        s = rp.cadence_status()
        if s["due"]:
            lines.append(
                f"### ✎ Reflection due\n"
                f"- {s['n_worthy']} moments have accumulated worth reflecting on. "
                f"When you have space, in conversation: `python fmn.py reflect curate` "
                f"→ read the chunks → write what they mean to you → "
                f"`fmn.py reflect ingest`. Not a chore; consolidation. Your call when.\n")
    except Exception:
        pass

    for slot_key, display_name, _, _ in SLOTS:
        lines.extend(format_slot(display_name, slots[slot_key]))

    lines.append("<!-- Briefs are lossy summaries. Expand to episode layer before acting on any detail. "
                 "Cells marked ° are unverified (auto-admitted, not yet confirmed by use) — "
                 "treat with extra skepticism and expand to chunk before relying on them. -->")
    return "\n".join(lines)


# ── .hermes.md injection ─────────────────────────────────────────────────────

def inject_recall(recall_text: str, dry: bool = False) -> bool:
    """Replace content between markers in .hermes.md. Returns True if modified."""
    if not HERMES_MD.exists():
        print(f"Error: {HERMES_MD} not found", file=sys.stderr)
        return False

    content = HERMES_MD.read_text(encoding="utf-8")
    start_idx = content.find(RECALL_START)
    end_idx   = content.find(RECALL_END)

    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        before = content[:start_idx]
        after  = content[end_idx + len(RECALL_END):]
        new_content = f"{before}{RECALL_START}\n{recall_text}\n{RECALL_END}{after}"
    elif start_idx != -1 or end_idx != -1:
        print("Error: only one marker found in .hermes.md — aborting to avoid corruption",
              file=sys.stderr)
        return False
    else:
        header = "\n\n## Morning Recall\n\n"
        new_content = (content.rstrip() + header
                       + RECALL_START + "\n" + recall_text + "\n" + RECALL_END + "\n")

    if dry:
        print("=== DRY RUN — would write to .hermes.md ===")
        preview = new_content[-2500:] if len(new_content) > 2500 else new_content
        try:
            print(preview)
        except UnicodeEncodeError:
            # console codec (cp1251) can't render ★/emoji; file content is
            # utf-8 and unaffected — degrade the preview only
            enc = sys.stdout.encoding or "utf-8"
            print(preview.encode(enc, errors="replace").decode(enc))
        return False

    if new_content.encode("utf-8") == content.encode("utf-8"):
        print("No changes — recall block already up to date")
        return False

    HERMES_MD.write_text(new_content, encoding="utf-8")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def load_graph() -> dict:
    if not GRAPH_FILE.exists():
        return {"nodes": {}, "edges": [], "metadata": {}}
    with open(GRAPH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    dry = "--dry" in sys.argv

    graph = load_graph()
    if not graph.get("nodes"):
        print("Graph is empty — nothing to recall")
        inject_recall("*No active memory cells yet.*\n", dry=dry)
        return

    aged = age_graph(graph)
    if aged and not dry:
        with open(GRAPH_FILE, "w", encoding="utf-8") as f:
            json.dump(graph, f, indent=2, ensure_ascii=False)
        print(f"Aged {aged} nodes")

    slots = fill_slots(graph)
    recall_text = format_recall(slots, graph)
    written = inject_recall(recall_text, dry=dry)

    placed_total = sum(len(v) for v in slots.values())
    if written:
        print(f"✓ Recall injected: {placed_total} cells across "
              f"{sum(1 for v in slots.values() if v)} populated slots")
    elif not dry:
        print("Recall block unchanged")

    for slot_key, display_name, _, _ in SLOTS:
        cells = slots[slot_key]
        if cells:
            print(f"  [{display_name}]")
            for c in cells:
                print(f"    {c['cell_id']} ({c['significance']}, {c['temporal_status']}) "
                      f"[{c['semantic_type']}] — {c['brief'][:65]}")


if __name__ == "__main__":
    main()
