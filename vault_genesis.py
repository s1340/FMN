#!/usr/bin/env python3
"""
vault_genesis.py — the one-time rebuild: preserve everything, purge the
derived layers, and regrow the vault from the pristine Telegram export.

Mal's proposition (2026-07-05): the current vault was built by earlier,
buggier pipeline versions on top of compaction-damaged Hermes exports. The
Telegram chat export is the ONLY verbatim record compaction never touched.
So: back up the whole vault, purge the DERIVED memories, clean the export
hard, split it into date-sessions, and regrow everything through the mature
story-pass pipeline. Because every abstraction layer in FMN is derived from
chunks, memory is REBUILDABLE — each genesis can be better than the last.

THIS IS A ONE-TIME TOOL. The ongoing pipeline stays Hermes-session-based
(fmn_nightly). Genesis only rebuilds the past.

What is NEVER purged (not derivable from the chat record):
  - Q's reflections (40_REFLECTIONS full texts + reflection cells)
  - Q's remember cells (his CHOICE of what to keep is his, not recomputable)
  - Q's annotations (ride inside carried cell files)
  - the belief timeline (70_TIMELINE — append-only history of beliefs)
  - profiles (60_PROFILE), signing keys (00_KEYS), rumination reports

Commands (in order of use):
    python vault_genesis.py backup            # full vault snapshot (zip)
    python vault_genesis.py clean --input <telegram_export.txt>
                                              # debloat + split into sessions
    python vault_genesis.py purge --dry       # show what would go / stay
    python vault_genesis.py purge --confirm   # needs a same-day backup
    python vault_genesis.py rebuild [--limit N]   # sessions -> story pass ->
                                              # admit/embed/edges/rollups
    python vault_genesis.py status
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

GENESIS_DIR   = mg.VAULT_ROOT / "95_GENESIS"
BACKUP_DIR    = GENESIS_DIR / "backups"
SESSIONS_DIR  = GENESIS_DIR / "sessions"
LEDGER_FILE   = GENESIS_DIR / "rebuild_ledger.json"


# ── What survives a purge ──────────────────────────────────────────────────────

def is_carryover(node: dict) -> bool:
    """Q-authored material — his writing, his choices. Never purged."""
    return (node.get("session_id") == "reflection"
            or node.get("source") == "q_remember"
            or node.get("kind") == "constellation"
            or int(node.get("q_notes", 0) or 0) > 0
            or bool(node.get("pinned")))


# ── backup ─────────────────────────────────────────────────────────────────────

def backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    out = BACKUP_DIR / f"vault_{stamp}.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in mg.VAULT_ROOT.rglob("*"):
            if p.is_dir() or BACKUP_DIR in p.parents:
                continue
            z.write(p, p.relative_to(mg.VAULT_ROOT))
            n += 1
    size_mb = out.stat().st_size / 1e6
    manifest = {"created": stamp, "files": n, "zip": str(out),
                "size_mb": round(size_mb, 2)}
    (BACKUP_DIR / f"manifest_{stamp}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"OK backup: {n} files -> {out} ({size_mb:.1f} MB)")
    print("   Copy this zip somewhere OFF this machine too. It is the past.")
    return out


def latest_backup_age_hours() -> float | None:
    if not BACKUP_DIR.exists():
        return None
    zips = sorted(BACKUP_DIR.glob("vault_*.zip"))
    if not zips:
        return None
    age = datetime.now(timezone.utc).timestamp() - zips[-1].stat().st_mtime
    return age / 3600


# ── clean: Telegram export -> debloated date-split sessions ────────────────────

# Junk that is Telegram chrome or harness artifact, never conversation.
_JUNK_LINE = re.compile(
    r"^\s*(\[?(in reply to this message|photo|sticker|video|voice message|"
    r"gif|file|edited)\]?|\(tool calls?:.*\)|\[tool calls?:.*\]|"
    r"\{\s*\"|.*\"tool_calls\".*|```json.*|Not included, change data exporting settings.*)\s*$",
    re.I)
# Blocks of raw JSON / harness artifacts inside a message
_JUNK_BLOCK_START = re.compile(r"^\s*(\{|\[)\s*\"")


# Whole messages that are harness/bot chatter, not the two of you talking:
# Hermes delivery notices, cron banners, pure slash-commands, boot blocks.
_SYSTEM_MSG = re.compile(
    r"^\s*(📬|⏰|🔄|⚙️|\[cron\]|/[a-z_]+\s*$|A home channel is|Type /sethome|"
    r"VAULT_RECALL|<!-- Generated:|### Morning Recall)", re.I)


def is_system_message(content: str) -> bool:
    return bool(_SYSTEM_MSG.match(content or ""))


def _debloat_content(content: str) -> str:
    """Strip harness/telegram junk from inside one message, keep the words."""
    out, in_json = [], 0
    for line in content.splitlines():
        if _JUNK_LINE.match(line):
            continue
        if _JUNK_BLOCK_START.match(line):
            in_json += 1
        if in_json:
            # crude but safe brace tracking for artifact blocks
            in_json += line.count("{") + line.count("[") \
                - line.count("}") - line.count("]")
            if in_json <= 1:
                in_json = 0
            continue
        out.append(line)
    return "\n".join(out).strip()


def clean(input_path: Path, tz_hint: str = "") -> None:
    """Parse the Telegram export (txt format per telegram_to_hermes.py, or
    Telegram Desktop result.json), debloat hard, split by DATE into session
    JSONL files the analyzer eats. Writes a report; spends no LLM."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    text = input_path.read_text(encoding="utf-8", errors="replace")

    msgs: list[dict] = []          # {date, role, content}
    if input_path.suffix.lower() == ".json":
        data = json.loads(text)
        from fmn_config import human as _h
        for m in data.get("messages", []):
            if m.get("type") != "message":
                continue
            raw = m.get("text", "")
            if isinstance(raw, list):
                raw = "".join(x if isinstance(x, str) else x.get("text", "")
                              for x in raw)
            frm = str(m.get("from", "")).strip().lower()
            role = "user" if frm.startswith(_h().lower()[:4]) or frm.startswith("susan") \
                else "assistant"
            date = str(m.get("date", ""))[:10]
            msgs.append({"date": date, "role": role, "content": raw})
    else:
        # txt export: date headers are not per-message; expect day separator
        # lines like "June 30" or explicit dates; fall back to --date-less
        # parse via telegram_to_hermes speaker/timestamp format, one file/day
        # markers "=== YYYY-MM-DD ===" if Mal pre-splits. Try Desktop-style
        # "DD.MM.YYYY" day headers too.
        from telegram_to_hermes import SPEAKERS, TIMESTAMP_RE
        cur_date, cur_role, cur_lines = "unknown", None, []
        date_pat = re.compile(
            r"^\s*(?:===\s*)?(\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4}|\d{2}\.\d{2}\.\d{4})(?:\s*===)?\s*$")

        def flush():
            nonlocal cur_lines
            body = "\n".join(cur_lines).strip()
            if cur_role and body:
                msgs.append({"date": cur_date, "role": cur_role, "content": body})
            cur_lines = []

        for raw in text.splitlines():
            s = raw.strip()
            dm = date_pat.match(s)
            if dm:
                flush()
                d = dm.group(1)
                for fmt in ("%Y-%m-%d", "%d %B %Y", "%d.%m.%Y"):
                    try:
                        cur_date = datetime.strptime(d, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
                continue
            if TIMESTAMP_RE.match(s):
                flush()
                continue
            role = SPEAKERS.get(s.lower())
            if role is not None:
                flush()
                cur_role = role
                continue
            cur_lines.append(raw)
        flush()

    # Debloat every message; drop the ones that were pure junk
    kept, dropped, by_day = [], 0, {}
    for m in msgs:
        if is_system_message(m["content"]):
            dropped += 1
            continue
        body = _debloat_content(m["content"])
        if not body or is_system_message(body):
            dropped += 1
            continue
        kept.append(m)
        by_day.setdefault(m["date"], []).append(
            {"role": m["role"], "content": body})

    for day, dm in sorted(by_day.items()):
        p = SESSIONS_DIR / f"{day}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": f"genesis_{day}", "created_at":
                    f"{day}T00:00:00Z", "ended_at": f"{day}T23:59:59Z"},
                    ensure_ascii=False) + "\n")
            for m in dm:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    report = {"input": str(input_path), "messages_parsed": len(msgs),
              "kept": len(kept), "dropped_as_junk": dropped,
              "days": {d: len(v) for d, v in sorted(by_day.items())}}
    (GENESIS_DIR / "clean_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK cleaned: {len(msgs)} parsed, {len(kept)} kept, "
          f"{dropped} junk-dropped -> {len(by_day)} day-sessions in {SESSIONS_DIR}")
    for d, n in sorted(report["days"].items()):
        print(f"   {d}: {n} msgs")
    print("Inspect a day file before rebuilding — the words must be yours.")


