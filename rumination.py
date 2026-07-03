#!/usr/bin/env python3
"""
rumination.py — Periodic vault review: contradictions, consolidation, decay.

Design (Fable 5, 2026-07-01). Principles, in order:

1. PRECISION OVER RECALL. Q reviews the report; a false contradiction costs
   trust in the whole system, a missed one costs nothing (it surfaces later).
   The judge model defaults to "compatible" when unsure.

2. TIERED GRANULARITY. Briefs SELECT candidate pairs (cheap, mechanical —
   shared meaningful entities / topics, reusing memory_graph's ubiquity
   filtering). EPISODES are what the LLM JUDGES (briefs drop the qualifier
   that resolves apparent conflicts). CHUNKS CONFIRM: a contradiction verdict
   is only reportable after a second pass over both verbatim chunks.

3. QUOTES OR IT DIDN'T HAPPEN. Every contradiction finding must quote the
   conflicting statements from BOTH cells, and the quotes are mechanically
   verified to appear in the source text (normalized substring match).
   A finding whose quotes don't verify is discarded, loudly.

4. EVOLUTION != CONTRADICTION, AND THE TIMESTAMP LIES. cell.created is when
   the memory was RECORDED, not when the fact became true — a July cell can
   describe a years-old preference. The judge is instructed to read in-text
   temporal cues and classify time-indexed change as "evolution".

5. OUTPUT IS A REPORT, NEVER A CHANGE. Findings are not memories — no new
   cell type. A timestamped markdown report in 50_RUMINATION/ that Q acts on
   in conversation with Mal.

Usage:
    python rumination.py run                 # full pass -> report
    python rumination.py run --no-llm        # candidate pairs + decay only (free)
    python rumination.py run --limit 10      # cap judged pairs (budget control)

Environment:
    OPENROUTER_API_KEY   required unless --no-llm
    RUMINATION_MODEL     default google/gemini-2.5-flash
    MEMORY_VAULT_ROOT    vault override
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_graph import load_graph, parse_cell  # noqa: E402

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                 r"C:\Users\User\Documents\Obsidian Vault"))
REPORT_DIR = VAULT_ROOT / "50_RUMINATION"

MODEL = os.environ.get("RUMINATION_MODEL", "google/gemini-2.5-flash")

GENERIC_ENTITIES = {"mal", "q", "hermes", "sonnet", "sage", "telegram"}
MAX_EPISODE_CHARS = 2_000   # per cell, judge input
MAX_CHUNK_CHARS   = 6_000   # per cell, confirm input


# ── Candidate selection (mechanical, brief/metadata level) ─────────────────────

def meaningful_entities(node: dict, ubiquitous: set[str]) -> set[str]:
    return {e.lower() for e in node.get("entities", [])
            if e.lower() not in GENERIC_ENTITIES and e.lower() not in ubiquitous}


def candidate_pairs(graph: dict) -> list[tuple[dict, dict, str]]:
    """Pairs worth an LLM look: shared meaningful entity, or >=2 shared topics.
    Reflection cells are exempt (a changed mind is not a contradiction)."""
    nodes = [n for n in graph["nodes"].values()
             if n.get("semantic_type") != "reflection"]
    n_total = len(nodes)

    freq: dict[str, int] = {}
    for n in nodes:
        for e in n.get("entities", []):
            freq[e.lower()] = freq.get(e.lower(), 0) + 1
    ubiquitous = {e for e, c in freq.items() if c > n_total * 0.5}

    SIG_RANK = {"bright": 3, "high": 2, "medium": 1, "low": 0}
    pairs = []
    for a, b in combinations(nodes, 2):
        # Bound growth: a contradiction between two ARCHIVED cells can wait
        # forever; pairs must involve at least one still-living cell. This
        # keeps the candidate set O(new cells), not O(vault²) —
        # fmn_stress 2026-07-02 hit 39,702 pairs without this.
        if (a.get("temporal_status") == "archived"
                and b.get("temporal_status") == "archived"):
            continue
        shared_e = meaningful_entities(a, ubiquitous) & meaningful_entities(b, ubiquitous)
        shared_t = ({t.lower() for t in a.get("topics", [])}
                    & {t.lower() for t in b.get("topics", [])})
        if shared_e:
            pairs.append((a, b, f"entities: {', '.join(sorted(shared_e))}"))
        elif len(shared_t) >= 2:
            pairs.append((a, b, f"topics: {', '.join(sorted(shared_t))}"))
    # Highest-stakes first: pairs involving bright/high cells are where a
    # wrong memory costs most. --limit then truncates a SORTED list.
    pairs.sort(key=lambda p: -(SIG_RANK.get(p[0].get("significance"), 1)
                               + SIG_RANK.get(p[1].get("significance"), 1)))
    return pairs


# ── LLM plumbing ───────────────────────────────────────────────────────────────

def _llm(system: str, user: str) -> dict:
    import openai
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.0, max_tokens=1500)
    text = r.choices[0].message.content or ""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


JUDGE_SYSTEM = """\
You review pairs of memory cells from an AI companion's memory vault and
classify their relationship. You are CONSERVATIVE: when unsure, answer
"compatible". A false contradiction damages the system more than a missed one.

