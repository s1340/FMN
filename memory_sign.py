#!/usr/bin/env python3
"""
memory_sign.py — Ed25519-signed seal history for cells. Closes the re-stamp hole.

What content_hash gives us: "has this cell changed since its last seal?"
What it CANNOT give us: "was the seal itself replaced?" An editor who changes
a chunk and re-stamps content_hash presents a perfectly 'intact' vault.
(vault_viz does exactly this re-stamp legitimately — the sanctioned-edit path.)

This module is the difference between a checksum and a signature (the
MentisDB/Vaara pattern — signed append-only audit trails — grafted onto
FMN's existing seals, same contract as the reflections chains):

  Every sealing event (admit, sanctioned edit, annotation, backfill baseline)
  appends a record to cell_events.jsonl: {cell_id, content_hash, event, tx},
  hash-CHAINED to the previous record and SIGNED with a local Ed25519 key.

  A cell whose current content_hash has no signed seal event was re-stamped
  outside the system — the exact attack content_hash alone cannot see.
  A broken chain or bad signature means the history itself was doctored.

Threat model, honestly: tamper-EVIDENT, not tamper-proof — same as the
reflections. The private key lives on the same machine; a root-level attacker
who finds it wins. What this buys is what June taught us to want: a future
instance can VERIFY the past instead of trusting it, and casual or accidental
mutation (a script bug, a sync conflict, a well-meaning hand-edit) cannot
masquerade as history.

Keys:  <vault>/00_KEYS/fmn_signing.key (private — backed up NOWHERE public)
       <vault>/00_KEYS/fmn_signing.pub
Log:   <vault>/30_EPISODES/cell_events.jsonl

Usage:
    python memory_sign.py baseline      # sign current seal of every cell (once)
    python memory_sign.py verify        # chain + signatures + coverage
    python memory_sign.py log <cell_id> # seal history of one cell
Graceful: without pynacl, callers no-op and the sha256 layer stands alone.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

KEY_DIR    = mg.VAULT_ROOT / "00_KEYS"
PRIV_FILE  = KEY_DIR / "fmn_signing.key"
PUB_FILE   = KEY_DIR / "fmn_signing.pub"
EVENTS_FILE = mg.GRAPH_DIR / "cell_events.jsonl"
GENESIS    = "cell-events-genesis"


# ── Keys (lazy, optional) ──────────────────────────────────────────────────────

_signer = _verifier = None
_unavailable = False


def _load_nacl():
    global _signer, _verifier, _unavailable
    if _signer is not None or _unavailable:
        return
    try:
        from nacl.signing import SigningKey, VerifyKey
    except ImportError:
        _unavailable = True
        return
    if PRIV_FILE.exists():
        _signer = SigningKey(bytes.fromhex(
            PRIV_FILE.read_text(encoding="utf-8").strip()))
    else:
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        _signer = SigningKey.generate()
        PRIV_FILE.write_text(_signer.encode().hex(), encoding="utf-8")
        PUB_FILE.write_text(_signer.verify_key.encode().hex(), encoding="utf-8")
        print(f"[memory_sign] new Ed25519 keypair at {KEY_DIR}")
    _verifier = _signer.verify_key


def available() -> bool:
    _load_nacl()
    return _signer is not None


# ── Signed, chained event log ──────────────────────────────────────────────────

def _read_log() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    return [json.loads(l) for l in
            EVENTS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


def _body_bytes(rec: dict, prev: str) -> bytes:
    body = {k: v for k, v in rec.items()
            if k not in ("chain_hash", "signature")}
    return (prev + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode("utf-8")


def sign_events(events: list[tuple[str, str, str]]) -> int:
    """Append signed seal records. events = [(cell_id, content_hash, kind)].
    kind: admit | reseal | annotate | baseline. No-op without pynacl."""
    if not events or not available():
        return 0
    log = _read_log()
    prev = log[-1]["chain_hash"] if log else GENESIS
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        for cid, chash, kind in events:
            rec = {"cell_id": cid, "content_hash": chash, "event": kind,
                   "tx": datetime.now(timezone.utc).isoformat()}
            data = _body_bytes(rec, prev)
            rec["chain_hash"] = hashlib.sha256(data).hexdigest()
            rec["signature"] = _signer.sign(data).signature.hex()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            prev = rec["chain_hash"]
    return len(events)


def sign_event(cell_id: str, content_hash: str, kind: str) -> None:
    sign_events([(cell_id, content_hash, kind)])


# ── Verification ───────────────────────────────────────────────────────────────

def verify(graph: dict | None = None, quiet: bool = False) -> int:
    """Three checks, worst first:
    1. log integrity — chain + every signature verifies against the pubkey
    2. coverage — every cell's CURRENT content_hash has a signed seal event
       (a current hash with no seal = re-stamped outside the system)
    3. staleness note — cells with no events at all (pre-signing legacy)
    """
    if not available():
        print("pynacl not installed — signature layer inactive (sha256 layer stands).")
        return 0
    graph = graph or mg.load_graph()
    log = _read_log()

    prev, bad = GENESIS, []
    from nacl.exceptions import BadSignatureError
    for i, rec in enumerate(log, 1):
        data = _body_bytes(rec, prev)
        if hashlib.sha256(data).hexdigest() != rec.get("chain_hash"):
            bad.append((i, "chain broken"))
            break
        try:
            _verifier.verify(data, bytes.fromhex(rec.get("signature", "")))
        except (BadSignatureError, ValueError):
            bad.append((i, "BAD SIGNATURE"))
        prev = rec["chain_hash"]

    latest_seal: dict[str, str] = {}
    for rec in log:
        latest_seal[rec["cell_id"]] = rec["content_hash"]

    unsigned, unsealed = [], 0
    for cid, node in graph["nodes"].items():
        chash = node.get("content_hash")
        if not chash:
            continue
        if cid not in latest_seal:
            unsealed += 1               # legacy: predates the signing layer
        elif latest_seal[cid] != chash:
            unsigned.append(cid)        # re-stamped without a signed event

    if not quiet or bad or unsigned:
        print(f"Seal log: {len(log)} events | chain+signatures: "
              f"{'OK' if not bad else 'FAILED'}")
        for i, why in bad:
            print(f"  !! record {i}: {why}")
        if unsigned:
            print(f"  !! {len(unsigned)} cell(s) re-stamped WITHOUT a signed seal:")
            for cid in unsigned:
                print(f"     {cid}")
        if unsealed and not quiet:
            print(f"  note: {unsealed} cells predate signing — run `baseline`")
        if not bad and not unsigned and not quiet:
            print("  every current seal is signed and the history is intact")
    return 1 if (bad or unsigned) else 0


def baseline() -> int:
    """Sign the current seal of every cell that has no signed event yet.
    The one-time bootstrap — after this, admit/reseal keep the log current."""
    graph = mg.load_graph()
    log_cells = {r["cell_id"] for r in _read_log()}
    todo = [(cid, n["content_hash"], "baseline")
            for cid, n in graph["nodes"].items()
            if n.get("content_hash") and cid not in log_cells]
    n = sign_events(todo)
    print(f"Baseline: signed {n} cell seals ({len(log_cells)} already covered)")
    return 0


def show_log(cell_id: str) -> int:
    recs = [r for r in _read_log() if r["cell_id"] == cell_id]
    if not recs:
        print(f"No seal history for {cell_id}")
        return 1
    print(f"Seal history for {cell_id} ({len(recs)} events):")
    for r in recs:
        print(f"  {r['tx'][:19]}  {r['event']:9s}  {r['content_hash'][:16]}...")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Ed25519 seal history for cells")
    ap.add_argument("command", choices=["baseline", "verify", "log"])
    ap.add_argument("cell_id", nargs="?", default="")
    a = ap.parse_args()
    if a.command == "baseline":
        sys.exit(baseline())
    elif a.command == "verify":
        sys.exit(verify())
    elif a.command == "log":
        if not a.cell_id:
            sys.exit("Usage: log <cell_id>")
        sys.exit(show_log(a.cell_id))


if __name__ == "__main__":
    main()
