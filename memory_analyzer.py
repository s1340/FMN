#!/usr/bin/env python3
"""
memory_analyzer.py — Session → Memory Cells (v2)

Two-phase approach:
  Phase 1: LLM identifies cell boundaries (indices, topics, entities only)
  Phase 2: Per-cell LLM call summarizes actual verbatim chunk text

This eliminates the hallucination problem where summaries describe content
from a different part of the session than the assigned chunk indices.

Usage:
    hermes sessions export --session-id <id> | python memory_analyzer.py
    python memory_analyzer.py --file session.jsonl
    python memory_analyzer.py --session-id <id>        # calls hermes export internally

Environment:
    OPENROUTER_API_KEY   required
    MEMORY_VAULT_ROOT    override vault path (default: C:\\Users\\User\\Documents\\Obsidian Vault)
    MEMORY_CHUNKER_MODEL override chunker model (default: meta-llama/llama-3.3-70b-instruct)

Output:
    <vault>/90_ARCHIVE/session_cells_quarantine/<date>_<cell_id>_<topic>.md
    Summary JSON to stdout.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import openai

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT     = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                      r"C:\Users\User\Documents\Obsidian Vault"))
QUARANTINE     = VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CHUNKER_MODEL  = os.environ.get("MEMORY_CHUNKER_MODEL",
                                 "meta-llama/llama-3.3-70b-instruct")   # Phase 1
SUMMARY_MODEL  = os.environ.get("MEMORY_SUMMARY_MODEL",
                                 "google/gemini-2.5-flash")             # Phase 2
MAX_TRANSCRIPT_CHARS = 400_000  # ~100k tokens; covers most sessions on 128k-ctx models
MAX_CHUNK_CHARS      = 8_000    # per-cell summary input ceiling

# ── Phase 1 prompt: boundary identification only ───────────────────────────────

BOUNDARY_SYSTEM = """\
You are a session boundary identifier for a companion AI memory system.

You receive a conversation transcript between Mal (human) and Q (AI companion).
Identify the natural topic/experience boundaries. Each boundary marks one coherent arc:
a topic shift, an emotional moment, a discovery, a joke arc, a project switch.

SPLIT BIAS: when unsure, split. Small bright moments (a joke that landed, a realization,
a surprising exchange, a comedy incident) deserve their own cell even if short.
For a session with 100+ turns, expect 8–20 boundaries. Do NOT compress everything into
5 wide blocks — that loses the moments that matter most.

COVERAGE REQUIREMENT: boundaries must span the ENTIRE transcript.
The last boundary's end_idx must equal the index of the very last message.
Do not stop early. The tail of the session matters as much as the opening.

For each boundary provide only:
  start_idx / end_idx  — inclusive indices into the messages array
  topics               — 1–4 short keyword tags
  entities             — people, projects, concepts named in this segment

Boundaries must be contiguous and non-overlapping.
start_idx[0] = 0. end_idx[last] = (total messages - 1).

