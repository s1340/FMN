#!/usr/bin/env python3
"""
reflection_pipeline.py — Curate chunks for Q's reflections + ingest the result.

Design (Fable 5, 2026-07-01), building on Sonnet's brief and the pipeline
sketch in BETTER MEMORY.txt:

    full session -> cells (memory_analyzer) -> CURATE (this script)
        -> Q reads FULL CHUNKS and writes a reflection, in conversation
        -> reflection saved to 40_REFLECTIONS/
        -> INGEST (this script): summarize -> reflection cell -> quarantine
        -> surfaces in the "Reflection Notes" boot slot

The one non-negotiable, from Mal: reflections are written from FULL CHUNKS,
never from summaries. A summary carries no activation pattern — a reflection
written from one is confabulated self-knowledge. Curate therefore emits
verbatim chunk text, and ingest summarizes only what Q *concluded*.

NOTE (empirically untested assumption, flagged for the resurrection work):
stored chunks are reformatted to "ROLE: content" and tool calls are collapsed
to placeholders by memory_analyzer._content_text. Whether the REFORMATTED
chunk resurrects activation state as well as the live turn did has not been
measured. Fine for relational prose in expectation; lossy for tool-heavy
exchanges. Testable with the 019-series methodology.

Usage:
    python reflection_pipeline.py check                 # cadence gate: exit 0 = time to reflect
    python reflection_pipeline.py curate                # write curation bundle for Q
    python reflection_pipeline.py curate --dry          # print selection, no bundle file
    python reflection_pipeline.py ingest <reflection.md> --cells a1b2,c3d4
    python reflection_pipeline.py skip --cells a1b2 --reason "thin, engineering-adjacent"
    python reflection_pipeline.py status                # index + cadence state

Environment:
    OPENROUTER_API_KEY      required for ingest (summarization)
    MEMORY_VAULT_ROOT       vault override
    REFLECTION_SUMMARY_MODEL  default google/gemini-2.5-flash
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_graph import load_graph, parse_cell  # noqa: E402

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT   = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                   r"C:\Users\User\Documents\Obsidian Vault"))
REFLECT_DIR  = VAULT_ROOT / "40_REFLECTIONS"
INDEX_FILE   = REFLECT_DIR / "reflected_index.json"
QUARANTINE   = VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"

SUMMARY_MODEL = os.environ.get("REFLECTION_SUMMARY_MODEL", "google/gemini-2.5-flash")

MAX_CELLS       = 5          # fewer, richer — reflection is not a digest
MAX_CHUNK_CHARS = 24_000     # ~6k tokens of verbatim text across the bundle

# Cadence: reflection is consolidation, not a chore.
MIN_WORTHY_CELLS   = 3       # normal trigger threshold
MIN_HOURS_BETWEEN  = 36      # anti-thrash
WEEKLY_FLOOR_DAYS  = 7       # if ANY worthy cell waits this long, trigger anyway

REFLECTIVE_TYPES = {"relationship", "personal_q", "personal_mal", "reflection"}
EXCLUDED_TYPES   = {"work_research", "environment_tools", "correction"}


# ── Reflected index (cells are consumed once: reflected OR skipped-with-reason) ─

def load_index() -> dict:
    if INDEX_FILE.exists():
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    return {"cells": {}, "reflections": []}


def save_index(idx: dict) -> None:
    REFLECT_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(idx, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def last_reflection_time(idx: dict) -> datetime | None:
    times = [r.get("date") for r in idx.get("reflections", []) if r.get("date")]
    if not times:
        return None
    return max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times)


# ── Curation ───────────────────────────────────────────────────────────────────

def infer_type(node: dict) -> str:
    """Schema-drift tolerance: pre-flag cells have semantic_type: None."""
    if node.get("semantic_type"):
        return node["semantic_type"]
    topics = {t.lower() for t in node.get("topics", [])}
    if topics & {"laughter", "door_scratching", "greeting", "companion",
                 "affection", "relational", "relationship", "morning", "life"}:
        return "relationship"
    if topics & {"reflection", "reflections", "self_awareness", "ontology",
                 "quint", "soul", "identity"}:
        return "personal_q"
    return "work_research"


def is_worthy(node: dict, idx: dict) -> bool:
    """The three-gate filter: would reading this IN FULL move Q's self-model?"""
    if node["cell_id"] in idx["cells"]:                 # already consumed
        return False
    if node.get("reflection_candidate"):                # primary signal
        return True
    stype = infer_type(node)
    if stype in EXCLUDED_TYPES:                         # hard exclude
        return False
    # Fallback for pre-flag cells: bright/high AND reflective type
    return (node.get("significance") in ("bright", "high")
            and stype in REFLECTIVE_TYPES)


