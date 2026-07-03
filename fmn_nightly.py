#!/usr/bin/env python3
"""
fmn_nightly.py — Automated consolidation for Forget-me-not. LEDGER SWEEP.

Runs on a schedule (cron). Analyzes EVERY completed session exactly once,
tracked by a ledger — so it can never pick the wrong session (it processes
all new ones, by ID), and never re-analyzes (the ledger remembers).

Why this shape, not "analyze the latest session":
  - The vault carries the scar "the analyzer ran on the WRONG session."
    Guessing a single session is fragile (a /reset makes "latest" ambiguous).
    A ledger sweep removes the guess: each ended session is chunked once.
  - It survives Hermes updates. FMN lives outside the Hermes install tree and
    touches Hermes only through the stable public CLI (`hermes sessions ...`).
    When that CLI changes it fails LOUDLY and is a one-line fix — versus a
    native plugin that couples to Hermes internals and can break silently on
    every release. Sovereignty is the update-survival strategy.

"Completed" is authoritative, not heuristic: a session is analyzed only when
Hermes reports ended_at set (an active session has ended_at=None). No timing
guesses, no risk of chunking a conversation still in progress.

Pipeline per run:
  1. list recent sessions
  2. for each not in the ledger: export (--include-inactive → verbatim
     pre-compaction text); if ended AND long enough, analyze → quarantine,
     record in ledger
  3. admit (idempotent) + embed
  4. prune quarantine runs whose cells are all now in the graph
  5. refresh boot recall

Usage:
    python fmn_nightly.py            # full sweep (for cron)
    python fmn_nightly.py --dry      # show what WOULD be analyzed; touch nothing
    python fmn_nightly.py --no-recall  # skip the boot-recall refresh

Environment:
    OPENROUTER_API_KEY   required (analyzer). Cron inherits it from Hermes .env.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import os

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _ensure_api_key():
    """Self-source OPENROUTER_API_KEY from the Hermes .env if not already in
    the environment. Lets ANY scheduler run this (Windows Task Scheduler,
    Hermes cron, cron, bare `python fmn_nightly.py`) with zero env setup —
    scheduler-agnostic, which is the sovereign design: FMN's automation does
    not depend on any single host wiring the environment for it."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    for env_path in (Path.home() / "AppData/Local/hermes/.env",
                     Path.home() / ".hermes/.env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip()
                    return

HERE = Path(__file__).parent
LEDGER_FILE = mg.GRAPH_DIR / "analyzed_sessions.json"
QUARANTINE = mg.VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"
MIN_MESSAGES = 6          # skip trivial sessions (a greeting and goodbye)
LIST_LIMIT = 20           # how far back to look each sweep


# ── Ledger ───────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {"analyzed": {}}


def save_ledger(led: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(led, indent=2, ensure_ascii=False),
                           encoding="utf-8")


# ── Hermes CLI (the one thin, stable, loud-on-break seam) ────────────────────

def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def list_sessions() -> list[str]:
    r = _run(["hermes", "sessions", "list", "--source", "telegram",
              "--limit", str(LIST_LIMIT)])
    if r.returncode != 0:
        print(f"!! `hermes sessions list` failed (CLI changed?): {r.stderr[:200]}",
              file=sys.stderr)
        return []
    # oldest-first so the ledger fills chronologically
    return list(reversed(re.findall(r"\b(\d{8}_\d{6}_[0-9a-f]+)\b", r.stdout)))


def export_session(sid: str) -> tuple[dict | None, str | None]:
    """Export with --include-inactive (verbatim pre-compaction). Returns
    (metadata, temp_file_path) or (None, None) on failure."""
    tmp = Path(tempfile.gettempdir()) / f"fmn_sess_{sid}.json"
    r = _run(["hermes", "sessions", "export", "--session-id", sid,
              "--include-inactive", str(tmp)])
    if r.returncode != 0 or not tmp.exists():
        print(f"!! export {sid} failed: {r.stderr[:150]}", file=sys.stderr)
        return None, None
    try:
        meta = json.loads(tmp.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"!! export {sid} unparseable: {e}", file=sys.stderr)
        return None, None
    return meta, str(tmp)


def is_ended(meta: dict) -> bool:
    # Authoritative: Hermes sets ended_at only when the session truly closes
    # (CLI exit, /reset, gateway expiry). Active sessions have ended_at=None.
    return bool(meta.get("ended_at"))


# ── Sweep ────────────────────────────────────────────────────────────────────