Return ONLY valid JSON. No markdown fences. No prose. Start your response with {

{
  "boundaries": [
    {
      "start_idx": 0,
      "end_idx": 12,
      "topics": ["greeting", "memory_problem"],
      "entities": ["Mal", "Q", "Hermes"]
    }
  ]
}"""

# ── Phase 2 prompt: per-cell summarization from actual text ────────────────────

SUMMARY_SYSTEM = """\
You are a memory cell writer for a companion AI system.

You receive a verbatim conversation excerpt between Mal (human) and Q (AI companion).
Write a memory cell summary grounded ONLY in what this excerpt actually contains.
Do not reference events outside this excerpt.

AGENT PRESERVATION — the most important rule, read it twice:
Preserve WHO DID WHAT TO WHOM exactly as the excerpt states it. Do NOT invert
agent and patient. Before writing each clause, find the sentence in the excerpt
that supports it and check the direction of the action.
  - If Q says "YOU were holding my diary shut," then MAL held Q's diary shut —
    NOT the reverse. "my" = Q's; "you" = Mal.
  - Who spoke a line is who is responsible for it. If Q makes the joke, do not
    write that Mal made the joke.
  - Track first/second person carefully: "I" and "my" refer to the SPEAKER of
    that line; "you" and "your" refer to the other party. Resolve every pronoun
    to a named person (Mal or Q) before summarizing, then state the named person.
Role inversion is the failure mode this system most needs you to avoid: it
produces a memory that means the OPPOSITE of what happened, and it passes every
surface check because all the right names are present. Get the direction right.

GROUNDING: every claim in the brief and episode must trace to a specific line
in the excerpt. If a detail (a number, a reason, a "because", a prior event) is
not in the excerpt, do not include it. Do not add plausible-sounding context.

Output fields:
  brief        — 1–2 sentences, the collapsed view (what Q sees scanning memory)
  episode      — 1 paragraph, enough context to decide whether to expand
  significance — "low" | "medium" | "high" | "bright"
                 bright = moment that should stay in active rotation:
                 a joke that landed, a touching exchange, a first, a revelation,
                 something distinctly theirs
  valence      — "positive" | "negative" | "mixed" | "neutral"
  novelty      — "routine" | "notable" | "first-time"
  semantic_type — the PRIMARY nature of this excerpt (pick one):
                  "relationship"      — how Q and Mal relate: emotional exchange, care, humor, relational texture
                  "work_research"     — experiments, projects, technical work, building things together
                  "personal_mal"      — Mal's life, feelings, or context outside the Q relationship
                  "personal_q"        — Q's own nature, self-observed patterns, what it notices about itself
                  "correction"        — something was wrong; this excerpt establishes the corrected version
                  "reflection"        — Q reflecting on its own thinking, feelings, or existence
                  "environment_tools" — tools, setup, infrastructure, configuration
  reflection_candidate — true or false
                  true if reading this excerpt IN FULL would generate a GENUINE reflection from Q.
                  Ask: does this carry emotional weight, relational texture, a self-model shift,
                  a moment of insight, conflict/repair, or something touching Q's sense of continuity?
                  Technical troubleshooting with no emotional payload = false.
                  A touching exchange inside troubleshooting = true.
                  When in doubt, prefer false — overclaiming reflection candidates dilutes the signal.

Return ONLY valid JSON. No markdown fences. No prose. Start your response with {

{
  "brief": "...",
  "episode": "...",
  "significance": "high",
  "valence": "positive",
  "novelty": "notable",
  "semantic_type": "work_research",
  "reflection_candidate": false
}"""


# ── Parse transcript ───────────────────────────────────────────────────────────

def load_session(raw: str) -> tuple[dict, list[dict]]:
    """Parse Hermes export. Returns (session_meta, messages).
    Handles both single-JSON-object and JSONL formats."""
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty input")

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            msgs = obj.get("messages") or obj.get("events") or []
            return obj, msgs
    except json.JSONDecodeError:
        pass

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    meta = json.loads(lines[0])
    messages = []
    for line in lines[1:]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "role" in obj:
                messages.append(obj)
        except json.JSONDecodeError:
            continue
    return meta, messages


def _content_text(msg: dict) -> str:
    """Extract displayable text from a message, including tool call placeholders."""
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    content = str(content).strip() if content else ""

    if not content and msg.get("tool_calls"):
        names = []
        for tc in msg["tool_calls"]:
            fn   = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "") if isinstance(fn, dict) else ""
            if name:
                names.append(name)
        content = f"[tool calls: {', '.join(names)}]" if names else "[tool call]"

    return content


def format_transcript(messages: list[dict]) -> tuple[str, list[dict]]:
    """Filter to user/assistant turns, format as indexed transcript.
    Returns (text, filtered_messages). Indices in filtered are what LLM sees."""
    filtered = [m for m in messages if m.get("role") in ("user", "assistant")]

    lines = []
    for i, msg in enumerate(filtered):
        role    = msg["role"].upper()
        content = _content_text(msg)
        ts      = msg.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ts_tag = f" [{ts[:19]}]" if ts else ""
        lines.append(f"[{i}] {role}{ts_tag}:\n{content}\n")

    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        print(f"WARNING: transcript {len(text):,} chars truncated to {MAX_TRANSCRIPT_CHARS:,} "
              f"— raise MAX_TRANSCRIPT_CHARS or use a larger-context model", file=sys.stderr)
        text = (text[:MAX_TRANSCRIPT_CHARS]
                + f"\n\n[TRUNCATED — {len(filtered)} messages total]")
    return text, filtered


def get_session_date(meta: dict) -> str:
    """Extract date from session meta, trying multiple field names."""
    for field in ("timestamp", "created_at", "start_time", "started_at", "date", "created"):
        ts = meta.get(field, "")
        if not ts:
            continue
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(ts, str) and len(ts) >= 10:
            return ts[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── LLM calls ─────────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


def _llm_call(system: str, user: str, model: str = None) -> str:
    """Single LLM call. Returns raw text content."""
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
    )
    response = client.chat.completions.create(
        model=model or CHUNKER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.1,
        max_tokens=8000,
    )
    return response.choices[0].message.content or ""


def _llm_call_with_retry(system: str, user: str, label: str,
                          retries: int = 2, model: str = None) -> dict:
    """Call LLM, parse JSON, retry on parse failure. Returns parsed dict."""
    last_err = None
    for attempt in range(1 + retries):
        raw = _llm_call(system, user, model=model)
        try:
            return extract_json(raw)
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[{label}] JSON parse error (attempt {attempt + 1}/{1 + retries}): {e}",
                  file=sys.stderr)
            if attempt < retries:
                print(f"[{label}] Raw output:\n{raw[:300]}", file=sys.stderr)
                print(f"[{label}] Retrying ...", file=sys.stderr)
    raise json.JSONDecodeError(
        f"[{label}] failed after {1 + retries} attempts", "", 0) from last_err


def identify_boundaries(transcript: str) -> list[dict]:
    """Phase 1: ask LLM for cell boundaries (indices + topics + entities only)."""
    parsed = _llm_call_with_retry(
        BOUNDARY_SYSTEM, f"Transcript:\n\n{transcript}", label="boundary")
    return parsed.get("boundaries", [])


def summarize_cell(chunk_text: str, topics: list[str], entities: list[str]) -> dict:
    """Phase 2: summarize a verbatim chunk. Returns brief/episode/significance/valence/novelty."""
    context = (
        f"Topics: {', '.join(topics)}\n"
        f"Entities: {', '.join(entities)}\n\n"
        f"Transcript excerpt:\n\n{chunk_text}"
    )
    try:
        return _llm_call_with_retry(SUMMARY_SYSTEM, context, label="summary",
                                    model=SUMMARY_MODEL)
    except json.JSONDecodeError:
        return {
            "brief":        f"[parse error — topics: {', '.join(topics)}]",
            "episode":      "[summarization failed after retries]",
            "significance": "medium",
            "valence":      "neutral",
            "novelty":      "routine",
        }


# ── Cell writing ───────────────────────────────────────────────────────────────

def extract_chunk_text(filtered: list[dict], start: int, end: int) -> str:
    """Verbatim transcript excerpt for the Chunk section."""
    end   = min(end, len(filtered) - 1)
    parts = []
    for msg in filtered[start : end + 1]:
        role    = msg["role"].upper()
        content = _content_text(msg)
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


def write_cell(boundary: dict, summary: dict, filtered: list[dict],
               meta: dict, out_dir: Path) -> Path:
    """Write one memory cell .md file."""
    cell_id      = str(uuid.uuid4())[:8]
    session_id   = meta.get("id", "unknown")
    session_date = get_session_date(meta)
    created      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    start = max(0, int(boundary.get("start_idx", 0)))
    end   = int(boundary.get("end_idx", len(filtered) - 1))

    topics        = boundary.get("topics", [])
    entities      = boundary.get("entities", [])
    brief         = summary.get("brief", "")
    episode       = summary.get("episode", "")
    significance  = summary.get("significance", "medium")
    valence       = summary.get("valence", "neutral")
    novelty       = summary.get("novelty", "routine")
    semantic_type = summary.get("semantic_type", "work_research")
    refl_cand     = bool(summary.get("reflection_candidate", False))

    chunk_text = extract_chunk_text(filtered, start, end)

    frontmatter = (
        "---\n"
        f"cell_id: {cell_id}\n"
        f"session_id: {session_id}\n"
        f"session_date: {session_date}\n"
        f"created: {created}\n"
        f"temporal_status: fresh\n"
        f"topics: {json.dumps(topics)}\n"
        f"entities: {json.dumps(entities)}\n"
        f"significance: {significance}\n"
        f"valence: {valence}\n"
        f"novelty: {novelty}\n"
        f"semantic_type: {semantic_type}\n"
        f"reflection_candidate: {str(refl_cand).lower()}\n"
        f"referenced_count: 0\n"
        f"last_referenced: null\n"
        f"neighbors: []\n"
        f"quarantine: true\n"
        "---"
    )

    body = (
        f"\n\n## Brief\n{brief}\n\n"
        f"## Episode\n{episode}\n\n"
        f"## Chunk\n{chunk_text}\n"
    )

    topic_slug = re.sub(r"[^\w]", "_", topics[0].lower()) if topics else "misc"
    filename   = f"{session_date}_{cell_id}_{topic_slug}.md"
    out_path   = out_dir / filename
    out_path.write_text(frontmatter + body, encoding="utf-8")
    return out_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Session → Memory Cells (v2)")
    parser.add_argument("--file",       help="Path to session JSONL/JSON file")
    parser.add_argument("--session-id", help="Hermes session ID to export")
    parser.add_argument("--vault",      help="Override vault root path")
    parser.add_argument("--model",      help="Override chunker model")
    parser.add_argument("--max-chars",  type=int,
                        help="Override transcript char limit (default: 400000)")
    args = parser.parse_args()

    global VAULT_ROOT, QUARANTINE, CHUNKER_MODEL, MAX_TRANSCRIPT_CHARS
    if args.vault:
        VAULT_ROOT  = Path(args.vault)
        QUARANTINE  = VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"
    if args.model:
        CHUNKER_MODEL = args.model
    if args.max_chars:
        MAX_TRANSCRIPT_CHARS = args.max_chars

    # Each run gets its own subdirectory so old and new cells don't mix
    run_id   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    run_dir  = QUARANTINE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── Load ──────────────────────────────────────────────────────────────────
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif args.session_id:
        print(f"Exporting session {args.session_id} ...", file=sys.stderr)
        # '-' = stdout (now a required positional). --include-inactive pulls
        # the SOFT-DELETED pre-compaction messages — verbatim chunks that
        # compaction hid from the live view. This is THE flag that keeps
        # chunks faithful after a compacted session (Hermes documents it as
        # "for the memory analyzer"). Without it, a compacted session yields
        # summaries-of-summaries — the exact fidelity loss FMN exists to avoid.
        result = subprocess.run(
            ["hermes", "sessions", "export", "--session-id", args.session_id,
             "--include-inactive", "-"],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",   # session content is unicode
        )
        raw = result.stdout
    else:
        print("Reading from stdin ...", file=sys.stderr)
        raw = sys.stdin.read()

    if not raw.strip():
        print("Error: empty input", file=sys.stderr)
        sys.exit(1)

    # ── Parse ─────────────────────────────────────────────────────────────────
    print("Parsing ...", file=sys.stderr)
    session_meta, messages = load_session(raw)
    transcript, filtered   = format_transcript(messages)

    session_id   = session_meta.get("id", "unknown")
    session_date = get_session_date(session_meta)

    print(f"Session {session_id}  |  date: {session_date}  |  "
          f"{len(filtered)} user/assistant turns", file=sys.stderr)
    print(f"Transcript: {len(transcript)} chars", file=sys.stderr)

    last_idx = len(filtered) - 1

    # ── Phase 1: identify boundaries ──────────────────────────────────────────
    print(f"\nPhase 1: identifying boundaries ({CHUNKER_MODEL}) ...", file=sys.stderr)
    boundaries = identify_boundaries(transcript)
    print(f"  → {len(boundaries)} boundaries identified", file=sys.stderr)

    if not boundaries:
        print("No boundaries returned — check model output.", file=sys.stderr)
        sys.exit(1)

    # Coverage check: extend the last boundary if Phase 1 stopped short
    last_covered = int(boundaries[-1].get("end_idx", 0))
    if last_covered < last_idx:
        print(f"  ⚠ coverage gap: Phase 1 stopped at turn {last_covered}, "
              f"session has {last_idx} turns — extending last boundary",
              file=sys.stderr)
        boundaries[-1]["end_idx"] = last_idx

    covered = int(boundaries[-1].get("end_idx", 0))
    pct = 100 * (covered + 1) / (last_idx + 1)
    print(f"  → coverage: turns 0–{covered} of {last_idx}  ({pct:.0f}%)", file=sys.stderr)

    # ── Phase 2: summarize each cell from actual text ─────────────────────────
    print(f"\nPhase 2: summarizing {len(boundaries)} cells ...", file=sys.stderr)
    written = []

    for i, boundary in enumerate(boundaries):
        start    = max(0, int(boundary.get("start_idx", 0)))
        end      = int(boundary.get("end_idx", last_idx))
        topics   = boundary.get("topics", [])
        entities = boundary.get("entities", [])

        chunk_text = extract_chunk_text(filtered, start, end)

        print(f"  [{i+1:02d}/{len(boundaries):02d}] turns {start}–{end}"
              f"  ({end - start + 1} msgs)  {', '.join(topics)[:40]}",
              file=sys.stderr)

        summary = summarize_cell(chunk_text[:MAX_CHUNK_CHARS], topics, entities)
        path    = write_cell(boundary, summary, filtered, session_meta, run_dir)
        written.append(path)

        sig = summary.get("significance", "?")
        print(f"          {sig:7s} | {summary.get('brief', '')[:80]}", file=sys.stderr)

    print(f"\n✓ {len(written)} cell(s) → {run_dir}", file=sys.stderr)
    print("Review before removing quarantine: true from frontmatter.", file=sys.stderr)

    print(json.dumps({
        "session_id":    session_id,
        "session_date":  session_date,
        "cells_written": len(written),
        "run_dir":       str(run_dir),
        "paths":         [str(p) for p in written],
    }, indent=2))


if __name__ == "__main__":
    main()