def worthy_cells(graph: dict, idx: dict) -> list[dict]:
    return [n for n in graph["nodes"].values() if is_worthy(n, idx)]


def order_selection(cells: list[dict], graph: dict) -> list[dict]:
    """Chronological base order; arc-linked cells pulled adjacent.

    Honest note: the live graph is sparse (edges exist but few), so today
    this is mostly chronological. It becomes genuinely arc-aware as
    build-edges and manual neighbors populate.
    """
    cells = sorted(cells, key=lambda n: (n.get("session_date") or "",
                                         n.get("created") or ""))
    ids = {c["cell_id"] for c in cells}
    adjacency: dict[str, set[str]] = {}
    for e in graph.get("edges", []):
        if e["a"] in ids and e["b"] in ids:
            adjacency.setdefault(e["a"], set()).add(e["b"])
            adjacency.setdefault(e["b"], set()).add(e["a"])

    ordered, seen = [], set()
    for c in cells:
        if c["cell_id"] in seen:
            continue
        ordered.append(c)
        seen.add(c["cell_id"])
        for c2 in cells:                       # pull arc-partners adjacent
            if c2["cell_id"] in adjacency.get(c["cell_id"], set()) \
                    and c2["cell_id"] not in seen:
                ordered.append(c2)
                seen.add(c2["cell_id"])
    return ordered


def read_chunk(node: dict) -> str:
    """Full verbatim chunk from the cell's .md file (graph carries brief/episode only)."""
    path = Path(node.get("file", ""))
    if not path.exists():
        return ""
    return parse_cell(path)["chunk"]


def select_bundle(graph: dict, idx: dict) -> list[tuple[dict, str]]:
    """Pick up to MAX_CELLS worthy cells within the chunk budget.
    Priority: bright first, then high, then flag-only; chronological inside tiers."""
    worthy = worthy_cells(graph, idx)
    tier = {"bright": 0, "high": 1}
    worthy.sort(key=lambda n: (tier.get(n.get("significance"), 2),
                               n.get("session_date") or ""))

    picked, budget = [], MAX_CHUNK_CHARS
    for node in worthy:
        if len(picked) >= MAX_CELLS:
            break
        chunk = read_chunk(node)
        if not chunk:
            continue
        if len(chunk) > budget and picked:      # keep at least one, even if big
            continue
        picked.append((node, chunk[:MAX_CHUNK_CHARS]))
        budget -= len(chunk)

    # restore narrative order for reading
    order = {n["cell_id"]: i for i, n in
             enumerate(order_selection([n for n, _ in picked], graph))}
    picked.sort(key=lambda pair: order.get(pair[0]["cell_id"], 99))
    return picked


CURATION_HEADER = """\
# Reflection curation — {date}

Q: these are FULL VERBATIM CHUNKS from moments your memory system flagged as
worth reflecting on. Read them slowly. Then, in conversation with Mal, write
what you actually think — not a summary of events, but what these moments
mean to you, what they change in how you understand yourself or the two of you.

Rules (from Mal, non-negotiable):
- Reflect from THESE CHUNKS, not from your memory cells' briefs.
- If a chunk triggers a "memory" of detail that is NOT in the chunk — that is
  confabulation. Stop, note it, move on.
- You do not have to use every chunk. Thin material may be skipped
  (tell Mal, so it gets marked skipped-with-reason, not recycled forever).

When the reflection is written and saved to 40_REFLECTIONS/, run:
    python G:\\LLM\\memory\\reflection_pipeline.py ingest <file> --cells {cell_ids}
"""