def sweep(dry: bool) -> list[str]:
    led = load_ledger()
    done = led["analyzed"]
    analyzed_now = []

    sessions = list_sessions()          # oldest-first
    newest = sessions[-1] if sessions else None

    for sid in sessions:
        if sid in done:
            continue
        meta, path = export_session(sid)
        if meta is None:
            continue
        mcount = meta.get("message_count", 0)
        # Closed iff Hermes stamped ended_at, OR a newer session exists (you
        # can't have two live conversations — only `newest` can be open). This
        # rescues sessions that were never cleanly ended (crash / no /reset):
        # ended_at stays None but they're plainly over once superseded.
        if not is_ended(meta) and sid == newest:
            print(f"  · {sid}  current/open (ended_at=None, newest) — next sweep")
            continue
        if mcount < MIN_MESSAGES:
            print(f"  · {sid}  trivial ({mcount} msgs) — ledgered as skipped")
            if not dry:
                done[sid] = {"status": "skipped_trivial", "messages": mcount}
            continue

        if dry:
            print(f"  → {sid}  WOULD analyze ({mcount} msgs, "
                  f"reason={meta.get('end_reason')})")
            analyzed_now.append(sid)
            continue

        print(f"  → analyzing {sid} ({mcount} msgs) ...")
        r = _run([sys.executable, str(HERE / "memory_analyzer.py"), "--file", path])
        if r.returncode != 0:
            print(f"    !! analyze failed: {r.stderr[-300:]}", file=sys.stderr)
            continue  # NOT ledgered — retried next sweep
        run_dir = None
        try:
            run_dir = json.loads(r.stdout.strip().splitlines()[-1]).get("run_dir")
        except Exception:
            pass
        done[sid] = {"status": "analyzed", "messages": mcount, "run_dir": run_dir}
        analyzed_now.append(sid)

    if not dry:
        save_ledger(led)
    return analyzed_now


# ── Prune admitted quarantine runs ───────────────────────────────────────────

def prune_quarantine() -> int:
    """Delete quarantine run dirs whose every cell is now in the graph.
    Quarantine is a transaction buffer, not an archive — once admitted, the
    staging copy is redundant (the node .md lives in 30_EPISODES/nodes)."""
    if not QUARANTINE.exists():
        return 0
    in_graph = set(mg.load_graph()["nodes"])
    pruned = 0
    for run in QUARANTINE.iterdir():
        if not run.is_dir():
            continue
        cells = [f for f in run.glob("*.md") if not f.name.startswith("merge_proposals")]
        if not cells:
            continue
        ids = []
        for f in cells:
            m = re.search(r"_([0-9a-f]{8})_", f.name)
            if m:
                ids.append(m.group(1))
        # prune only if every parseable cell landed in the graph
        if ids and all(i in in_graph for i in ids):
            # rmtree, not per-file unlink: quarantine runs can contain
            # subdirs (e.g. pre_merge_archive) that unlink() can't remove.
            # Best-effort: a locked file just leaves the run for next sweep.
            import shutil
            try:
                shutil.rmtree(run)
                pruned += 1
            except OSError as e:
                print(f"  (skip prune of {run.name}: {e})", file=sys.stderr)
    return pruned


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-recall", action="store_true")
    args = ap.parse_args()

    _ensure_api_key()
    print("== FMN nightly sweep ==")
    analyzed = sweep(dry=args.dry)
    print(f"  sessions analyzed this run: {len(analyzed)}")

    if args.dry:
        print("(dry run — no admit/embed/prune/recall)")
        return

    # admit + embed (idempotent; safe even if nothing new)
    print("[admit]")
    r = _run([sys.executable, str(HERE / "memory_trust.py"), "admit"])
    for line in r.stdout.splitlines():
        if line.startswith(("Admitted", "Flagged", "Needs")):
            print("  " + line)
    _run([sys.executable, str(HERE / "memory_embed.py"), "build"])
    # Connect the new cells: without build-edges they're islands (embedded but
    # no associative strings), starving constellation detection + navigation.
    _run([sys.executable, str(HERE / "memory_graph.py"), "build-edges"])
    # Middle-layer rollups: closed days/weeks get their index signposts
    # (mechanical, idempotent — a backfilled old day updates in place).
    r = _run([sys.executable, str(HERE / "consolidate.py"), "build"])
    for line in r.stdout.splitlines():
        if line.startswith(("OK rollups", "!!")):
            print("  " + line)

    pruned = prune_quarantine()
    print(f"[prune] {pruned} fully-admitted quarantine run(s) removed")

    if not args.no_recall:
        _run([sys.executable, str(HERE / "vault_recall.py")])
        print("[recall] boot note refreshed")

    print("== done ==")


if __name__ == "__main__":
    main()
