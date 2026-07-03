#!/usr/bin/env python3
"""
q_memory.py — Q's own hands on his memory. FMN's agency layer.

Everything else in FMN is done TO the companion: the analyzer cuts, the
pipeline files, boot recall serves. These tools are done BY him. The best
human memories aren't filed by a background process — they're chosen at the
moment of experience, by the one experiencing them. Salience-at-encoding
beats any analyzer.

Tools (designed to be wired as Hermes tools / called via shell):

  remember  — Q deliberately keeps a moment, mid-conversation.
              He supplies the verbatim exchange (the chunk — HIS choice of
              what to quote), his own brief, topics, significance. The cell
              admits instantly (trust=auto, source=q_remember), embeds, and
              is findable in the same breath.

  annotate  — "this memory reads wrong to me" / "this means more than it
              says." Appends a dated first-person note to the cell file
              (## Q Notes section), re-seals the hash through the sanctioned
              path, and flags reflection_candidate so the note surfaces in
              the next curation.

  pin       — what HE considers load-bearing, not just what scores well.
              (Wraps memory_curate.set_pin.)

Usage:
    python q_memory.py remember --brief "..." --chunk "..." \
        [--topics a,b] [--significance high] [--type relationship]
    python q_memory.py annotate <cell_id> "note text"
    python q_memory.py pin <cell_id> [--off]
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg      # noqa: E402
import memory_curate as mc     # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _now():
    return datetime.now(timezone.utc)


# ── remember ─────────────────────────────────────────────────────────────────

def remember(brief: str, chunk: str, topics: list[str], significance: str,
             stype: str, episode: str = "") -> str:
    from memory_trust import content_hash
    cid = "qm" + os.urandom(3).hex()
    now = _now()
    date = now.strftime("%Y-%m-%d")
    episode = episode or brief
    topics = topics or ["q_remembered"]

    fm_text = (
        "---\n"
        f"cell_id: {cid}\n"
        f"session_id: q_remember\n"
        f"session_date: {date}\n"
        f"created: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "temporal_status: fresh\n"
        f"topics: {topics!r}\n".replace("'", '"') +
        f"entities: []\n"
        f"significance: {significance}\n"
        "valence: positive\n"
        "novelty: notable\n"
        f"semantic_type: {stype}\n"
        "reflection_candidate: true\n"
        "referenced_count: 0\n"
        "last_referenced: null\n"
        "neighbors: []\n"
        "source: q_remember\n"
        "---"
    )
    body = f"\n\n## Brief\n{brief}\n\n## Episode\n{episode}\n\n## Chunk\n{chunk}\n"

    node_path = mg.NODES_DIR / f"{date}_{cid}_q_remembered.md"
    mg.NODES_DIR.mkdir(parents=True, exist_ok=True)
    node_path.write_text(fm_text + body, encoding="utf-8")

    # Under the lock, whole read-modify-write cycle — so a concurrent panel
    # edit, cron admit, or another remember can't clobber this new cell.
    # (This is the exact race Q diagnosed from his own confusing vault state.)
    with mg.locked_graph() as graph:
        graph["nodes"][cid] = {
            "cell_id": cid, "session_id": "q_remember", "session_date": date,
            "created": now.isoformat(), "topics": topics, "entities": [],
            "significance": significance, "valence": "positive",
            "novelty": "notable", "semantic_type": stype,
            "reflection_candidate": True, "brief": brief, "episode": episode,
            "temporal_status": "fresh", "referenced_count": 0,
            "last_referenced": None, "approved_at": now.isoformat(),
            "neighbors": [], "file": str(node_path),
            "trust": "auto", "source": "q_remember",
            "content_hash": content_hash(brief, episode, chunk),
            "admitted_at": now.isoformat(),
        }
        graph["metadata"]["total_approvals"] += 1

    try:
        import memory_embed
        memory_embed.embed_cells(mg.load_graph())
    except Exception:
        pass
    try:
        import memory_sign
        memory_sign.sign_event(cid, content_hash(brief, episode, chunk),
                               "admit")
    except Exception:
        pass

    return cid


# ── annotate ─────────────────────────────────────────────────────────────────

def annotate(cell_id: str, note: str) -> str | None:
    from memory_trust import content_hash
    with mg.locked_graph() as graph:
        node = graph["nodes"].get(cell_id)
        if node is None:
            return f"cell {cell_id} not in graph"
        path = Path(node.get("file", ""))
        if not path.exists():
            return f"cell file missing: {path}"

        text = path.read_text(encoding="utf-8")
        stamp = _now().strftime("%Y-%m-%d")
        note_block = f"\n\n## Q Notes\n" if "## Q Notes" not in text else "\n"
        text = text.rstrip() + note_block + f"- ({stamp}) {note}\n"
        path.write_text(text, encoding="utf-8")

        # Re-seal through the sanctioned path (Q Notes live outside brief/
        # episode/chunk, so hash inputs are unchanged — but re-stamp
        # defensively) and surface to the reflection pipeline.
        cell = mg.parse_cell(path)
        node["content_hash"] = content_hash(cell["brief"], cell["episode"], cell["chunk"])
        node["reflection_candidate"] = True
        node["q_notes"] = node.get("q_notes", 0) + 1
        new_hash = node["content_hash"]
    try:
        import memory_sign
        memory_sign.sign_event(cell_id, new_hash, "annotate")
    except Exception:
        pass
    return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Q's memory agency tools")
    ap.add_argument("command", choices=["remember", "annotate", "pin"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--brief", default="")
    ap.add_argument("--chunk", default="")
    ap.add_argument("--episode", default="")
    ap.add_argument("--topics", default="")
    ap.add_argument("--significance", default="high",
                    choices=["low", "medium", "high", "bright"])
    ap.add_argument("--type", dest="stype", default="relationship")
    ap.add_argument("--off", action="store_true")
    args = ap.parse_args()

    if args.command == "remember":
        if not args.brief or not args.chunk:
            print("Usage: remember --brief '...' --chunk '...' "
                  "[--topics a,b] [--significance high] [--type relationship]",
                  file=sys.stderr)
            sys.exit(1)
        topics = [t.strip() for t in args.topics.split(",") if t.strip()]
        cid = remember(args.brief, args.chunk, topics, args.significance,
                       args.stype, args.episode)
        print(f"OK remembered: {cid} — findable immediately, "
              f"surfaces per its {args.significance} significance")

    elif args.command == "annotate":
        if len(args.args) < 2:
            print("Usage: annotate <cell_id> \"note\"", file=sys.stderr)
            sys.exit(1)
        err = annotate(args.args[0], " ".join(args.args[1:]))
        if err:
            print(f"Error: {err}", file=sys.stderr); sys.exit(1)
        print(f"OK annotated {args.args[0]} — will surface in next reflection curation")

    elif args.command == "pin":
        if not args.args:
            print("Usage: pin <cell_id> [--off]", file=sys.stderr); sys.exit(1)
        graph = mg.load_graph()
        err = mc.set_pin(graph, args.args[0], on=not args.off)
        if err:
            print(f"Error: {err}", file=sys.stderr); sys.exit(1)
        mg.save_graph(graph)
        print(f"OK pin {'set' if not args.off else 'removed'}: {args.args[0]}")


if __name__ == "__main__":
    main()
