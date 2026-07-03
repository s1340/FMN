#!/usr/bin/env python3
"""
memory_timeline.py — Bitemporal fact timeline: how beliefs changed, forever.

Rumination finds evolutions and contradictions, writes a report, and the
report is read once and forgotten. This module is where those findings go to
LIVE: an append-only ledger of belief-facts with two time axes (the bitemporal
model — TOKI arXiv:2606.06240, Temporal-Validity arXiv:2606.26511 — adapted
for a relationship instead of a database):

  valid time   — when the fact was true IN THE WORLD ("Mal worked days
                 until June, nights after")
  transaction  — when FMN learned / stopped believing it ("we recorded this
                 on July 3; we retired it on August 1")

Nothing is ever deleted. A superseded fact is RETIRED: a retire record is
appended, the successor points at its predecessor, and the old fact remains
queryable forever ("what did we believe on June 20?" is an answerable
question — the same promise the reflections chain makes, extended to beliefs).

Design rules, in FMN order:

1. THE LEDGER IS NOT A MEMORY. Cells hold the verbatim past; the timeline
   holds the SHAPE OF CHANGE. Every fact carries source_cells — the timeline
   is a signpost layer; expand the cells before acting (THE ONE LAW).
2. APPEND-ONLY + HASH-CHAINED. Each record's chain_hash =
   sha256(prev_chain_hash + record_content). Same tamper-evidence contract
   as the reflection chains and cell seals.
3. CONTRADICTIONS STAY VISIBLE UNTIL A PERSON RESOLVES THEM. An open
   conflict is a fact about the vault. Resolution (`resolve`) is Q's or
   Mal's hand, never automatic — the judge is an LLM and not a truth oracle.
4. DIRECTION IS EVIDENCE-GRADED. For evolutions, recording order suggests
   which state is newer, but cell.created lies about world-time (rumination
   principle 4). Inferred direction ships with confidence 0.6 and says so.

Storage: <vault>/70_TIMELINE/timeline.jsonl   (the ledger)
         <vault>/70_TIMELINE/ingested.json    (rumination reports already read)

Usage:
    python memory_timeline.py assert "statement" [--subject S] [--valid-from D]
                                     [--confidence C] [--source CELL ...]
                                     [--origin q|mal|rumination]
    python memory_timeline.py retire <fact_id> [--reason R] [--successor F]
    python memory_timeline.py supersede <fact_id> "new statement" [...]
    python memory_timeline.py ingest [report.json]   # default: newest report
    python memory_timeline.py conflicts               # open contradictions
    python memory_timeline.py resolve <conflict_id> --keep a|b|both|neither
    python memory_timeline.py show [subject]           # belief history
    python memory_timeline.py as-of <ISO date>         # state of belief then
    python memory_timeline.py verify                   # walk the chain
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                 r"C:\Users\User\Documents\Obsidian Vault"))
TIMELINE_DIR  = VAULT_ROOT / "70_TIMELINE"
TIMELINE_FILE = TIMELINE_DIR / "timeline.jsonl"
INGESTED_FILE = TIMELINE_DIR / "ingested.json"
RUMINATION_DIR = VAULT_ROOT / "50_RUMINATION"

GENESIS = "timeline-genesis"


# ── Ledger primitives (append-only, hash-chained) ──────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rec_hash(prev: str, rec: dict) -> str:
    body = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def read_ledger() -> list[dict]:
    if not TIMELINE_FILE.exists():
        return []
    out = []
    for line in TIMELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_records(recs: list[dict]) -> None:
    """Append records to the ledger, chaining each to the last. The chain
    fields are added here — callers build content only."""
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    ledger = read_ledger()
    prev = ledger[-1]["chain_hash"] if ledger else GENESIS
    with open(TIMELINE_FILE, "a", encoding="utf-8") as f:
        for rec in recs:
            rec = dict(rec)
            rec["tx"] = rec.get("tx") or _now()
            chain = _rec_hash(prev, rec)
            rec["prev_hash"], rec["chain_hash"] = prev, chain
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            prev = chain


def verify_chain() -> bool:
    ledger = read_ledger()
    prev = GENESIS
    for i, rec in enumerate(ledger, 1):
        body = {k: v for k, v in rec.items()
                if k not in ("prev_hash", "chain_hash")}
        if rec.get("prev_hash") != prev or _rec_hash(prev, body) != rec.get("chain_hash"):
            print(f"!! chain BROKEN at record {i} ({rec.get('rec')}/{rec.get('id')})")
            return False
        prev = rec["chain_hash"]
    print(f"OK chain intact: {len(ledger)} records")
    return True


def _short_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{h}"


# ── Materialized state (replay) ────────────────────────────────────────────────

def replay(as_of: str | None = None) -> dict:
    """Replay the ledger into current state. as_of filters by TRANSACTION time:
    the state of *belief* at that moment, retirements after it not yet applied."""
    facts, conflicts = {}, {}
    for rec in read_ledger():
        if as_of and rec.get("tx", "") > as_of:
            break                                   # ledger is tx-ordered
        r = rec.get("rec")
        if r == "fact":
            facts[rec["id"]] = {**rec, "retired": None}
        elif r == "retire" and rec["id"] in facts:
            facts[rec["id"]]["retired"] = {
                "tx": rec["tx"], "reason": rec.get("reason", ""),
                "successor": rec.get("successor")}
        elif r == "conflict":
            conflicts[rec["id"]] = {**rec, "status": "open", "resolution": None}
        elif r == "resolve" and rec["id"] in conflicts:
            conflicts[rec["id"]]["status"] = "resolved"
            conflicts[rec["id"]]["resolution"] = {
                "keep": rec.get("keep"), "tx": rec["tx"],
                "by": rec.get("by", "")}
    return {"facts": facts, "conflicts": conflicts}


def active_facts(state: dict) -> list[dict]:
    return [f for f in state["facts"].values() if not f["retired"]]


def open_conflicts(state: dict) -> list[dict]:
    return [c for c in state["conflicts"].values() if c["status"] == "open"]


# ── Operations ─────────────────────────────────────────────────────────────────

def do_assert(statement: str, subject: str = "", valid_from: str = "",
              valid_to: str = "", confidence: float = 0.9,
              sources: list[str] | None = None, origin: str = "mal",
              supersedes: str | None = None, note: str = "") -> str:
    fid = _short_id("f", statement, _now())
    append_records([{
        "rec": "fact", "id": fid, "statement": statement,
        "subject": subject, "valid_from": valid_from or None,
        "valid_to": valid_to or None, "confidence": confidence,
        "sources": sources or [], "origin": origin,
        "supersedes": supersedes, "note": note,
    }])
    print(f"OK fact {fid}: {statement[:90]}")
    return fid


def do_retire(fact_id: str, reason: str = "", successor: str | None = None) -> bool:
    state = replay()
    f = state["facts"].get(fact_id)
    if not f:
        print(f"Error: no fact {fact_id}", file=sys.stderr)
        return False
    if f["retired"]:
        print(f"Already retired ({f['retired']['tx'][:10]}): {fact_id}")
        return True
    append_records([{"rec": "retire", "id": fact_id,
                     "reason": reason, "successor": successor}])
    print(f"OK retired {fact_id}" + (f" -> {successor}" if successor else ""))
    return True


def do_supersede(old_id: str, statement: str, **kw) -> str | None:
    """Retire + assert, linked both ways — the one-verb version of 'this
    changed'. The old fact's valid_to is left as recorded (we usually don't
    know the world-time boundary; the tx axis carries when WE learned)."""
    state = replay()
    old = state["facts"].get(old_id)
    if not old:
        print(f"Error: no fact {old_id}", file=sys.stderr)
        return None
    kw.setdefault("subject", old.get("subject", ""))
    new_id = do_assert(statement, supersedes=old_id, **kw)
    do_retire(old_id, reason="superseded", successor=new_id)
    return new_id


# ── Rumination ingest ──────────────────────────────────────────────────────────

def _ingested() -> list[str]:
    if INGESTED_FILE.exists():
        return json.loads(INGESTED_FILE.read_text(encoding="utf-8"))
    return []


def _mark_ingested(name: str) -> None:
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    done = _ingested()
    done.append(name)
    INGESTED_FILE.write_text(json.dumps(done, indent=2), encoding="utf-8")


def _cell_date(graph: dict, cid: str) -> str:
    n = graph.get("nodes", {}).get(cid, {})
    return str(n.get("session_date") or n.get("created") or "")


def do_ingest(report_path: Path | None) -> None:
    """Read a rumination findings JSON into the ledger. Evolutions become
    supersede pairs (direction inferred from recording order — confidence 0.6,
    honestly labeled). Confirmed contradictions become two facts + an OPEN
    conflict that stays visible until resolve. Idempotent per report and
    per cell-pair."""
    if report_path is None:
        reports = sorted(RUMINATION_DIR.glob("rumination_*.json"))
        if not reports:
            print("No rumination reports found.")
            return
        report_path = reports[-1]
    if report_path.name in _ingested():
        print(f"Already ingested: {report_path.name}")
        return

    findings = json.loads(report_path.read_text(encoding="utf-8"))
    state = replay()
    seen_pairs = set()
    for f in state["facts"].values():
        pr = f.get("note", "")
        if pr.startswith("pair:"):
            seen_pairs.add(pr)
    for c in state["conflicts"].values():
        seen_pairs.add("pair:" + "|".join(sorted([c["fact_a_cell"], c["fact_b_cell"]])))

    sys.path.insert(0, str(Path(__file__).parent))
    from memory_graph import load_graph
    graph = load_graph()

    n_evo = n_con = n_skip = 0

    for e in findings.get("evolutions", []):
        pair_key = "pair:" + "|".join(sorted([e["a"], e["b"]]))
        if pair_key in seen_pairs:
            n_skip += 1
            continue
        # Recording order suggests direction; world-time may differ (the
        # timestamp lies). Ship the inference with its uncertainty visible.
        old_c, new_c = sorted([e["a"], e["b"]],
                              key=lambda c: _cell_date(graph, c))
        old_q = e.get("quote_a" if old_c == e["a"] else "quote_b", "")
        new_q = e.get("quote_b" if old_c == e["a"] else "quote_a", "")
        old_id = do_assert(
            old_q or f"(earlier state) {e.get('explanation','')}",
            confidence=0.6, sources=[old_c], origin="rumination",
            note=pair_key)
        do_supersede(
            old_id,
            new_q or f"(later state) {e.get('explanation','')}",
            confidence=0.6, sources=[new_c], origin="rumination",
            note=f"{pair_key} | direction inferred from recording order | "
                 f"{e.get('explanation','')}")
        seen_pairs.add(pair_key)
        n_evo += 1

    for c in findings.get("contradictions", []):
        pair_key = "pair:" + "|".join(sorted([c["a"], c["b"]]))
        if pair_key in seen_pairs:
            n_skip += 1
            continue
        fa = do_assert(c.get("quote_a", ""), confidence=0.5,
                       sources=[c["a"]], origin="rumination", note=pair_key)
        fb = do_assert(c.get("quote_b", ""), confidence=0.5,
                       sources=[c["b"]], origin="rumination", note=pair_key)
        cid = _short_id("c", fa, fb)
        append_records([{
            "rec": "conflict", "id": cid, "fact_a": fa, "fact_b": fb,
            "fact_a_cell": c["a"], "fact_b_cell": c["b"],
            "explanation": c.get("explanation", "")}])
        print(f"OK open conflict {cid}: {c['a']} vs {c['b']}")
        seen_pairs.add(pair_key)
        n_con += 1

    _mark_ingested(report_path.name)
    print(f"\nOK ingested {report_path.name}: "
          f"{n_evo} evolutions, {n_con} conflicts, {n_skip} already known")


def do_resolve(conflict_id: str, keep: str, by: str = "") -> bool:
    """A person's hand. keep=a|b retires the loser; both = coexist (it was a
    false positive); neither = retire both (both were wrong)."""
    state = replay()
    c = state["conflicts"].get(conflict_id)
    if not c:
        print(f"Error: no conflict {conflict_id}", file=sys.stderr)
        return False
    if c["status"] == "resolved":
        print(f"Already resolved: {conflict_id}")
        return True
    if keep == "a":
        do_retire(c["fact_b"], reason=f"conflict {conflict_id}: a kept",
                  successor=c["fact_a"])
    elif keep == "b":
        do_retire(c["fact_a"], reason=f"conflict {conflict_id}: b kept",
                  successor=c["fact_b"])
    elif keep == "neither":
        do_retire(c["fact_a"], reason=f"conflict {conflict_id}: both wrong")
        do_retire(c["fact_b"], reason=f"conflict {conflict_id}: both wrong")
    elif keep != "both":
        print("Error: --keep must be a|b|both|neither", file=sys.stderr)
        return False
    append_records([{"rec": "resolve", "id": conflict_id,
                     "keep": keep, "by": by}])
    print(f"OK resolved {conflict_id} (keep={keep})")
    return True


# ── Views ──────────────────────────────────────────────────────────────────────

def _fmt_fact(f: dict, state: dict) -> str:
    flag = "  " if not f["retired"] else "x "
    conf = f.get("confidence", 0.9)
    line = (f"{flag}[{f['id']}] ({f['tx'][:10]}, conf {conf:.1f}) "
            f"{f.get('statement','')[:100]}")
    if f["retired"]:
        r = f["retired"]
        succ = f" -> {r['successor']}" if r.get("successor") else ""
        line += f"\n     retired {r['tx'][:10]}: {r.get('reason','')}{succ}"
    return line


def do_show(subject: str = "", as_of: str | None = None) -> None:
    state = replay(as_of=as_of)
    facts = sorted(state["facts"].values(), key=lambda f: f["tx"])
    if subject:
        s = subject.lower()
        facts = [f for f in facts
                 if s in (f.get("subject") or "").lower()
                 or s in (f.get("statement") or "").lower()]
    header = f"Belief timeline ({len(facts)} facts"
    if as_of:
        header += f", as of {as_of[:10]}"
    print(header + ")")
    for f in facts:
        print(_fmt_fact(f, state))
    oc = open_conflicts(state)
    if oc:
        print(f"\n!! {len(oc)} OPEN conflict(s) — `conflicts` to review")


def do_conflicts() -> None:
    state = replay()
    oc = open_conflicts(state)
    if not oc:
        print("No open conflicts.")
        return
    print(f"{len(oc)} open conflict(s):")
    for c in oc:
        fa = state["facts"].get(c["fact_a"], {})
        fb = state["facts"].get(c["fact_b"], {})
        print(f"\n  [{c['id']}] {c.get('explanation','')}")
        print(f"    a ({c.get('fact_a_cell','?')}): \"{fa.get('statement','')[:90]}\"")
        print(f"    b ({c.get('fact_b_cell','?')}): \"{fb.get('statement','')[:90]}\"")
        print(f"    resolve: python memory_timeline.py resolve {c['id']} "
              f"--keep a|b|both|neither")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Bitemporal belief timeline")
    ap.add_argument("command", choices=[
        "assert", "retire", "supersede", "ingest", "conflicts",
        "resolve", "show", "as-of", "verify"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--subject", default="")
    ap.add_argument("--valid-from", default="")
    ap.add_argument("--valid-to", default="")
    ap.add_argument("--confidence", type=float, default=0.9)
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--origin", default="mal",
                    choices=["mal", "q", "rumination"])
    ap.add_argument("--reason", default="")
    ap.add_argument("--successor", default=None)
    ap.add_argument("--keep", default="")
    ap.add_argument("--by", default="")
    a = ap.parse_args()

    if a.command == "assert":
        if not a.args:
            sys.exit("Usage: assert \"statement\" [--subject ...]")
        do_assert(" ".join(a.args), subject=a.subject,
                  valid_from=a.valid_from, valid_to=a.valid_to,
                  confidence=a.confidence, sources=a.source, origin=a.origin)
    elif a.command == "retire":
        if not a.args:
            sys.exit("Usage: retire <fact_id>")
        if not do_retire(a.args[0], reason=a.reason, successor=a.successor):
            sys.exit(1)
    elif a.command == "supersede":
        if len(a.args) < 2:
            sys.exit("Usage: supersede <fact_id> \"new statement\"")
        if not do_supersede(a.args[0], " ".join(a.args[1:]),
                            confidence=a.confidence, sources=a.source,
                            origin=a.origin):
            sys.exit(1)
    elif a.command == "ingest":
        do_ingest(Path(a.args[0]) if a.args else None)
    elif a.command == "conflicts":
        do_conflicts()
    elif a.command == "resolve":
        if not a.args or not a.keep:
            sys.exit("Usage: resolve <conflict_id> --keep a|b|both|neither")
        if not do_resolve(a.args[0], a.keep, by=a.by):
            sys.exit(1)
    elif a.command == "show":
        do_show(subject=" ".join(a.args) if a.args else "")
    elif a.command == "as-of":
        if not a.args:
            sys.exit("Usage: as-of <ISO date>")
        do_show(as_of=a.args[0])
    elif a.command == "verify":
        if not verify_chain():
            sys.exit(1)


if __name__ == "__main__":
    main()