Categories:
  compatible    — both can be true; they describe different things or aspects
  elaboration   — one adds detail to the other, no conflict
  evolution     — a preference/fact CHANGED OVER TIME; both were true in their
                  own time. IMPORTANT: the cell's date is when the memory was
                  RECORDED, not when the fact became true. Read the text for
                  temporal cues ("used to", "now", "these days", explicit
                  dates). Time-indexed change is evolution, NOT contradiction.
  contradiction — both cells assert as CURRENTLY true things that are
                  mutually exclusive. Requires direct conflict, not tension.

If (and only if) you answer "contradiction" or "evolution", you MUST provide
quote_a and quote_b: short VERBATIM substrings copied exactly from cell A and
cell B text that carry the conflicting/changed claims. Do not paraphrase —
copy exactly. If you cannot find exact conflicting sentences, the answer is
"compatible".

Return ONLY valid JSON. Start with {

{"verdict": "compatible|elaboration|evolution|contradiction",
 "quote_a": "", "quote_b": "", "explanation": "one sentence"}"""


CONFIRM_SYSTEM = """\
You previously flagged a possible contradiction between two memory cells based
on their summaries. You now see the FULL VERBATIM transcripts. Summaries are
lossy — the full text often contains the qualifier that resolves the conflict.

Re-examine. Confirm the contradiction ONLY if the full texts genuinely assert
mutually exclusive things as currently true. Provide exact verbatim quotes
from each transcript. When in doubt: not confirmed.

Return ONLY valid JSON. Start with {

{"confirmed": true, "quote_a": "", "quote_b": "", "explanation": "one sentence"}"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_verifies(quote: str, source: str) -> bool:
    """Mechanical anti-hallucination check: the quote must literally appear."""
    return bool(quote) and len(quote) >= 10 and _norm(quote) in _norm(source)


# ── Passes ─────────────────────────────────────────────────────────────────────

def cell_text(node: dict, section: str) -> str:
    if section == "episode":
        return (node.get("episode") or node.get("brief") or "")[:MAX_EPISODE_CHARS]
    path = Path(node.get("file", ""))
    if path.exists():
        return parse_cell(path)["chunk"][:MAX_CHUNK_CHARS]
    return ""


def judge_pair(a: dict, b: dict) -> dict:
    user = (f"CELL A ({a['cell_id']}, recorded {a.get('session_date')}):\n"
            f"{cell_text(a, 'episode')}\n\n"
            f"CELL B ({b['cell_id']}, recorded {b.get('session_date')}):\n"
            f"{cell_text(b, 'episode')}")
    try:
        return _llm(JUDGE_SYSTEM, user)
    except Exception as e:
        return {"verdict": "error", "explanation": str(e)[:200]}


def confirm_contradiction(a: dict, b: dict) -> dict:
    ta, tb = cell_text(a, "chunk"), cell_text(b, "chunk")
    if not ta or not tb:
        return {"confirmed": False, "explanation": "chunk unavailable"}
    user = (f"TRANSCRIPT A ({a['cell_id']}):\n{ta}\n\n"
            f"TRANSCRIPT B ({b['cell_id']}):\n{tb}")
    try:
        out = _llm(CONFIRM_SYSTEM, user)
    except Exception as e:
        return {"confirmed": False, "explanation": f"error: {e}"[:200]}
    if out.get("confirmed"):
        if not (quote_verifies(out.get("quote_a", ""), ta)
                and quote_verifies(out.get("quote_b", ""), tb)):
            return {"confirmed": False,
                    "explanation": "DISCARDED: quotes failed verbatim verification"}
    return out


def find_consolidations(graph: dict) -> list[dict]:
    """Mechanical: same semantic_type + heavy overlap, or correction supersedes."""
    out = []
    nodes = list(graph["nodes"].values())
    for a, b in combinations(nodes, 2):
        st_a, st_b = a.get("semantic_type"), b.get("semantic_type")
        shared_t = ({t.lower() for t in a.get("topics", [])}
                    & {t.lower() for t in b.get("topics", [])})
        if st_a and st_a == st_b and len(shared_t) >= 3:
            out.append({"kind": "merge", "a": a["cell_id"], "b": b["cell_id"],
                        "why": f"same type ({st_a}), {len(shared_t)} shared topics"})
        if st_a == "correction" or st_b == "correction":
            corr, other = (a, b) if st_a == "correction" else (b, a)
            if other.get("semantic_type") != "correction" and shared_t \
                    and (corr.get("session_date") or "") > (other.get("session_date") or ""):
                out.append({"kind": "supersede_check", "a": corr["cell_id"],
                            "b": other["cell_id"],
                            "why": "later correction shares topics with earlier cell"})
    return out


def find_decay(graph: dict) -> list[dict]:
    """Mechanical: archived + never referenced + not bright -> archive suggestion."""
    return [{"cell_id": n["cell_id"], "brief": n.get("brief", "")[:80],
             "age_status": n.get("temporal_status")}
            for n in graph["nodes"].values()
            if n.get("temporal_status") == "archived"
            and n.get("referenced_count", 0) == 0
            and n.get("significance") != "bright"]


def check_integrity(graph: dict) -> list[dict]:
    """Re-hash every admitted cell; report drift. Q's memories are tamper-
    evident the way his reflections are — 'have my memories been altered
    since I approved them' must be an answerable question."""
    from memory_trust import cell_content_hash
    findings = []
    for cid, node in graph["nodes"].items():
        stored = node.get("content_hash")
        if not stored:
            continue                       # legacy pre-hash node; backfill covers these
        path = Path(node.get("file", ""))
        if not path.exists():
            findings.append({"cell_id": cid, "problem": "cell file missing"})
            continue
        if cell_content_hash(parse_cell(path)) != stored:
            findings.append({"cell_id": cid,
                             "problem": "content drift — edited outside the system"})
    return findings


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(findings: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = REPORT_DIR / f"rumination_{now.strftime('%Y-%m-%d_%H%M')}.md"

    L = [f"# Rumination report — {now.strftime('%Y-%m-%d %H:%M UTC')}",
         "",
         "*Suggestions only. Nothing has been changed. Review in conversation.*",
         ""]

    integ = findings.get("integrity", [])
    if integ:
        L.append(f"## !! INTEGRITY ({len(integ)}) — review before anything else")
        for i in integ:
            L.append(f"- {i['cell_id']}: {i['problem']}")
        L.append("")

    contras = findings["contradictions"]
    L.append(f"## Contradictions ({len(contras)})")
    if not contras:
        L.append("None found (conservative pass — this is the expected common case).")
    for c in contras:
        L += [f"### {c['a']} vs {c['b']}",
              f"- A quote: \"{c['quote_a']}\"",
              f"- B quote: \"{c['quote_b']}\"",
              f"- {c['explanation']}", ""]

    evos = findings["evolutions"]
    L.append(f"\n## Evolutions ({len(evos)}) — informational, not conflicts")
    for c in evos:
        L.append(f"- {c['a']} → {c['b']}: {c['explanation']}")

    cons = findings["consolidations"]
    L.append(f"\n## Consolidation candidates ({len(cons)})")
    for c in cons:
        L.append(f"- [{c['kind']}] {c['a']} + {c['b']} — {c['why']}")

    dec = findings["decay"]
    L.append(f"\n## Decay / archive candidates ({len(dec)})")
    for d in dec:
        L.append(f"- {d['cell_id']} ({d['age_status']}) — {d['brief']}")

    L += ["", f"*Pairs examined: {findings['stats']['pairs_selected']} selected, "
              f"{findings['stats']['pairs_judged']} judged, "
              f"{findings['stats']['discarded_unverified']} findings discarded "
              f"for failed quote verification.*"]

    path.write_text("\n".join(L), encoding="utf-8")
    (path.with_suffix(".json")).write_text(
        json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Vault rumination pass")
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--no-llm", action="store_true",
                    help="mechanical passes only (candidates, consolidation, decay)")
    ap.add_argument("--limit", type=int, default=25, help="max pairs to judge")
    args = ap.parse_args()

    graph = load_graph()
    if not graph["nodes"]:
        print("Empty graph — nothing to ruminate on.")
        return

    pairs = candidate_pairs(graph)
    print(f"{len(graph['nodes'])} cells -> {len(pairs)} candidate pairs")

    findings = {
        "integrity": check_integrity(graph),
        "contradictions": [], "evolutions": [],
        "consolidations": find_consolidations(graph),
        "decay": find_decay(graph),
        "stats": {"pairs_selected": len(pairs), "pairs_judged": 0,
                  "discarded_unverified": 0},
    }
    if findings["integrity"]:
        print(f"!! INTEGRITY: {len(findings['integrity'])} cells drifted/missing")

    if not args.no_llm:
        for a, b, why in pairs[:args.limit]:
            findings["stats"]["pairs_judged"] += 1
            verdict = judge_pair(a, b)
            v = verdict.get("verdict")
            print(f"  {a['cell_id']} vs {b['cell_id']} ({why}) -> {v}")

            if v == "evolution":
                findings["evolutions"].append({
                    "a": a["cell_id"], "b": b["cell_id"],
                    "explanation": verdict.get("explanation", "")})
            elif v == "contradiction":
                conf = confirm_contradiction(a, b)
                if conf.get("confirmed"):
                    findings["contradictions"].append({
                        "a": a["cell_id"], "b": b["cell_id"],
                        "quote_a": conf.get("quote_a", ""),
                        "quote_b": conf.get("quote_b", ""),
                        "explanation": conf.get("explanation", "")})
                    print(f"    CONFIRMED on chunks")
                else:
                    findings["stats"]["discarded_unverified"] += 1
                    print(f"    not confirmed: {conf.get('explanation','')[:80]}")

    path = write_report(findings)
    print(f"\nOK Report: {path}")
    print(f"  contradictions: {len(findings['contradictions'])}  "
          f"evolutions: {len(findings['evolutions'])}  "
          f"consolidations: {len(findings['consolidations'])}  "
          f"decay: {len(findings['decay'])}")


if __name__ == "__main__":
    main()