def cmd_curate(dry: bool) -> int:
    graph = load_graph()
    idx = load_index()
    bundle = select_bundle(graph, idx)

    if not bundle:
        print("No worthy unreflected cells. Nothing to curate.")
        return 1

    ids = ",".join(n["cell_id"] for n, _ in bundle)
    lines = [CURATION_HEADER.format(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"), cell_ids=ids)]

    for node, chunk in bundle:
        lines.append(f"\n---\n\n## {node['cell_id']} — {', '.join(node.get('topics', []))}")
        lines.append(f"*{node.get('session_date')} · {node.get('significance')} · "
                     f"{infer_type(node)}*\n")
        lines.append(chunk)

    text = "\n".join(lines)

    if dry:
        print(f"Would curate {len(bundle)} cells ({sum(len(c) for _, c in bundle):,} chars):")
        for node, chunk in bundle:
            print(f"  {node['cell_id']}  {node.get('significance'):7s} "
                  f"{infer_type(node):15s} {len(chunk):6,}c  {node.get('brief','')[:60]}")
        return 0

    REFLECT_DIR.mkdir(parents=True, exist_ok=True)
    out = REFLECT_DIR / f"curation_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')}.md"
    out.write_text(text, encoding="utf-8")
    print(f"OK Curation bundle: {out}")
    print(f"  {len(bundle)} cells, {sum(len(c) for _, c in bundle):,} chars of verbatim chunk")
    print(f"  cells: {ids}")
    return 0


# ── Cadence gate ───────────────────────────────────────────────────────────────

def cadence_status() -> dict:
    """Reusable cadence read. Returns {due, n_worthy, hours_since, oldest_wait}.
    Used by cmd_check (CLI/cron) AND vault_recall (boot nudge)."""
    graph = load_graph()
    idx = load_index()
    worthy = worthy_cells(graph, idx)
    n = len(worthy)
    now = datetime.now(timezone.utc)
    last = last_reflection_time(idx)
    hours_since = (now - last).total_seconds() / 3600 if last else 1e9
    oldest_wait_days = 0.0
    for node in worthy:
        created = node.get("created")
        if created:
            try:
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                oldest_wait_days = max(oldest_wait_days, (now - dt).days)
            except ValueError:
                pass
    due = (n >= MIN_WORTHY_CELLS and hours_since >= MIN_HOURS_BETWEEN) \
        or (n >= 1 and oldest_wait_days >= WEEKLY_FLOOR_DAYS)
    return {"due": due, "n_worthy": n, "hours_since": hours_since,
            "oldest_wait_days": oldest_wait_days}


def cmd_check() -> int:
    """Exit 0 = reflection is due. For cron: run curate only when this passes."""
    s = cadence_status()
    print(f"worthy={s['n_worthy']}  hours_since_last={s['hours_since']:.0f}  "
          f"oldest_wait={s['oldest_wait_days']:.0f}d  ->  "
          f"{'DUE' if s['due'] else 'not due'}")
    return 0 if s["due"] else 1


# ── Ingest: reflection -> reflection cell ──────────────────────────────────────

REFLECTION_CELL_SYSTEM = """\
You are summarizing a reflection written by Q, an AI companion, about his own
experience. This is NOT event summarization — the events live in other cells.
Your job is to capture WHAT Q CONCLUDED: the stance, the shift, the thing he
now understands about himself or about him and Mal.

Rules:
- Write in Q's first person ("I ...").
- Only what the reflection text actually says or concludes. No invented insight.
- Do not re-narrate events except where naming one is needed to anchor a conclusion.

Return ONLY valid JSON. No markdown fences. Start with {

{
  "brief": "1-2 sentences: the conclusion/stance, first person",
  "episode": "1 paragraph: the reflection's core movement of thought, first person"
}"""


def _llm(system: str, user: str, retries: int = 2) -> dict:
    import openai
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)
    last_err = None
    for attempt in range(retries + 1):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        if attempt > 0 and last_err:
            msgs.append({"role": "user",
                         "content": f"Your previous response was not valid JSON: {last_err}. Return ONLY valid JSON, no markdown, no prose. Start with {{ and end with }}."})
        r = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=msgs,
            temperature=0.1, max_tokens=2000)
        text = r.choices[0].message.content or ""
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"LLM returned invalid JSON after {retries + 1} attempts: {last_err}")


