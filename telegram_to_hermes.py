#!/usr/bin/env python3
"""
telegram_to_hermes.py — Convert Telegram text export to Hermes-compatible JSON

The Telegram text export format is:
  [Speaker name]
  [In reply to this message]   ← optional UI chrome, discarded
  [content lines...]
  HH:MM                        ← timestamp closes the message

Consecutive messages from the same speaker do NOT repeat the speaker name.
The timestamp belongs to the message immediately above it.

Output: single JSON object matching what memory_analyzer.py's load_session()
expects: {"id":..., "timestamp":..., "messages": [{role, content, timestamp}...]}

Usage:
    python telegram_to_hermes.py | python memory_analyzer.py
    python telegram_to_hermes.py --output session.json
    python telegram_to_hermes.py --date 2026-06-29   # if multi-day, use first day
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Map exact speaker names (lowercased) to roles
SPEAKERS = {
    "susan malvin": "user",
    "quint":        "assistant",
}

TIMESTAMP_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse(text: str, date: str) -> list[dict]:
    messages = []
    current_role  = None
    current_lines = []
    current_ts    = None

    def flush():
        nonlocal current_lines
        if current_role is None or current_ts is None:
            return
        content = "\n".join(current_lines).strip()
        if not content:
            current_lines = []
            return
        messages.append({
            "role":      current_role,
            "content":   content,
            "timestamp": current_ts,
        })
        current_lines = []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        # HH:MM timestamp → assign to current message and flush
        m = TIMESTAMP_RE.match(stripped)
        if m:
            h, mn = int(m.group(1)), int(m.group(2))
            dt = datetime.strptime(f"{date} {h:02d}:{mn:02d}", "%Y-%m-%d %H:%M")
            current_ts = dt.replace(tzinfo=timezone.utc).timestamp()
            flush()
            continue

        # Speaker name → switch role (flush happens on next timestamp)
        role = SPEAKERS.get(stripped.lower())
        if role is not None:
            current_role  = role
            current_lines = []
            continue

        # Telegram reply-quote UI marker → skip
        if stripped == "In reply to this message":
            continue

        # Everything else is message content
        current_lines.append(line)

    # Last message may have no trailing timestamp (file ended mid-block)
    if current_lines and current_role and current_ts:
        content = "\n".join(current_lines).strip()
        if content:
            messages.append({
                "role":      current_role,
                "content":   content,
                "timestamp": current_ts,
            })

    return messages


def main():
    parser = argparse.ArgumentParser(description="Telegram export → Hermes JSON")
    parser.add_argument(
        "--input", default=r"G:\LLM\test\EXPORTED_CHAT_TELEGRAM.txt",
        help="Path to Telegram text export")
    parser.add_argument(
        "--output", default="-",
        help="Output file path (default: stdout)")
    parser.add_argument(
        "--date", default="2026-06-30",
        help="Date of the session (YYYY-MM-DD); used to build full timestamps")
    parser.add_argument(
        "--session-id", default="telegram-2026-06-30",
        help="Session ID to embed in the output")
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    messages = parse(text, args.date)

    user_count = sum(1 for m in messages if m["role"] == "user")
    asst_count = sum(1 for m in messages if m["role"] == "assistant")
    print(f"Parsed {len(messages)} messages  "
          f"(user: {user_count}  assistant: {asst_count})",
          file=sys.stderr)

    session = {
        "id":        args.session_id,
        "timestamp": args.date,
        "source":    "telegram",
        "messages":  messages,
    }

    out = json.dumps(session, ensure_ascii=False, indent=2)
    if args.output == "-":
        sys.stdout.write(out + "\n")
    else:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
        print(f"Written → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