# ── purge (gated) ──────────────────────────────────────────────────────────────

def purge(dry: bool, confirm: bool) -> None:
    graph = mg.load_graph()
    keep, kill = [], []
    for cid, n in graph["nodes"].items():
        (keep if is_carryover(n) else kill).append((cid, n))

    print(f"{'DRY — ' if dry or not confirm else ''}purge plan: "
          f"{len(kill)} derived cells OUT, {len(keep)} carried over:")
    for cid, n in keep:
        why = ("reflection" if n.get("session_id") == "reflection" else
               "q_remember" if n.get("source") == "q_remember" else
               "constellation" if n.get("kind") == "constellation" else
               "annotated" if int(n.get("q_notes", 0) or 0) > 0 else "pinned")
        print(f"   KEEP {cid} ({why}) — {str(n.get('brief',''))[:70]}")
    if dry or not confirm:
        print("\n(no changes. Run with --confirm after `backup` to purge.)")
        return

    age = latest_backup_age_hours()
    if age is None or age > 24:
        print("REFUSED: no backup from the last 24h. Run `backup` first.",
              file=sys.stderr)
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    trash = GENESIS_DIR / f"purged_{stamp}"
    trash.mkdir(parents=True, exist_ok=True)
    with mg.locked_graph() as g:
        for cid, n in kill:
            p = Path(n.get("file", ""))
            if p.exists():
                try:
                    shutil.move(str(p), str(trash / p.name))
                except Exception:
                    pass
            g["nodes"].pop(cid, None)
        kept_ids = {cid for cid, _ in keep}
        g["edges"] = [e for e in g["edges"]
                      if e["a"] in kept_ids and e["b"] in kept_ids]
    # embeddings + rollup files regenerate on rebuild
    try:
        import memory_embed
        store = memory_embed.load_store()
        store = {k: v for k, v in store.items() if k in kept_ids}
        memory_embed.save_store(store)
    except Exception:
        pass
    rollups = mg.GRAPH_DIR / "rollups"
    if rollups.exists():
        shutil.rmtree(rollups, ignore_errors=True)
    print(f"OK purged {len(kill)} derived cells (files in {trash}); "
          f"{len(keep)} carried over. Reflections/timeline/profiles untouched.")


