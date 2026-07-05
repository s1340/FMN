#!/usr/bin/env python3
"""
memory_trust.py — Split ADMISSION from ACTIVATION. End the manual-review treadmill.

The design error in the original vault was fusing two gates into one: a cell
had to be human-approved to EXIST. But you don't approve your own memories —
everything encodes (gray), attention decides what stays warm, and most of it
is reconstructable-but-never-recalled. This module implements that:

  ADMISSION   — automatic. Every cell enters the graph at birth as trust=auto
                ("gray"). Safe because the verbatim CHUNK rides along: a wrong
                brief over a correct chunk is a mislabeled folder, not a false
                memory. The anti-confabulation rail already says expand-before-
                acting. Session DB keeps everything regardless.

  QC          — mechanical, not Mal. Confidence checks at admit time. Only
                FAILURES go to quarantine for human eyes (2-3/week, not 200).

  VERIFICATION— by use, not review. A cell earns trust by being retrieved and
                NOT causing a stumble (see memory_graph.touch_cell). Trust flows
                from the relationship's actual traffic.

  ACTIVATION  — small and warm. vault_recall keeps ~15 cells hot; the rest is
                gray, reachable on demand. Nothing is destroyed; what mattered
                stays close.

FULLY AUTONOMOUS since 2026-07-02 (Mal's call): no human in the admission
loop at all — bright cells auto-approve too. Anchor influence is earned via
referenced_count ranking, false brights drift out of rotation by disuse,
corrections happen in conversation (living), never in a review queue (admin).
The `review` command still exists but is optional attention, not a duty.

Trust tiers (stored on the node as `trust`):
  auto     — admitted automatically, not yet used. Gray.
  checked  — surfaced in recall and did not cause a correction. Earned.
  flagged  — failed a confidence check OR was corrected in conversation.
  human    — explicitly confirmed by Mal (legacy tier; optional).

Content hashing: every admitted node stores content_hash (sha256 of the cell's
brief+episode+chunk). `verify` re-hashes and reports drift — Q's memories become
tamper-evident the way his reflections already are. We learned in June what
external verification is worth.

Usage:
    python memory_trust.py admit                 # batch-admit all quarantine cells
    python memory_trust.py admit --dry           # show what would admit / flag
    python memory_trust.py verify                # re-hash nodes, report drift
    python memory_trust.py review                # show cells needing human eyes
    python memory_trust.py stats                 # trust-tier breakdown

Environment:
    MEMORY_VAULT_ROOT   vault override
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402


# ── Content hash ────────────────────────────────────────────────────────────

def content_hash(brief: str, episode: str, chunk: str) -> str:
    h = hashlib.sha256()
    for part in (brief, episode, chunk):
        h.update((part or "").strip().encode("utf-8"))
        h.update(b"\x00")            # field separator, prevents concatenation collisions
    return h.hexdigest()


def cell_content_hash(cell_data: dict) -> str:
    return content_hash(cell_data.get("brief", ""),
                        cell_data.get("episode", ""),
                        cell_data.get("chunk", ""))


# ── Confidence checks (mechanical QC — replaces human gatekeeping) ───────────

def _tokens(text: str) -> set[str]:
    return {w.strip(".,!?;:'\"()").lower()
            for w in (text or "").split() if len(w) > 3}


def confidence_checks(cell_data: dict) -> list[str]:
    """Return a list of failure reasons. Empty list = passes, admit as auto.

    These are the checks Q himself proposed: does the brief describe its own
    chunk, is the cell well-formed, is significance plausible. Conservative —
    only clear failures flag, because a flag costs Mal's attention.
    """
    fm    = cell_data.get("frontmatter", {})
    brief = cell_data.get("brief", "")
    chunk = cell_data.get("chunk", "")
    fails = []

    if len(brief.strip()) < 10:
        fails.append("brief empty/trivial")
    if "[parse error" in brief or "summarization failed" in cell_data.get("episode", ""):
        fails.append("analyzer parse-error sentinel in cell")
    if len(chunk.strip()) < 20:
        fails.append("chunk missing/trivial — cannot ground the brief")

    # Brief-vs-chunk grounding: the brief should share vocabulary with the text
    # it claims to summarize. Near-zero overlap = brief describes something else
    # (the exact hallucination the two-phase analyzer was built to prevent —
    # this catches the residual).
    bt, ct = _tokens(brief), _tokens(chunk)
    if bt and ct:
        overlap = len(bt & ct) / len(bt)
        if overlap < 0.15:
            fails.append(f"brief/chunk overlap {overlap:.0%} — brief may not match chunk")

    sig = fm.get("significance", "medium")
    if sig not in ("low", "medium", "high", "bright"):
        fails.append(f"significance '{sig}' not in schema")

    return fails


# ── Admission ───────────────────────────────────────────────────────────────

def initial_trust(cell_data: dict, fails: list[str]) -> str:
    # 2026-07-02, Mal's call: bright cells auto-approve like everything else —
    # human fully out of the admission loop; the system self-corrects.
    # Anchor influence is still EARNED, not granted: boot-slot ranking weights
    # referenced_count, so a false bright that never gets used drifts out of
    # rotation naturally, and a corrected one demotes to flagged via touch_cell.
    # Correction happens in conversation (living), not in a review queue (admin).
    return "flagged" if fails else "auto"


def latest_quarantine_runs(vault: Path) -> list[Path]:
    qdir = vault / "90_ARCHIVE" / "session_cells_quarantine"
    if not qdir.exists():
        return []
    return sorted([d for d in qdir.iterdir() if d.is_dir()])


def admit(dry: bool) -> int:
    runs = latest_quarantine_runs(mg.VAULT_ROOT)
    if not runs:
        print("No quarantine runs to admit.")
        return 0

    admitted = {"auto": 0, "bright_pending": 0}
    flagged, skipped = [], 0

    # Phase 1 (NO lock): the slow work — parse quarantine, run QC, write node
    # .md files — building a list of node dicts to merge. Reading files can be
    # slow; do it outside the critical section.
    existing = set(mg.load_graph()["nodes"])
    to_add: list[dict] = []
    for run in runs:
        for f in sorted(run.glob("*.md")):
            if f.name.startswith("merge_proposals"):
                continue
            try:
                cell = mg.parse_cell(f)
            except Exception as e:
                flagged.append((f.name, [f"unparseable: {e}"]))
                continue
            cid = cell["frontmatter"].get("cell_id")
            if not cid:
                flagged.append((f.name, ["no cell_id"]))
                continue
            if cid in existing:
                skipped += 1
                continue

            fails = confidence_checks(cell)
            trust = initial_trust(cell, fails)
            if fails:
                flagged.append((cid, fails))
            else:
                admitted[trust] = admitted.get(trust, 0) + 1

            if dry:
                tag = trust if not fails else f"FLAGGED ({'; '.join(fails)})"
                print(f"  {cid}  {cell['frontmatter'].get('significance','?'):7s}  {tag}")
                continue

            node_path = mg.NODES_DIR / f.name
            mg.NODES_DIR.mkdir(parents=True, exist_ok=True)
            node_path.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            fm = cell["frontmatter"]
            to_add.append({
                "cell_id": cid,
                "session_id": fm.get("session_id"),
                "session_date": str(fm.get("session_date", "")),
                "created": fm.get("created", datetime.now(timezone.utc).isoformat()),
                "topics": fm.get("topics", []),
                "entities": fm.get("entities", []),
                "significance": fm.get("significance", "medium"),
                "valence": fm.get("valence", "neutral"),
                "novelty": fm.get("novelty", "routine"),
                "semantic_type": fm.get("semantic_type", "work_research"),
                "arc": str(fm.get("arc", "") or ""),
                "arc_role": str(fm.get("arc_role", "") or ""),
                "reflection_candidate": bool(fm.get("reflection_candidate", False)),
                "brief": cell["brief"],
                "episode": cell["episode"],
                "temporal_status": "fresh",
                "referenced_count": 0,
                "last_referenced": None,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "neighbors": fm.get("neighbors", []),
                "file": str(node_path),
                "trust": trust,
                "content_hash": cell_content_hash(cell),
                "admitted_at": datetime.now(timezone.utc).isoformat(),
            })

    # Phase 2 (UNDER lock): brief critical section — re-load fresh (a concurrent
    # remember may have landed), merge, save. Never clobbers another writer.
    if not dry and to_add:
        merged = []
        with mg.locked_graph() as graph:
            for node in to_add:
                if node["cell_id"] in graph["nodes"]:
                    continue
                graph["nodes"][node["cell_id"]] = node
                graph["metadata"]["total_approvals"] += 1
                merged.append(node)
        # Ed25519 seal-event log (memory_sign): each admission's seal is
        # signed and chained, so a later re-stamp can't masquerade as history
        try:
            import memory_sign
            memory_sign.sign_events(
                [(n["cell_id"], n["content_hash"], "admit") for n in merged])
        except Exception:
            pass

    # Embed newly admitted cells (FMN semantic layer; no-op if unavailable)
    if not dry:
        try:
            import memory_embed
            n_emb = memory_embed.embed_cells(mg.load_graph())
            if n_emb:
                print(f"Embedded {n_emb} new cells")
        except Exception:
            pass

    print(f"\n{'DRY RUN — ' if dry else ''}Admitted: "
          f"{admitted.get('auto',0)} auto, {admitted.get('bright_pending',0)} bright_pending  |  "
          f"Flagged for review: {len(flagged)}  |  Already in graph: {skipped}")
    if flagged:
        print("\nNeeds human eyes (flagged):")
        for cid, reasons in flagged:
            print(f"  {cid}: {'; '.join(reasons)}")
    return 0


# ── Integrity verification ───────────────────────────────────────────────────

def verify() -> int:
    graph = mg.load_graph()
    ok, drift, missing_hash, missing_file = 0, [], 0, []
    for cid, node in graph["nodes"].items():
        stored = node.get("content_hash")
        if not stored:
            missing_hash += 1
            continue
        path = Path(node.get("file", ""))
        if not path.exists():
            missing_file.append(cid)
            continue
        cell = mg.parse_cell(path)
        current = cell_content_hash(cell)
        if current == stored:
            ok += 1
        else:
            drift.append(cid)

    print(f"Integrity: {ok} intact | {len(drift)} DRIFTED | "
          f"{missing_hash} pre-hash (legacy) | {len(missing_file)} file-missing")
    if drift:
        print("\n⚠ DRIFT — cell content changed since admission (edited outside the system):")
        for cid in drift:
            print(f"  {cid}")
    if missing_file:
        print("\n⚠ FILE MISSING for admitted nodes:")
        for cid in missing_file:
            print(f"  {cid}")
    if missing_hash:
        print(f"\nNote: {missing_hash} legacy nodes predate hashing — run `backfill` to hash them.")
    # Signature layer: catches the attack this hash check can't — content
    # edited AND hash re-stamped. Quiet unless it finds something.
    sig_bad = 0
    try:
        import memory_sign
        sig_bad = memory_sign.verify(graph, quiet=True)
    except Exception:
        pass
    return 1 if (drift or missing_file or sig_bad) else 0


def backfill() -> int:
    """Stamp content_hash + default trust onto legacy nodes that lack them."""
    graph = mg.load_graph()
    n_hash, n_trust = 0, 0
    for node in graph["nodes"].values():
        if not node.get("trust"):
            # legacy approved cells were human-vetted at the old gate → 'human'
            node["trust"] = "human"
            n_trust += 1
        if not node.get("content_hash"):
            path = Path(node.get("file", ""))
            if path.exists():
                node["content_hash"] = cell_content_hash(mg.parse_cell(path))
                n_hash += 1
    mg.save_graph(graph)
    print(f"Backfilled: {n_trust} trust tiers, {n_hash} content hashes")
    try:
        import memory_sign
        if memory_sign.available():
            memory_sign.baseline()
    except Exception:
        pass
    return 0


# ── Review queue + stats ─────────────────────────────────────────────────────

def review() -> int:
    graph = mg.load_graph()
    flagged = [(c, n) for c, n in graph["nodes"].items() if n.get("trust") == "flagged"]

    print(f"── Flagged cells (QC failure or corrected in conversation): {len(flagged)} ──\n")
    if flagged:
        for cid, n in flagged:
            print(f"  {cid}  {n.get('session_date')}  {n.get('brief','')[:70]}")
        print("\nOptional attention — the system runs without you clearing these;")
        print("flagged cells never surface at boot until corrected or re-admitted.")
    else:
        print("Nothing flagged. The traffic is doing the verification.")
    return 0


def migrate_bright_pending() -> int:
    """One-time: bright_pending tier retired (2026-07-02) — brights are auto."""
    graph = mg.load_graph()
    n = 0
    for node in graph["nodes"].values():
        if node.get("trust") == "bright_pending":
            node["trust"] = "auto"
            n += 1
    mg.save_graph(graph)
    print(f"Migrated {n} bright_pending -> auto")
    return 0


def stats() -> int:
    """The trust_profile (consolidation-memory's idea, FMN's axes): one
    readout answering 'how grounded is this vault right now?' — coverage,
    contradiction pressure, drift posture. Automation stays inspectable."""
    graph = mg.load_graph()
    nodes = graph["nodes"]
    tiers = {}
    for n in nodes.values():
        tiers[n.get("trust", "untiered")] = tiers.get(n.get("trust", "untiered"), 0) + 1
    print(f"Nodes: {len(nodes)}")
    for t in ("human", "checked", "auto", "bright_pending", "flagged", "untiered"):
        if t in tiers:
            print(f"  {t:15s} {tiers[t]}")

    n_total = len(nodes) or 1
    hashed = sum(1 for n in nodes.values() if n.get("content_hash"))
    provenanced = sum(1 for n in nodes.values()
                      if n.get("session_id") and Path(str(n.get("file", ""))).exists())
    signed = 0
    try:
        import memory_sign
        if memory_sign.available():
            latest = {}
            for rec in memory_sign._read_log():
                latest[rec["cell_id"]] = rec["content_hash"]
            signed = sum(1 for cid, n in nodes.items()
                         if n.get("content_hash")
                         and latest.get(str(cid)) == n["content_hash"])
    except Exception:
        pass
    print(f"\nCoverage:")
    print(f"  sealed (sha256)   {hashed}/{len(nodes)} ({hashed/n_total:.0%})")
    print(f"  signed (ed25519)  {signed}/{len(nodes)} ({signed/n_total:.0%})")
    print(f"  provenance        {provenanced}/{len(nodes)} ({provenanced/n_total:.0%})"
          f"  (session + file on disk)")

    open_c = evo = 0
    try:
        import memory_timeline
        state = memory_timeline.replay()
        open_c = len(memory_timeline.open_conflicts(state))
        evo = sum(1 for f in state["facts"].values()
                  if f["retired"] and f["retired"].get("successor"))
    except Exception:
        pass
    n_sup = sum(1 for n in nodes.values() if n.get("timeline_superseded"))
    n_conf = sum(1 for n in nodes.values() if n.get("in_conflict"))
    print(f"\nContradiction pressure:")
    print(f"  open conflicts    {open_c}"
          + ("   <- held from boot until resolved" if open_c else ""))
    print(f"  beliefs evolved   {evo} (retired with successor)")
    print(f"  drift markers     {n_sup} superseded, {n_conf} in-conflict")
    if open_c >= 3 or (n_total > 50 and open_c / n_total > 0.02):
        print("  !! pressure high — run rumination sooner than the weekly floor")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Trust-tiered admission + integrity")
    ap.add_argument("command", choices=["admit", "verify", "backfill", "review", "stats", "migrate"])
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.command == "admit":
        sys.exit(admit(dry=args.dry))
    elif args.command == "verify":
        sys.exit(verify())
    elif args.command == "backfill":
        sys.exit(backfill())
    elif args.command == "review":
        sys.exit(review())
    elif args.command == "stats":
        sys.exit(stats())
    elif args.command == "migrate":
        sys.exit(migrate_bright_pending())


if __name__ == "__main__":
    main()
