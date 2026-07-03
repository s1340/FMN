#!/usr/bin/env python3
"""
fmn.py — Forget-me-not: one entry point for the whole memory system.

A thin dispatcher over the subsystem scripts so users (and cron) run one
command instead of remembering eight filenames.

    python fmn.py analyze --session-id <id>   # session -> cells (quarantine)
    python fmn.py admit                        # auto-admit + QC + embed
    python fmn.py recall                        # write boot note into .hermes.md
    python fmn.py query "text"                  # what would Q recall?
    python fmn.py expand <cell_id>              # print full cell (brief+episode+chunk)
    python fmn.py reflect check|curate|ingest   # reflection pipeline
    python fmn.py ruminate                       # contradictions/consolidation/decay
    python fmn.py constellation detect|form|...  # consolidation layer
    python fmn.py pin|mute|link|sever ...        # curation primitives
    python fmn.py remember|annotate ...          # Q's own hands
    python fmn.py timeline show|conflicts|...     # belief timeline (bitemporal)
    python fmn.py verify                          # tamper-evidence check
    python fmn.py eval                            # retrieval regression suite
    python fmn.py panel                           # launch the control panel UI
    python fmn.py doctor                          # health check

Config: fmn.py reads vault.toml (next to it) if present; otherwise the
per-script defaults apply. See fmn_config.py.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

# command -> (script, prepend-args)
ROUTES = {
    "analyze":       ("memory_analyzer.py", []),
    "admit":         ("memory_trust.py", ["admit"]),
    "verify":        ("memory_trust.py", ["verify"]),
    "review":        ("memory_trust.py", ["review"]),
    "backfill":      ("memory_trust.py", ["backfill"]),
    "recall":        ("vault_recall.py", []),
    "query":         ("memory_graph.py", ["query"]),
    "reflect":       ("reflection_pipeline.py", []),
    "ruminate":      ("rumination.py", ["run"]),
    "constellation": ("constellation.py", []),
    "pin":           ("memory_curate.py", ["pin"]),
    "mute":          ("memory_curate.py", ["mute"]),
    "link":          ("memory_curate.py", ["link"]),
    "sever":         ("memory_curate.py", ["sever"]),
    "remember":      ("q_memory.py", ["remember"]),
    "annotate":      ("q_memory.py", ["annotate"]),
    "embed":         ("memory_embed.py", ["build"]),
    "audit":         ("memory_audit.py", []),
    "eval":          ("memory_eval.py", ["run"]),
    "sign":          ("memory_sign.py", []),
    "stats":         ("memory_trust.py", ["stats"]),
    "panel":         ("vault_viz.py", []),
    "timeline":      ("memory_timeline.py", []),
    "rollup":        ("consolidate.py", []),
}


def doctor():
    """Health check: deps, vault, integrity, eval — one green/red readout."""
    import importlib.util as u
    print("Forget-me-not · health check\n")
    for m, need in [("numpy", True), ("model2vec", False), ("flask", False),
                    ("yaml", True), ("openai", False)]:
        ok = u.find_spec(m) is not None
        tag = "OK " if ok else ("MISSING (required)" if need else "optional — absent")
        print(f"  dep {m:14s} {tag}")
    sys.path.insert(0, str(HERE))
    import memory_graph as mg
    g = mg.load_graph()
    print(f"\n  vault: {len(g['nodes'])} cells, {len(g['edges'])} edges")
    print(f"  graph: {mg.GRAPH_FILE}")
    r = subprocess.run([sys.executable, str(HERE / "memory_trust.py"), "verify"],
                       capture_output=True, text=True)
    print("  " + (r.stdout.strip().splitlines() or ["(verify produced no output)"])[0])


def expand(cell_id):
    """Print full cell content (brief + episode + chunk) by cell ID."""
    import glob
    pattern = str(HERE.parent / "**" / f"*_{cell_id}_*.md")
    # Search vault nodes directory first
    vault = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                 r"C:\Users\User\Documents\Obsidian Vault"))
    nodes = vault / "30_EPISODES" / "nodes"
    matches = list(nodes.glob(f"*_{cell_id}_*.md"))
    if not matches:
        # Fallback: search quarantine too
        q = vault / "30_EPISODES" / "quarantine"
        matches = list(q.glob(f"*_{cell_id}_*.md"))
    if not matches:
        # Last resort: broad search
        matches = list(vault.rglob(f"*_{cell_id}_*.md"))
    if not matches:
        print(f"Cell {cell_id} not found in vault.")
        return 1
    path = matches[0]
    print(path.read_text(encoding="utf-8"))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "doctor":
        doctor()
        return
    if cmd == "expand":
        if len(sys.argv) < 3:
            print("Usage: python fmn.py expand <cell_id>", file=sys.stderr)
            sys.exit(1)
        sys.exit(expand(sys.argv[2]))
    if cmd not in ROUTES:
        print(f"Unknown command: {cmd}\n\n{__doc__}", file=sys.stderr)
        sys.exit(1)
    script, prepend = ROUTES[cmd]
    args = [sys.executable, str(HERE / script)] + prepend + sys.argv[2:]
    sys.exit(subprocess.run(args).returncode)


if __name__ == "__main__":
    main()
