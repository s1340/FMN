#!/usr/bin/env python3
"""
memory_curate.py — Curation primitives for FMN: pin, mute, link, sever.

The library layer under both the control panel (Mal's hands) and Q's agency
tools (his hands). Everything returns error strings instead of exiting —
these run inside Flask and inside Q's session, never die loudly.

Concepts:
  PIN    — always surfaces at boot, regardless of score. A manual anchor.
           (referenced_count measures USE; pin expresses WORTH directly.)
  MUTE   — never surfaces at boot; still searchable on demand. For the
           mundane cell that gets touched constantly and crowds out better.
  LINK   — a manual string between two cells (strongest edge type). Humans
           and companions know arcs no embedding can see.
  SEVER  — cut a string, PERSISTENTLY: severed pairs are recorded and
           build-edges will not resurrect them. Forgetting an association
           is as legitimate as making one.

Usage:
    python memory_curate.py pin <cell_id> [--off]
    python memory_curate.py mute <cell_id> [--off]
    python memory_curate.py link <a> <b> [--note "..."]
    python memory_curate.py sever <a> <b> [--type semantic_sim]
    python memory_curate.py severed          # list the do-not-relink registry
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pair_key(a: str, b: str) -> list:
    return sorted([a, b])


def _severed_list(graph: dict) -> list:
    return graph["metadata"].setdefault("severed", [])


def is_severed(graph: dict, a: str, b: str, edge_type: str) -> bool:
    key = _pair_key(a, b)
    for s in _severed_list(graph):
        if s["pair"] == key and s["type"] in (edge_type, "*"):
            return True
    return False


# ── Pin / Mute ───────────────────────────────────────────────────────────────

def set_pin(graph: dict, cell_id: str, on: bool = True) -> str | None:
    node = graph["nodes"].get(cell_id)
    if node is None:
        return f"cell {cell_id} not in graph"
    node["pinned"] = bool(on)
    if on:
        node["muted"] = False          # pin and mute are mutually exclusive
    return None


def set_mute(graph: dict, cell_id: str, on: bool = True) -> str | None:
    node = graph["nodes"].get(cell_id)
    if node is None:
        return f"cell {cell_id} not in graph"
    node["muted"] = bool(on)
    if on:
        node["pinned"] = False
    return None


# ── Link / Sever ─────────────────────────────────────────────────────────────

def link_cells(graph: dict, a: str, b: str, note: str = "",
               by: str = "panel") -> str | None:
    if a == b:
        return "cannot link a cell to itself"
    missing = [x for x in (a, b) if x not in graph["nodes"]]
    if missing:
        return f"cell(s) not in graph: {missing}"
    # Linking un-severs: an explicit new connection overrides old scissors.
    sev = _severed_list(graph)
    key = _pair_key(a, b)
    graph["metadata"]["severed"] = [s for s in sev if s["pair"] != key]
    graph["edges"].append({
        "a": a, "b": b, "type": "manual", "weight": 2.0,
        "note": note or f"linked by {by}",
        "created": datetime.now(timezone.utc).isoformat(),
    })
    return None


def sever_edge(graph: dict, a: str, b: str, edge_type: str = "*",
               by: str = "panel") -> int:
    """Remove edge(s) between a and b and register the pair as severed so
    build-edges never resurrects it. Returns count removed."""
    key = _pair_key(a, b)
    before = len(graph["edges"])
    graph["edges"] = [
        e for e in graph["edges"]
        if not (_pair_key(e["a"], e["b"]) == key
                and (edge_type == "*" or e["type"] == edge_type))
    ]
    removed = before - len(graph["edges"])
    sev = _severed_list(graph)
    if not any(s["pair"] == key and s["type"] == edge_type for s in sev):
        sev.append({"pair": key, "type": edge_type, "by": by,
                    "at": datetime.now(timezone.utc).isoformat()})
    return removed


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="FMN curation: pin/mute/link/sever")
    ap.add_argument("command", choices=["pin", "mute", "link", "sever", "severed"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--note", default="")
    ap.add_argument("--type", dest="etype", default="*")
    args = ap.parse_args()

    if args.command in ("pin", "mute"):
        if not args.args:
            print("Usage: pin|mute <cell_id> [--off]", file=sys.stderr); sys.exit(1)
        fn = set_pin if args.command == "pin" else set_mute
        with mg.locked_graph() as graph:
            err = fn(graph, args.args[0], on=not args.off)
            if err:
                print(f"Error: {err}", file=sys.stderr); sys.exit(1)
        print(f"OK {args.command}{'ed' if not args.off else ' removed'}: {args.args[0]}")

    elif args.command == "link":
        if len(args.args) < 2:
            print("Usage: link <a> <b> [--note ...]", file=sys.stderr); sys.exit(1)
        with mg.locked_graph() as graph:
            err = link_cells(graph, args.args[0], args.args[1], note=args.note, by="cli")
            if err:
                print(f"Error: {err}", file=sys.stderr); sys.exit(1)
        print(f"OK linked: {args.args[0]} <-> {args.args[1]}")

    elif args.command == "sever":
        if len(args.args) < 2:
            print("Usage: sever <a> <b> [--type T]", file=sys.stderr); sys.exit(1)
        with mg.locked_graph() as graph:
            n = sever_edge(graph, args.args[0], args.args[1], args.etype, by="cli")
        print(f"OK severed {n} edge(s); pair registered do-not-relink ({args.etype})")

    elif args.command == "severed":
        graph = mg.load_graph()
        for s in _severed_list(graph):
            print(f"  {s['pair'][0]} x {s['pair'][1]}  [{s['type']}]  by {s.get('by')} at {s.get('at','')[:10]}")
        if not _severed_list(graph):
            print("  (none)")


if __name__ == "__main__":
    main()
