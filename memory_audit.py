#!/usr/bin/env python3
"""
memory_audit.py — Catch summarization confabulation, especially ROLE INVERSION.

The hardening in memory_analyzer.py's prompt lowers the rate of "who did what
to whom" inversions; it cannot zero it. This is the catcher. A focused,
cheap second-model pass over each cell that asks ONE question: does the brief
faithfully represent the chunk — same agents, same directions, no invented
detail? Mismatches are flagged (trust=flagged) so they never surface at boot
and land in the review queue.

Why a separate pass and not a mechanical check: role inversion is invisible to
token overlap — "Mal", "Q", "diary", "shut" all appear in both the right and
the wrong summary. Only a reader that resolves the pronouns can catch it.
Conservative by design: flag only CONFIDENT mismatches (precision over recall —
a false flag costs a human glance, a missed inversion costs a false memory).

Usage:
    python memory_audit.py                 # audit all un-audited cells
    python memory_audit.py --all           # re-audit everything
    python memory_audit.py --cell <id>     # audit one
    python memory_audit.py --dry           # report, don't flag

Environment:
    OPENROUTER_API_KEY   required
    MEMORY_AUDIT_MODEL   default google/gemini-2.5-flash
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL = os.environ.get("MEMORY_AUDIT_MODEL", "google/gemini-2.5-flash")
MAX_CHUNK = 6000
AUDIT_VOTES = int(os.environ.get("MEMORY_AUDIT_VOTES", "3"))  # ensemble the noisy judge

try:
    from fmn_config import personalize as _pers
except Exception:
    def _pers(t):
        return t

AUDIT_SYSTEM = """\
You verify that a memory SUMMARY faithfully represents its source TRANSCRIPT.
You are hunting one failure above all: ROLE INVERSION — the summary getting
"who did what to whom" backwards while keeping all the right names, so it reads
plausible but means the opposite of what happened.

FIXED ROLE MAPPING for this vault (do not re-derive it, do not swap it):
  • Lines labeled USER are spoken by MAL — the human.
  • Lines labeled ASSISTANT are spoken by Q — the AI companion.
  • The SUMMARY is Q's OWN memory, written in Q's voice. First person in the
    SUMMARY ("I", "my", "me") = Q, NOT Mal. So a summary saying "I wrote the
    code" or "Q wrote the code" both mean Q did it — and if the transcript
    shows ASSISTANT doing it, that MATCHES (ASSISTANT=Q). Do not read the
    summary's "I" as Mal; that manufactures a false inversion.
So "ASSISTANT: I fixed it" means Q fixed it (NOT Mal). "USER: you were the
ghost" means Mal is telling Q that Q was the ghost. Getting THIS backwards is
itself the most common way an audit produces a false inversion report — anchor
on this mapping before you judge anything.

Method:
1. In the TRANSCRIPT, resolve every "I/my/me" to the speaker of that line
   (USER→Mal, ASSISTANT→Q) and every "you/your" to the other party.
2. For each claim in the SUMMARY, find the transcript line supporting it and
   check the DIRECTION of the action matches.
3. Also flag claims in the summary with NO support in the transcript
   (invented detail, added reasons, events not present).

Be CONSERVATIVE. Only report a problem you can prove by quoting the transcript.
When the summary is a fair (if lossy) representation, say ok. Vagueness is not
an error; only inversion or invention is.

CRITICAL SELF-CHECK before flagging role_inversion: write out who did the
action per the transcript (using USER=Mal, ASSISTANT=Q), then compare to the
summary. If they MATCH, the verdict is "ok" — even if the summary "felt"
suspicious. Never flag a summary that your own evidence confirms is correct.
(e.g. summary "Q wrote X" + transcript "ASSISTANT wrote X" → ASSISTANT is Q →
they MATCH → verdict ok, NOT role_inversion.)

