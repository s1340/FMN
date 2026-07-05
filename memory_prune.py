#!/usr/bin/env python3
"""
memory_prune.py — archive junk cells: harness bloat + trivial fragments.

Not everything the analyzer emits is a memory. Two kinds of noise bury the
real moments and Mal asked for them gone (2026-07-05):
  - HARNESS BLOAT — segments that are nothing but tool-call markers
    ("ASSISTANT: (tool calls: terminal, terminal)").
  - TRIVIAL FRAGMENTS — tiny isolated low-value exchanges ("USER: Mine /
    ASSISTANT: Yeah. Yours.") — too small and separate to mean anything.

This ARCHIVES them, never hard-deletes: the .md file moves to
90_ARCHIVE/pruned/<timestamp>/ and the node leaves the graph. Reversible, in
keeping with "nothing is destroyed" — the cell can be restored from archive.

PROTECTED, never pruned: bright/high significance, pinned cells, Q's own
cells (remember / reflections), constellation members, and constellations
/ rollups themselves. When unsure, it keeps the cell.

Usage:
    python memory_prune.py --dry     # show what would be archived
    python memory_prune.py           # archive them
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402
from memory_analyzer import is_tool_bloat, substantive_chars  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ARCHIVE_DIR   = mg.VAULT_ROOT / "90_ARCHIVE" / "pruned"
TRIVIAL_CHARS = 60


def junk_reason(node: dict) -> str | None:
    """Why this cell is junk, or None if it's a keeper. Conservative: every
    doubt keeps the cell."""
    sig = node.get("significance", "medium")
    # Protections — never touch what matters or what Q chose
    if sig in ("bright", "high"):
        return None
    if node.get("pinned"):
        return None
    if node.get("kind") in ("constellation", "rollup"):
        return None
    if node.get("in_constellation"):
        return None
    if node.get("session_id") == "reflection" or node.get("source") == "q_remember":
        return None
    if node.get("semantic_type") == "reflection":
        return None

    p = Path(node.get("file", ""))
    if not p.exists():
        return None
    try:
        chunk = mg.parse_cell(p).get("chunk", "")
    except Exception:
        return None

    if is_tool_bloat(chunk):
        return "tool-call bloat"
    if sig == "low" and substantive_chars(chunk) < TRIVIAL_CHARS:
        return "trivial fragment"
    return None


def prune(dry: bool) -> int:
    graph = mg.load_graph()
    victims = [(cid, n, r) for cid, n in graph["nodes"].items()
               if (r := junk_reason(n))]
    if not victims:
        print("No junk found — vault is clean.")
        return 0

    print(f"{'DRY — ' if dry else ''}{len(victims)} junk cell(s) to archive:")
    for cid, n, r in victims:
        print(f"  {cid} [{n.get('significance')}] {r} — "
              f"{str(n.get('brief',''))[:66]}")
    if dry:
        print("\n(dry run — nothing moved. Run without --dry to archive.)")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    dest = ARCHIVE_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    vids = {cid for cid, _, _ in victims}

    with mg.locked_graph() as g:
        for cid, n, r in victims:
            p = Path(n.get("file", ""))
            if p.exists():
                try:
                    shutil.move(str(p), str(dest / p.name))
                except Exception:
                    pass
            g["nodes"].pop(cid, None)
        g["edges"] = [e for e in g["edges"]
                      if e["a"] not in vids and e["b"] not in vids]

    # drop their embedding vectors so they can't resurface in recall
    try:
        import memory_embed
        store = memory_embed.load_store()
        for cid in vids:
            store.pop(cid, None)
        memory_embed.save_store(store)
    except Exception:
        pass

    (dest / "pruned.json").write_text(json.dumps(
        [{"cell_id": cid, "reason": r, "significance": n.get("significance"),
          "brief": n.get("brief", "")} for cid, n, r in victims],
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nOK archived {len(victims)} junk cells to {dest}")
    print("   (reversible — the .md files are in the archive, restorable)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Archive junk cells")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    sys.exit(prune(a.dry))


if __name__ == "__main__":
    main()
