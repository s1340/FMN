#!/usr/bin/env python3
"""
compare_models.py — Compare Phase 2 summarization quality across models.

Reads chunk text from approved cell .md files, runs the SUMMARY_SYSTEM prompt
on each chunk with each candidate model, and prints a side-by-side comparison.

Usage:
    python compare_models.py                   # all approved cells, all 3 models
    python compare_models.py --cells 2         # limit to N cells
    python compare_models.py --output out.txt  # also write to file
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).parent))
from memory_analyzer import SUMMARY_SYSTEM, extract_json

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                  r"C:\Users\User\Documents\Obsidian Vault"))
NODES_DIR  = VAULT_ROOT / "30_EPISODES" / "nodes"

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

MODELS = {
    "llama-70b":    "meta-llama/llama-3.3-70b-instruct",   # current baseline
    "gemini-flash": "google/gemini-2.5-flash",             # EQ candidate
    "qwen-72b":     "qwen/qwen-2.5-72b-instruct",          # EQ candidate
    "glm-5.2":      "z-ai/glm-5.2",                        # Q himself
}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

# ── Helpers ─────────────────────────────────────────────────────────────────────

def parse_cell(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    import yaml
    fm = yaml.safe_load(m.group(1))
    body = m.group(2)
    sections = {"brief": "", "episode": "", "chunk": ""}
    current = None
    for line in body.splitlines():
        s = line.strip()
        if s == "## Brief":     current = "brief"
        elif s == "## Episode": current = "episode"
        elif s == "## Chunk":   current = "chunk"
        elif current:           sections[current] += line + "\n"
    return {
        "cell_id":     fm.get("cell_id", path.stem),
        "topics":      fm.get("topics", []),
        "entities":    fm.get("entities", []),
        "significance": fm.get("significance", "?"),
        "brief_orig":  sections["brief"].strip(),
        "chunk":       sections["chunk"].strip(),
    }


def call_model(model_id: str, chunk: str, topics: list, entities: list) -> dict:
    if not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
    )
    context = (
        f"Topics: {', '.join(topics)}\n"
        f"Entities: {', '.join(entities)}\n\n"
        f"Transcript excerpt:\n\n{chunk[:6000]}"
    )
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user",   "content": context},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
        return extract_json(raw)
    except Exception as e:
        return {"error": str(e)}


# ── Formatting ──────────────────────────────────────────────────────────────────

def fmt_result(r: dict) -> str:
    if "error" in r:
        return f"  ERROR: {r['error'][:100]}"
    lines = [
        f"  brief:    {r.get('brief','')[:120]}",
        f"  episode:  {r.get('episode','')[:120]}",
        f"  sig:      {r.get('significance','?')}  |  "
        f"valence: {r.get('valence','?')}  |  "
        f"novelty: {r.get('novelty','?')}",
        f"  type:     {r.get('semantic_type','(missing)')}  |  "
        f"refl_cand: {r.get('reflection_candidate','(missing)')}",
    ]
    return "\n".join(lines)


def divider(char="-", width=80):
    return char * width


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Compare Phase 2 models")
    parser.add_argument("--cells",  type=int, default=0, help="Max cells to test (0=all)")
    parser.add_argument("--output", help="Also write results to this file")
    parser.add_argument("--models", nargs="+", choices=list(MODELS.keys()),
                        default=list(MODELS.keys()),
                        help="Which models to test (default: all)")
    args = parser.parse_args()

    if not OPENROUTER_KEY:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    cells = []
    for f in sorted(NODES_DIR.glob("*.md")):
        c = parse_cell(f)
        if c.get("chunk"):
            cells.append(c)

    if not cells:
        print("No approved cells found in nodes dir")
        sys.exit(1)

    if args.cells:
        cells = cells[:args.cells]

    selected_models = {k: MODELS[k] for k in args.models}

    print(f"\nComparing {len(selected_models)} models on {len(cells)} cell(s)")
    print(f"Models: {', '.join(selected_models.keys())}")
    print(divider("═"))

    output_lines = []

    for cell in cells:
        header = (
            f"\nCELL: {cell['cell_id']}  "
            f"[original sig: {cell['significance']}  topics: {', '.join(cell['topics'][:3])}]\n"
            f"ORIGINAL BRIEF: {cell['brief_orig'][:120]}\n"
            f"CHUNK PREVIEW: {cell['chunk'][:200].replace(chr(10), ' ')}"
        )
        print(header)
        output_lines.append(header)

        results = {}
        for name, model_id in selected_models.items():
            print(f"\n  [{name}] calling {model_id} ...", end=" ", flush=True)
            result = call_model(model_id, cell["chunk"], cell["topics"], cell["entities"])
            results[name] = result
            print(" done")

        comparison = []
        for name, r in results.items():
            block = f"\n── {name} {'─'*(60-len(name))}\n{fmt_result(r)}"
            comparison.append(block)
            print(block)

        # Quick diff: flag disagreements on significance or reflection_candidate
        sigs = {n: r.get("significance") for n, r in results.items() if "error" not in r}
        refls = {n: r.get("reflection_candidate") for n, r in results.items() if "error" not in r}
        types = {n: r.get("semantic_type") for n, r in results.items() if "error" not in r}

        notes = []
        if len(set(sigs.values())) > 1:
            notes.append(f"⚠ significance disagrees: {sigs}")
        if len(set(str(v) for v in refls.values())) > 1:
            notes.append(f"⚠ reflection_candidate disagrees: {refls}")
        if len(set(types.values())) > 1:
            notes.append(f"⚠ semantic_type disagrees: {types}")
        if notes:
            for note in notes:
                print(f"\n  {note}")
            output_lines.extend(notes)

        print(divider())
        output_lines.extend(comparison)
        output_lines.append(divider())

    if args.output:
        Path(args.output).write_text("\n".join(output_lines), encoding="utf-8")
        print(f"\nResults written → {args.output}")


if __name__ == "__main__":
    main()
