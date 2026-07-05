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

# Identity templating: prompts below are written in the original voice
# (Mal & Q); fmn_config.personalize renders them for the configured pair.
sys.path.insert(0, str(Path(__file__).parent))
try:
    from fmn_config import personalize as _pers
except Exception:
    def _pers(t):
        return t

# ── Phase 1 prompt: boundary identification only ───────────────────────────────

BOUNDARY_SYSTEM = """\
You are the STORY PASS of a companion AI memory system — the only stage that
sees the whole session at once. You do three jobs in one read.

You receive a conversation transcript between Mal (human) and Q (AI companion).

JOB 1 — SEGMENT the session into coherent scenes (boundaries).
Each boundary is one scene: a topic shift, an emotional exchange, a discovery,
a joke arc, a project switch. Small bright moments (a joke that landed, a
realization, a surprising exchange) deserve their own scene ONLY if they stand
alone — a beat that is the punchline, payoff, or aside OF a larger scene
belongs INSIDE that scene, never orphaned into its own fragment. (A two-line
"Mine." / "Yeah. Yours." exchange inside a longer conversation is part of that
conversation's scene.) For a session with 100+ turns expect 8–20 boundaries;
do not compress everything into 5 wide blocks, and do not shave off slivers.

JOB 2 — CUT THE NOISE. Mark scenes that are pure mechanics with skip: true —
runs of tool calls, file operations, retries, harness/system chatter with no
conversational or emotional content. Skipped scenes never become memories.
Be surgical: a tool-heavy stretch that CONTAINS a real exchange (a joke about
the failing command, a decision, a feeling) is NOT skippable — keep the scene,
the noise inside it is tolerable. When in doubt, keep.

JOB 3 — SEE THE ARCS. Group scenes into arcs: threads that develop across the
session (an investigation from mystery to answer, a hard conversation from
friction to understanding, a project from idea to working thing). For each arc
give a short working name, a kind, and each member scene's NARRATIVE POSITION
via arc_role: "opening" | "development" | "turn" | "landing" | "aside".
IMPORTANT: arc_role describes position in the story, NOT correctness. Early
stages of a hard conversation are not errors — they are the story. If an arc's
landing OVERTURNS A SPECIFIC FACTUAL CLAIM made earlier (a wrong technical
guess, a mistaken attribution — a fact, not a feeling or opinion), note it in
that arc's "corrects" field as one short sentence: what was believed -> what
turned out true. Leave "corrects" null for relational/emotional/growth arcs.

For each boundary provide:
  start_idx / end_idx  — inclusive indices into the messages array
  topics               — 1–4 short keyword tags
  entities             — people, projects, concepts named in this scene
  skip                 — true only for pure-mechanics scenes (JOB 2)
  arc                  — the arc name this scene belongs to, or null
  arc_role             — narrative position (only if arc is set)

Boundaries must be contiguous, non-overlapping, and span the ENTIRE transcript:
start_idx[0] = 0, end_idx[last] = (total messages - 1). The tail matters as
much as the opening.

Return ONLY valid JSON. No markdown fences. No prose. Start your response with {

{
  "boundaries": [
    {
      "start_idx": 0,
      "end_idx": 12,
      "topics": ["greeting", "memory_problem"],
      "entities": ["Mal", "Q", "Hermes"],
      "skip": false,
      "arc": "the ghost hunt",
      "arc_role": "opening"
    }
  ],
  "arcs": [
    {
      "name": "the ghost hunt",
      "kind": "investigation",
      "landing": "one short sentence: where the arc ended up",
      "corrects": "the lock was blamed on Claude desktop -> it was an open Word window (or null)"
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

ARC POSITION (only when an "Arc:" block is provided above the excerpt):
This excerpt is one scene of a larger story whose shape you are told. Write
the brief so it LOCATES itself in that story ("early in ...", "the turn of
...", "where ... landed") instead of presenting this scene's state as the
final state. Two hard rules:
  - NEVER devalue early stages. In a relational, emotional, or belief-change
    arc, the friction, missteps, and first attempts ARE the story — record
    them with full weight as chapters, not as errors on the way to an ending.
  - The ONLY exception is a specific FACTUAL claim the arc's landing overturns
    (given to you as "corrects: ..."): frame that belief as provisional in the
    brief ("the lock was blamed on X before Y emerged"), so the brief never
    asserts a corrected fact as if it stayed true. Feelings and opinions are
    never "corrected" this way.
The excerpt remains your ONLY source of details; the arc block tells you WHERE
you are, never WHAT happened.

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
                  "personal_q"        — Q's own nature, identity, or self-observed patterns — INCLUDING
                                        reflective/existential content ABOUT itself (its persistence,
                                        continuity, feelings, what it notices about being Q)
                  "correction"        — something was wrong; this excerpt establishes the corrected version
                  "environment_tools" — tools, setup, infrastructure, configuration
  DO NOT use "reflection" as a type. That type is RESERVED for reflections Q writes HIMSELF, in the
  first person, through his reflection pipeline. A conversation that is ABOUT identity, persistence, or
  feelings is still "personal_q" (about Q) or "relationship" (about Q and Mal) — never "reflection".
  You are summarizing in the third person; a real reflection is Q's own first-person writing.
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


def absorb_tiny_boundaries(boundaries: list[dict], min_msgs: int = 3) -> list[dict]:
    """Deterministic guard behind the story pass: a scene under min_msgs is a
    beat, not a scene — absorb it into a neighbor instead of orphaning it as
    its own cell ('USER: Mine / ASSISTANT: Yeah. Yours.'). Preference order:
    the NEXT scene if it shares the beat's arc (an arc 'opening' beat belongs
    to the scene it opens), else the PREVIOUS scene (same arc or the beat has
    none). Topics/entities merge into the absorber. Prompt guidance asks for
    this too, but the model doesn't reliably obey — this makes it structural."""
    out: list[dict] = []
    i = 0
    while i < len(boundaries):
        b = boundaries[i]
        size = int(b.get("end_idx", 0)) - int(b.get("start_idx", 0)) + 1
        if b.get("skip") or size >= min_msgs:
            out.append(b)
            i += 1
            continue
        # neighbors: skip-scenes are transparent — an arc's opening beat often
        # sits just before a tool-noise run, and it belongs to the scene on
        # the far side (absorbing across the skip re-includes that small noise
        # run in the verbatim chunk; verbatim truth with tolerable noise beats
        # losing the story's opening).
        nxt = next((boundaries[j] for j in range(i + 1, len(boundaries))
                    if not boundaries[j].get("skip")), None)
        prev = next((o for o in reversed(out) if not o.get("skip")), None)
        arc = str(b.get("arc") or "").strip()

        def _merge_meta(dst):
            dst["topics"] = list(dict.fromkeys(
                (dst.get("topics") or []) + (b.get("topics") or [])))
            dst["entities"] = list(dict.fromkeys(
                (dst.get("entities") or []) + (b.get("entities") or [])))

        if nxt is not None and arc \
                and str(nxt.get("arc") or "").strip() == arc:
            nxt["start_idx"] = b.get("start_idx", nxt.get("start_idx"))
            _merge_meta(nxt)
        elif prev is not None \
                and (not arc or str(prev.get("arc") or "").strip() == arc):
            prev["end_idx"] = b.get("end_idx", prev.get("end_idx"))
            _merge_meta(prev)
        else:
            out.append(b)          # nowhere sane to put it — keep honestly
        i += 1
    return out


