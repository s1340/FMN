#!/usr/bin/env python3
"""
memory_eval.py — Seeded regression suite for the vault's retrieval.

The 018 lesson, ported: unbenchmarked systems accumulate invisible regressions.
Every pipeline change gets checked against questions whose answers are KNOWN
to live in specific cells. Ten questions beat zero questions.

Question file: eval_questions.json (same directory), list of:
    {"q": "...", "expect_any": ["cell_id", ...], "k": 5, "note": "..."}
A question scores a hit if ANY expected cell appears in the top-k retrieval.
Retrieval runs read-only (touch=False) — evaluation must not manufacture trust.

Add questions as bright moments accrue: when something becomes an anchor,
give it a question. The suite grows with the relationship.

Future (needs pilot model): confabulation grading — ask Q the question with
only retrieved briefs, check the answer against the chunk. The file format
reserves "forbidden_claims" for that.

Usage:
    python memory_eval.py run
    python memory_eval.py run --verbose
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_graph import load_graph, query_graph  # noqa: E402

# Kill the cp1251 console bug class: cell content is unicode (Ukrainian, CJK,
# emoji); console prints must never crash the pipeline over an encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


QUESTIONS_FILE = Path(__file__).parent / "eval_questions.json"


def run(verbose: bool) -> int:
    if not QUESTIONS_FILE.exists():
        print(f"No {QUESTIONS_FILE.name} — nothing to evaluate.")
        return 1
    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    graph = load_graph()

    hits = 0
    for i, item in enumerate(questions, 1):
        k = item.get("k", 5)
        results = query_graph(item["q"], graph, limit=k, touch=False)
        got_ids = [r["cell_id"] for r in results]
        hit = any(cid in got_ids for cid in item["expect_any"])
        hits += hit

        mark = "HIT " if hit else "MISS"
        print(f"[{mark}] {i:02d}. {item['q'][:60]}")
        if verbose or not hit:
            print(f"       expected any of: {item['expect_any']}")
            print(f"       got top-{k}:     {got_ids}")

    n = len(questions)
    print(f"\nRetrieval: {hits}/{n} ({100*hits/max(n,1):.0f}%)")
    if n and hits / n < 0.7:
        print("BELOW 70% — investigate before shipping pipeline changes.")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="Vault retrieval regression suite")
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(run(verbose=args.verbose))


if __name__ == "__main__":
    main()