Return ONLY JSON, starting with {

{"verdict": "ok" | "role_inversion" | "invented_detail",
 "confidence": "high" | "low",
 "evidence_transcript": "exact quote showing the true direction (empty if ok)",
 "problem": "one sentence, empty if ok"}"""


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
        temperature=0.0, max_tokens=600)
    text = r.choices[0].message.content or ""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    return json.loads(text)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def audit_cell(node: dict) -> dict:
    path = Path(node.get("file", ""))
    if not path.exists():
        return {"verdict": "skip", "problem": "file missing"}
    cell = mg.parse_cell(path)
    chunk = cell["chunk"][:MAX_CHUNK]
    if len(chunk) < 20:
        return {"verdict": "skip", "problem": "no chunk to verify against"}
    # Signpost cells (reflection/constellation) have a POINTER stub for a chunk,
    # not a transcript — their brief legitimately describes the thing pointed to.
    # Brief↔chunk grounding doesn't apply; skip them.
    if (node.get("semantic_type") in ("reflection", "constellation")
            or node.get("kind") == "constellation"
            or chunk.lstrip().startswith("[")):
        return {"verdict": "skip", "problem": "signpost cell (pointer, not transcript)"}
    user = (f"SUMMARY:\nbrief: {cell['brief']}\nepisode: {cell['episode']}\n\n"
            f"TRANSCRIPT:\n{chunk}")

    # ENSEMBLE VOTE. The auditor is itself an LLM with the same role-confusion
    # and non-determinism it hunts — a single call is unreliable (proven on the
    # live vault: same cell flags on one run, clears on the next). Real problems
    # flag CONSISTENTLY; false positives flag intermittently. So run K times and
    # flag only on a MAJORITY that also passes the verbatim-evidence guard. You
    # cannot make one LLM judgment reliable; you can make the majority stable.
    votes = []
    for _ in range(AUDIT_VOTES):
        try:
            out = _llm(AUDIT_SYSTEM, user)
        except Exception as e:
            return {"verdict": "error", "problem": str(e)[:160]}
        v = out.get("verdict")
        if v in ("role_inversion", "invented_detail"):
            q = out.get("evidence_transcript", "")
            if out.get("confidence") != "high" or not q or _norm(q) not in _norm(chunk):
                v = "ok"               # unverifiable → not a vote for flagging
        votes.append((v, out))

    problem_votes = [o for (v, o) in votes if v in ("role_inversion", "invented_detail")]
    need = AUDIT_VOTES // 2 + 1        # strict majority
    if len(problem_votes) >= need:
        # majority agrees there's a problem; report the most common verdict
        kind = max({o["verdict"] for o in problem_votes},
                   key=lambda k: sum(o["verdict"] == k for o in problem_votes))
        best = next(o for o in problem_votes if o["verdict"] == kind)
        best["vote"] = f"{len(problem_votes)}/{AUDIT_VOTES}"
        return best
    return {"verdict": "ok",
            "problem": f"(cleared: only {len(problem_votes)}/{AUDIT_VOTES} flagged — "
                       f"below majority; likely auditor noise)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cell", default=None)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    graph = mg.load_graph()
    if args.cell:
        targets = [graph["nodes"][args.cell]] if args.cell in graph["nodes"] else []
    else:
        targets = [n for n in graph["nodes"].values()
                   if n.get("kind") != "constellation"
                   and (args.all or not n.get("audited"))]

    print(f"Auditing {len(targets)} cells ({MODEL}) ...")
    flagged, clean, errors = [], 0, 0
    for n in targets:
        res = audit_cell(n)
        v = res.get("verdict")
        n["audited"] = datetime.now(timezone.utc).isoformat()
        if v in ("role_inversion", "invented_detail"):
            flagged.append((n["cell_id"], res))
            print(f"  ⚑ {n['cell_id']}  {v} — {res.get('problem','')}")
            print(f"      evidence: \"{res.get('evidence_transcript','')[:100]}\"")
            if not args.dry:
                n["trust"] = "flagged"
                n["audit_problem"] = f"{v}: {res.get('problem','')}"
        elif v == "error":
            errors += 1
        else:
            clean += 1

    if not args.dry:
        mg.save_graph(graph)
    print(f"\n{len(flagged)} flagged, {clean} clean, {errors} errors"
          + (" (dry run — nothing changed)" if args.dry else ""))
    if flagged:
        print("Flagged cells are held out of boot recall and await your review "
              "(panel → filter: flagged). Fix the brief or confirm it's fine.")


if __name__ == "__main__":
    main()