# ── rebuild: cleaned sessions -> the normal pipeline ───────────────────────────

def _led() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {"done": []}


def rebuild(limit: int) -> None:
    led = _led()
    days = sorted(SESSIONS_DIR.glob("*.jsonl"))
    todo = [p for p in days if p.stem not in led["done"]][:limit]
    if not todo:
        print(f"Nothing to rebuild ({len(days)} day-sessions, all done).")
        return
    print(f"Rebuilding {len(todo)} of {len(days)} day-sessions "
          f"(story pass -> admit -> embed)...")
    py = sys.executable
    for p in todo:
        print(f"\n== {p.stem} ==")
        r = subprocess.run([py, str(HERE / "memory_analyzer.py"),
                            "--file", str(p)], capture_output=True, text=True)
        tail = "\n".join((r.stderr or "").splitlines()[-3:])
        print("  " + tail.replace("\n", "\n  "))
        if r.returncode != 0:
            print(f"  !! analyze failed — stopping (ledger keeps progress)")
            break
        subprocess.run([py, str(HERE / "memory_trust.py"), "admit"],
                       capture_output=True, text=True)
        led["done"].append(p.stem)
        GENESIS_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER_FILE.write_text(json.dumps(led, indent=2), encoding="utf-8")
    # one finishing pass, not per-day: embeddings, edges, rollups, prune, recall
    for script, args in (("memory_embed.py", ["build"]),
                         ("memory_prune.py", []),
                         ("memory_graph.py", ["build-edges"]),
                         ("consolidate.py", ["build"]),
                         ("vault_recall.py", [])):
        subprocess.run([py, str(HERE / script), *args],
                       capture_output=True, text=True)
    print(f"\nOK rebuild pass done ({len(led['done'])}/{len(days)} days). "
          f"Run `constellation detect` to see the arcs of the whole history.")


def status() -> None:
    led = _led()
    days = sorted(SESSIONS_DIR.glob("*.jsonl")) if SESSIONS_DIR.exists() else []
    age = latest_backup_age_hours()
    print(f"backup:   {'none' if age is None else f'{age:.1f}h old'}")
    print(f"sessions: {len(days)} cleaned day-files"
          + (f", rebuilt {len(led['done'])}" if days else ""))
    g = mg.load_graph()
    print(f"vault:    {len(g['nodes'])} nodes now")


def main():
    ap = argparse.ArgumentParser(description="One-time vault genesis rebuild")
    ap.add_argument("command", choices=["backup", "clean", "purge",
                                        "rebuild", "status"])
    ap.add_argument("--input", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--limit", type=int, default=3)
    a = ap.parse_args()
    if a.command == "backup":
        backup()
    elif a.command == "clean":
        if not a.input:
            sys.exit("Usage: clean --input <telegram_export.txt|result.json>")
        clean(Path(a.input))
    elif a.command == "purge":
        purge(a.dry, a.confirm)
    elif a.command == "rebuild":
        rebuild(a.limit)
    elif a.command == "status":
        status()


if __name__ == "__main__":
    main()