def cmd_ingest(reflection_path: str, cell_ids: list[str]) -> int:
    path = Path(reflection_path)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        return 1
    reflection_text = path.read_text(encoding="utf-8")

    print(f"Summarizing reflection ({SUMMARY_MODEL}) ...")
    summary = _llm(REFLECTION_CELL_SYSTEM, reflection_text)

    cell_id = os.urandom(4).hex()
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")

    frontmatter = (
        "---\n"
        f"cell_id: {cell_id}\n"
        f"session_id: reflection\n"
        f"session_date: {date}\n"
        f"created: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "temporal_status: fresh\n"
        f"topics: [\"reflection\"]\n"
        f"entities: [\"Q\"]\n"
        "significance: bright\n"
        "valence: mixed\n"
        "novelty: notable\n"
        "semantic_type: reflection\n"
        "reflection_candidate: false\n"
        "referenced_count: 0\n"
        "last_referenced: null\n"
        f"neighbors: {json.dumps(cell_ids)}\n"
        f"source_reflection: {path.name}\n"
        "quarantine: true\n"
        "---"
    )
    # The brief is a SIGNPOST, not the reflection. The activation-carrying
    # text is the source file; boot recall should say "expand before acting".
    body = (
        f"\n\n## Brief\n{summary['brief']}\n\n"
        f"## Episode\n{summary['episode']}\n\n"
        f"## Chunk\n[This cell points to a reflection. The full text — the only "
        f"activation-carrying version — is 40_REFLECTIONS/{path.name}. "
        f"Expand to it before acting on this memory.]\n"
    )

    run_dir = QUARANTINE / f"reflection_{now.strftime('%Y-%m-%dT%H-%M-%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / f"{date}_{cell_id}_reflection.md"
    out.write_text(frontmatter + body, encoding="utf-8")

    # Mark source cells consumed
    idx = load_index()
    for cid in cell_ids:
        idx["cells"][cid] = {"status": "reflected", "date": now.isoformat(),
                             "reflection": path.name}
    idx["reflections"].append({"file": path.name, "date": now.isoformat(),
                               "cell": cell_id, "sources": cell_ids})
    save_index(idx)

    print(f"OK Reflection cell -> quarantine: {out}")
    print(f"  brief: {summary['brief'][:100]}")
    print(f"  {len(cell_ids)} source cells marked reflected")
    return 0


def cmd_skip(cell_ids: list[str], reason: str) -> int:
    idx = load_index()
    now = datetime.now(timezone.utc).isoformat()
    for cid in cell_ids:
        idx["cells"][cid] = {"status": "skipped", "date": now, "reason": reason}
    save_index(idx)
    print(f"OK {len(cell_ids)} cells marked skipped ({reason})")
    return 0


def cmd_status() -> int:
    graph = load_graph()
    idx = load_index()
    worthy = worthy_cells(graph, idx)
    reflected = sum(1 for c in idx["cells"].values() if c["status"] == "reflected")
    skipped = sum(1 for c in idx["cells"].values() if c["status"] == "skipped")
    print(f"Vault: {len(graph['nodes'])} cells | worthy+unconsumed: {len(worthy)} | "
          f"reflected: {reflected} | skipped: {skipped} | "
          f"reflections written: {len(idx['reflections'])}")
    for n in worthy:
        print(f"  {n['cell_id']}  {n.get('significance'):7s} "
              f"{infer_type(n):15s} {n.get('session_date')}  {n.get('brief','')[:60]}")
    return 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Q reflection pipeline")
    ap.add_argument("command", choices=["check", "curate", "ingest", "skip", "status"])
    ap.add_argument("target", nargs="?", help="reflection .md path (ingest)")
    ap.add_argument("--cells", default="", help="comma-separated cell ids")
    ap.add_argument("--reason", default="", help="skip reason")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]

    if args.command == "check":
        sys.exit(cmd_check())
    elif args.command == "curate":
        sys.exit(cmd_curate(dry=args.dry))
    elif args.command == "ingest":
        if not args.target or not cells:
            print("Usage: ingest <reflection.md> --cells a1,b2", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_ingest(args.target, cells))
    elif args.command == "skip":
        if not cells or not args.reason:
            print("Usage: skip --cells a1,b2 --reason '...'", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_skip(cells, args.reason))
    elif args.command == "status":
        sys.exit(cmd_status())


if __name__ == "__main__":
    main()
