#!/usr/bin/env python3
"""
merge_cells.py — Collapse adjacent quarantine cells with overlapping topics.

Two modes:
  --preview   Scan a quarantine run directory, identify merge candidates,
              output a proposal file for Mal to approve.
  --execute   Read approved proposals, collapse each group into one cell
              (re-summarized via LLM), archive originals.

Merge criterion (Sonnet's refinement):
  - adjacent cells (in file order = transcript order)
  - 2+ shared topics
  - same valence
  - NO cell in the group has significance: bright
    (bright cells don't get absorbed into something blander)

Usage:
    python merge_cells.py --preview  <quarantine_run_dir>
    python merge_cells.py --execute <proposals_file>

Environment:
    OPENROUTER_API_KEY   required (for --execute re-summarization)
    MEMORY_CHUNKER_MODEL optional (default: meta-llama/llama-3.3-70b-instruct)
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import openai
import yaml

# ── Config ─────────────────────────────────────────────────────────────────────

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CHUNKER_MODEL  = os.environ.get("MEMORY_CHUNKER_MODEL",
                                 "meta-llama/llama-3.3-70b-instruct")
MAX_MERGE_CHARS = 16_000  # ceiling for merged chunk sent to LLM

# ── Cell I/O ──────────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

def parse_cell(path: Path) -> dict:
    """Read a .md cell file. Returns dict with frontmatter + body sections."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"No frontmatter in {path.name}")
    fm = yaml.safe_load(m.group(1))
    body = m.group(2)

    # Split body into Brief / Episode / Chunk
    sections = {"brief": "", "episode": "", "chunk": ""}
    current = None
    for line in body.splitlines():
        if line.strip() == "## Brief":
            current = "brief"
        elif line.strip() == "## Episode":
            current = "episode"
        elif line.strip() == "## Chunk":
            current = "chunk"
        elif current:
            sections[current] += line + "\n"

    return {
        "path": path,
        "frontmatter": fm,
        "brief": sections["brief"].strip(),
        "episode": sections["episode"].strip(),
        "chunk": sections["chunk"].strip(),
    }


def load_run(run_dir: Path) -> list[dict]:
    """Load all cell .md files from a quarantine run directory, in transcript order."""
    files = [f for f in run_dir.glob("*.md") if not f.name.startswith("merge_proposals")]
    cells = [parse_cell(f) for f in files]
    # Sort by created timestamp = transcript order (cells are written sequentially)
    cells.sort(key=lambda c: str(c["frontmatter"].get("created", "")))
    return cells


# ── Merge grouping ────────────────────────────────────────────────────────────

def shared_topics(a: list[str], b: list[str]) -> list[str]:
    """Return topics shared between two cells (case-insensitive)."""
    a_set = {t.lower().strip() for t in a}
    b_set = {t.lower().strip() for t in b}
    return [t for t in a if t.lower().strip() in b_set]


MERGE_SYSTEM = """\
You are a memory cell merge judge for a companion AI system.

You receive the briefs of two ADJACENT memory cells from the same session.
Decide whether they should be collapsed into a single memory cell.

MERGE if:
  - They are the same topic arc continued across a boundary (e.g. two parts
    of the same debugging loop, two exchanges in the same emotional beat)
  - Merging would not lose a distinct moment worth remembering

DO NOT MERGE if:
  - Either cell is significance "bright" (bright moments stay standalone)
  - They are distinct emotional arcs or distinct discoveries
  - The valence differs (positive vs negative vs mixed vs neutral)
  - One is a technical discussion and the other is an emotional exchange

Return ONLY valid JSON. No markdown fences. No prose. Start your response with {

{
  "should_merge": true,
  "reason": "both are the same analyzer-debugging loop continued across two boundaries"
}"""