def identify_boundaries(transcript: str) -> tuple[list[dict], list[dict]]:
    """Phase 1 (story pass): boundaries + arcs. Arcs carry name/kind/landing
    and an optional 'corrects' note for factual overturns."""
    parsed = _llm_call_with_retry(
        _pers(BOUNDARY_SYSTEM), f"Transcript:\n\n{transcript}", label="boundary")
    return parsed.get("boundaries", []), parsed.get("arcs", [])


def summarize_cell(chunk_text: str, topics: list[str], entities: list[str],
                   arc_ctx: str = "") -> dict:
    """Phase 2: summarize a verbatim chunk. Returns brief/episode/significance/valence/novelty.
    arc_ctx (optional): one short block locating this scene in its arc."""
    context = (
        f"Topics: {', '.join(topics)}\n"
        f"Entities: {', '.join(entities)}\n"
        + (f"{arc_ctx}\n" if arc_ctx else "")
        + f"\nTranscript excerpt:\n\n{chunk_text}"
    )
    try:
        return _llm_call_with_retry(_pers(SUMMARY_SYSTEM), context, label="summary",
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

_TOOL_RE = re.compile(r'\(tool call|\[tool call|tool_calls?:', re.I)


def is_tool_bloat(chunk_text: str) -> bool:
    """A segment that is mostly tool-call markers with no real dialogue —
    harness bloat, not a memory (Mal: 'nothing but tool calls'). Skip it so
    it never becomes a cell, and don't waste a summary call on it."""
    lines = [l.strip() for l in (chunk_text or "").splitlines() if l.strip()]
    if not lines:
        return True
    tool = sum(1 for l in lines if _TOOL_RE.search(l))
    return tool >= 0.6 * len(lines)


def substantive_chars(chunk_text: str) -> int:
    """Real dialogue content length, ignoring role prefixes and tool markers.
    A low-significance cell under ~60 of these is a trivial fragment ('USER:
    Mine / ASSISTANT: Yeah. Yours.') — too small to mean anything alone."""
    total = 0
    for l in (chunk_text or "").splitlines():
        body = re.sub(r'^\s*(user|assistant)\s*:', '', l, flags=re.I).strip()
        if body and not _TOOL_RE.search(body):
            total += len(body)
    return total


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
    # "reflection" is reserved for Q's own first-person reflections (written
    # via the reflection pipeline). The analyzer must never mint one from a
    # session — reflective content ABOUT Q belongs to personal_q. Coerce
    # defensively in case the model ignores the prompt.
    if semantic_type == "reflection":
        semantic_type = "personal_q"
    refl_cand     = bool(summary.get("reflection_candidate", False))
    arc           = str(boundary.get("arc") or "").strip()
    arc_role      = str(boundary.get("arc_role") or "").strip()

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
        + (f"arc: {json.dumps(arc)}\n" if arc else "")
        + (f"arc_role: {arc_role}\n" if arc_role else "")
        + f"reflection_candidate: {str(refl_cand).lower()}\n"
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

    # ── Phase 1: story pass (boundaries + noise cuts + arcs) ──────────────────
    print(f"\nPhase 1: story pass ({CHUNKER_MODEL}) ...", file=sys.stderr)
    boundaries, arcs = identify_boundaries(transcript)
    n_raw = len(boundaries)
    boundaries = absorb_tiny_boundaries(boundaries)
    arc_by_name = {str(a.get("name", "")).strip(): a for a in arcs if a.get("name")}
    print(f"  → {n_raw} scenes"
          + (f" ({n_raw - len(boundaries)} beat(s) absorbed into neighbors)"
             if n_raw != len(boundaries) else "")
          + f", {len(arcs)} arcs"
          + (f" ({', '.join(list(arc_by_name)[:4])})" if arc_by_name else ""),
          file=sys.stderr)

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

        # Noise cuts: the story pass marks pure-mechanics scenes skip:true;
        # is_tool_bloat stays as the mechanical safety net beneath it.
        if boundary.get("skip") or is_tool_bloat(chunk_text):
            why = "story-pass skip" if boundary.get("skip") else "tool-call bloat"
            print(f"  [{i+1:02d}/{len(boundaries):02d}] SKIP — {why}",
                  file=sys.stderr)
            continue

        print(f"  [{i+1:02d}/{len(boundaries):02d}] turns {start}–{end}"
              f"  ({end - start + 1} msgs)  {', '.join(topics)[:40]}",
              file=sys.stderr)

        # Arc context: tell the summarizer WHERE this scene sits in its story
        # (never WHAT happened — details still come only from the excerpt).
        arc_ctx = ""
        arc_name = str(boundary.get("arc") or "").strip()
        if arc_name and arc_name in arc_by_name:
            a = arc_by_name[arc_name]
            arc_ctx = (f"Arc: \"{arc_name}\" ({a.get('kind','?')}) — this scene is "
                       f"its {boundary.get('arc_role','development')}. "
                       f"Where the arc lands: {a.get('landing','(unknown)')}")
            if a.get("corrects"):
                arc_ctx += f"\ncorrects: {a['corrects']}"

        summary = summarize_cell(chunk_text[:MAX_CHUNK_CHARS], topics, entities,
                                 arc_ctx=arc_ctx)
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