def _llm_call(system: str, user: str, max_tokens: int = 2000) -> str:
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
    )
    response = client.chat.completions.create(
        model=CHUNKER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def extract_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


def _llm_call_with_retry(system: str, user: str, label: str,
                          retries: int = 2) -> dict:
    last_err = None
    for attempt in range(1 + retries):
        raw = _llm_call(system, user)
        try:
            return extract_json(raw)
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[{label}] JSON parse error (attempt {attempt + 1}/{1 + retries}): {e}",
                  file=sys.stderr)
            if attempt < retries:
                print(f"[{label}] Retrying ...", file=sys.stderr)
    raise json.JSONDecodeError(f"[{label}] failed after {1 + retries} attempts", "", 0) from last_err


def should_merge_llm(a: dict, b: dict) -> tuple[bool, str]:
    """Ask LLM whether two adjacent cells should be merged. Returns (should, reason)."""
    fm_a, fm_b = a["frontmatter"], b["frontmatter"]

    # Fast path: bright cells never merge
    if fm_a.get("significance") == "bright" or fm_b.get("significance") == "bright":
        return False, "bright cell — never absorbed"

    # Fast path: valence mismatch
    val_a = fm_a.get("valence", "neutral")
    val_b = fm_b.get("valence", "neutral")
    if val_a != val_b:
        return False, f"valence mismatch ({val_a} vs {val_b})"

    # LLM judgment
    context = (
        f"Cell A (significance={fm_a.get('significance')}, valence={val_a}):\n"
        f"  topics: {fm_a.get('topics', [])}\n"
        f"  brief: {a['brief']}\n\n"
        f"Cell B (significance={fm_b.get('significance')}, valence={val_b}):\n"
        f"  topics: {fm_b.get('topics', [])}\n"
        f"  brief: {b['brief']}\n\n"
        f"Should these two adjacent cells be merged into one?"
    )
    try:
        result = _llm_call_with_retry(MERGE_SYSTEM, context, label="merge")
        return bool(result.get("should_merge", False)), result.get("reason", "")
    except json.JSONDecodeError:
        return False, "[parse error — defaulting to no merge]"


def can_merge(a: dict, b: dict) -> bool:
    """Check if two adjacent cells are merge candidates (semantic, LLM-based)."""
    should, _ = should_merge_llm(a, b)
    return should


def group_merge_candidates(cells: list[dict]) -> list[list[int]]:
    """Group adjacent mergeable cells. Returns list of index groups."""
    if len(cells) < 2:
        return []

    groups = []
    current = [0]

    for i in range(1, len(cells)):
        prev = cells[current[-1]]
        curr = cells[i]

        if can_merge(prev, curr):
            current.append(i)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [i]

    if len(current) >= 2:
        groups.append(current)

    return groups


# ── Preview mode ──────────────────────────────────────────────────────────────

def write_preview(cells: list[dict], groups: list[list[int]],
                  output_path: Path) -> None:
    """Write a proposal file for Mal to approve/reject."""
    lines = [
        "# Merge Proposals",
        "",
        f"Source: {cells[0]['path'].parent}",
        f"Total cells: {len(cells)}",
        f"Proposed merge groups: {len(groups)}",
        f"Cells in merge groups: {sum(len(g) for g in groups)}",
        f"Cells unchanged: {len(cells) - sum(len(g) for g in groups)}",
        "",
        "Mark each group with Y (approve) or N (reject) on the line provided.",
        "Unchanged cells are listed at the bottom for reference.",
        "",
        "---",
        "",
    ]

    for gi, group in enumerate(groups, 1):
        group_cells = [cells[i] for i in group]
        shared = shared_topics(
            group_cells[0]["frontmatter"].get("topics", []),
            group_cells[1]["frontmatter"].get("topics", []),
        )
        for c in group_cells[2:]:
            shared = [t for t in shared
                      if t.lower() in {x.lower() for x in c["frontmatter"].get("topics", [])}]

        lines.append(f"## Group {gi}  [ Y / N ]: _____")
        lines.append(f"Shared topics: {', '.join(shared)}")
        lines.append(f"Valence: {group_cells[0]['frontmatter'].get('valence', 'neutral')}")
        lines.append(f"Cells ({len(group)}):")
        lines.append("")
        for j, c in enumerate(group_cells):
            fm = c["frontmatter"]
            lines.append(f"  {j+1}. `{fm.get('cell_id')}` — {fm.get('topics')}")
            lines.append(f"     sig: {fm.get('significance')} | "
                         f"val: {fm.get('valence')} | "
                         f"novelty: {fm.get('novelty')}")
            lines.append(f"     brief: {c['brief'][:120]}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # Unchanged cells
    merged_idx = set(i for g in groups for i in g)
    unchanged = [(i, cells[i]) for i in range(len(cells)) if i not in merged_idx]
    lines.append("## Unchanged cells (not part of any merge group)")
    lines.append("")
    for i, c in unchanged:
        fm = c["frontmatter"]
        marker = "⭐ bright" if fm.get("significance") == "bright" else ""
        lines.append(f"- `{fm.get('cell_id')}` — {fm.get('topics')} {marker}")
        lines.append(f"  {c['brief'][:100]}")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Preview written: {output_path}")
    print(f"  {len(groups)} merge groups, {sum(len(g) for g in groups)} cells to merge, "
          f"{len(unchanged)} unchanged")


# ── Execute mode ───────────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """\
You are a memory cell writer for a companion AI system.

You receive a merged conversation excerpt (concatenated from multiple adjacent
cells with overlapping topics). Write a single unified memory cell summary
grounded ONLY in what this excerpt contains. Do not reference events outside.

Output fields:
  brief        — 1–2 sentences, the collapsed view (what Q sees scanning memory)
  episode      — 1 paragraph, enough context to decide whether to expand
  significance — "low" | "medium" | "high" | "bright"
                 bright = moment that should stay in active rotation
  valence      — "positive" | "negative" | "mixed" | "neutral"
  novelty      — "routine" | "notable" | "first-time"

Return ONLY valid JSON. No markdown fences. No prose. Start your response with {

{
  "brief": "...",
  "episode": "...",
  "significance": "high",
  "valence": "positive",
  "novelty": "notable"
}"""


def llm_summarize_merged(chunk_text: str, topics: list[str],
                          entities: list[str]) -> dict:
    """Re-summarize a merged chunk. Returns brief/episode/significance/valence/novelty."""
    context = (
        f"Topics: {', '.join(topics)}\n"
        f"Entities: {', '.join(entities)}\n\n"
        f"Transcript excerpt:\n\n{chunk_text[:MAX_MERGE_CHARS]}"
    )
    for attempt in range(3):
        raw = _llm_call(SUMMARY_SYSTEM, context, max_tokens=8000)
        try:
            return extract_json(raw)
        except json.JSONDecodeError as e:
            print(f"[merge] JSON parse error (attempt {attempt+1}/3): {e}",
                  file=sys.stderr)
            if attempt < 2:
                print(f"[merge] Raw output:\n{raw[:300]}", file=sys.stderr)
    return {
        "brief":        f"[merge parse error — topics: {', '.join(topics)}]",
        "episode":      "[merge summarization failed after retries]",
        "significance": "medium",
        "valence":      "neutral",
        "novelty":      "routine",
    }


def parse_approvals(proposal_path: Path) -> dict:
    """Parse the proposal file for Y/N marks. Returns {group_num: approved_bool}."""
    text = proposal_path.read_text(encoding="utf-8")
    approvals = {}
    for m in re.finditer(r"## Group (\d+)\s*\[ Y / N \]:\s*([YNyn])", text):
        gi = int(m.group(1))
        approved = m.group(2).upper() == "Y"
        approvals[gi] = approved
    return approvals


def execute_merge(cells: list[dict], groups: list[list[int]],
                  approvals: dict, run_dir: Path) -> None:
    """Collapse approved groups into single cells, archive originals."""
    archive = run_dir / "pre_merge_archive"
    archive.mkdir(exist_ok=True)

    sig_order = {"low": 0, "medium": 1, "high": 2, "bright": 3}

    merged_count = 0
    for gi, group in enumerate(groups, 1):
        if not approvals.get(gi, False):
            print(f"  Group {gi}: skipped (not approved)")
            continue

        group_cells = [cells[i] for i in group]

        # Merge metadata
        all_topics = []
        for c in group_cells:
            for t in c["frontmatter"].get("topics", []):
                if t not in all_topics:
                    all_topics.append(t)

        all_entities = []
        for c in group_cells:
            for e in c["frontmatter"].get("entities", []):
                if e not in all_entities:
                    all_entities.append(e)

        highest_sig = max(
            (c["frontmatter"].get("significance", "medium") for c in group_cells),
            key=lambda s: sig_order.get(s, 1)
        )

        # Merge chunk text (concatenate verbatim)
        merged_chunk = "\n\n".join(c["chunk"] for c in group_cells)

        # Re-summarize via LLM
        summary = llm_summarize_merged(merged_chunk, all_topics, all_entities)

        # Write new merged cell
        new_id = uuid.uuid4().hex[:8]
        base = group_cells[0]["frontmatter"]
        new_fm = {
            "cell_id":          new_id,
            "session_id":       base.get("session_id"),
            "session_date":     base.get("session_date"),
            "created":          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "temporal_status":  "fresh",
            "topics":           all_topics,
            "entities":         all_entities,
            "significance":     summary.get("significance", highest_sig),
            "valence":          summary.get("valence", base.get("valence", "neutral")),
            "novelty":          summary.get("novelty", "notable"),
            "referenced_count": 0,
            "last_referenced": None,
            "neighbors":        [],
            "quarantine":       True,
            "merged_from":      [c["frontmatter"].get("cell_id") for c in group_cells],
        }

        topic_slug = all_topics[0].replace(" ", "_")[:20] if all_topics else "merged"
        new_filename = f"{new_fm['session_date']}_{new_id}_{topic_slug}.md"
        new_path = run_dir / new_filename

        content = (
            "---\n"
            + yaml.dump(new_fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            f"## Brief\n{summary['brief']}\n\n"
            f"## Episode\n{summary['episode']}\n\n"
            f"## Chunk\n{merged_chunk}\n"
        )
        new_path.write_text(content, encoding="utf-8")

        # Archive originals
        for c in group_cells:
            c["path"].rename(archive / c["path"].name)

        print(f"  Group {gi}: merged {len(group)} cells → {new_filename}")
        print(f"    brief: {summary['brief'][:100]}")
        merged_count += 1

    print(f"\n✓ {merged_count} merge(s) executed. Originals in {archive}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Merge adjacent quarantine cells.")
    parser.add_argument("--preview",  metavar="DIR", help="Generate merge proposals")
    parser.add_argument("--execute",  metavar="FILE", help="Execute approved merges")
    parser.add_argument("--output",  metavar="FILE", help="Preview output path")
    args = parser.parse_args()

    if args.preview:
        run_dir = Path(args.preview)
        if not run_dir.is_dir():
            print(f"Error: {run_dir} is not a directory", file=sys.stderr)
            sys.exit(1)

        cells = load_run(run_dir)
        groups = group_merge_candidates(cells)

        if not groups:
            print("No merge candidates found.")
            return

        out = Path(args.output) if args.output else run_dir / "merge_proposals.md"
        write_preview(cells, groups, out)

    elif args.execute:
        proposal_path = Path(args.execute)
        if not proposal_path.is_file():
            print(f"Error: {proposal_path} not found", file=sys.stderr)
            sys.exit(1)

        run_dir = proposal_path.parent
        cells = load_run(run_dir)
        groups = group_merge_candidates(cells)
        approvals = parse_approvals(proposal_path)

        print(f"Approvals: {sum(1 for v in approvals.values() if v)}/"
              f"{len(approvals)} groups approved")
        execute_merge(cells, groups, approvals, run_dir)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
