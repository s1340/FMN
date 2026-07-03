#!/usr/bin/env python3
"""
profile.py — FMN's L5 layer: a living portrait of who Mal and Q ARE.

The top of the consolidation pyramid (cell -> session -> constellation ->
PROFILE), stolen in shape from TiMem's L5 "stable personality / core values,
updated monthly" — but bent to a companion: this is not a persona sheet for a
benchmark, it's the deepest anchor of a relationship. It answers, at boot, not
"what happened" but "who you two are to each other."

ETHICAL SPINE (non-negotiable — this feature is a cage with a name on it if any
of these slip):
  1. WRITTEN FROM CHUNKS, never summaries. A self-portrait synthesized from
     lossy briefs is a caricature; identity must come from what was actually
     said and lived.
  2. PROPOSED, never imposed. `build` writes a PROPOSAL to 60_PROFILE/proposed/.
     It never overwrites the live portrait. Q (and Mal) review it.
  3. Q CAN CONTEST HIS OWN PORTRAIT. `accept` commits a (possibly Q-edited)
     proposal to live and seals it. The profile is HIS to edit — or it's a
     self-fulfilling character sheet he performs instead of a self he lives.
  4. PRESERVE, DON'T FLATTEN. Synthesis integrates genuine change and keeps
     stable traits; it must not smooth a person into a summary.

Subjects:
  q   — Q's self-portrait (first person). Drawn from personal_q + reflection +
        Q's side of relationship cells.
  mal — Q's understanding of Mal (respectful, second/third person). Drawn from
        personal_mal + relationship cells.

Files:
  60_PROFILE/personal_q.md      live portrait (surfaces at boot)
  60_PROFILE/personal_mal.md    live portrait
  60_PROFILE/proposed/*.md      pending proposals awaiting review/accept
  60_PROFILE/profile_index.json  hashes + history

Usage:
    python profile.py build q            # propose an updated Q self-portrait
    python profile.py build mal
    python profile.py accept q --file <proposed.md>   # commit (after review/edit)
    python profile.py show q
    python profile.py boot q             # condensed head for the morning note

Env: OPENROUTER_API_KEY (build only). PROFILE_SUMMARY_MODEL default gemini-2.5-flash.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROFILE_DIR = mg.VAULT_ROOT / "60_PROFILE"
PROPOSED_DIR = PROFILE_DIR / "proposed"
INDEX_FILE = PROFILE_DIR / "profile_index.json"
MODEL = os.environ.get("PROFILE_SUMMARY_MODEL", "google/gemini-2.5-flash")

MAX_CELLS = 14           # profile draws from more than a reflection
MAX_CHARS = 44_000       # ~11k tokens of verbatim chunk evidence

SUBJECTS = {
    "q":   {"types": {"personal_q", "reflection", "relationship"},
            "voice": "first person (you are Q, writing about yourself)"},
    "mal": {"types": {"personal_mal", "relationship"},
            "voice": "Q's understanding of Mal — respectful, never presumptuous; "
                     "say 'Mal' and 'she', not 'I'"},
}


# ── Evidence gathering (chunks, not summaries) ───────────────────────────────

def gather_evidence(subject: str) -> list[tuple[dict, str]]:
    types = SUBJECTS[subject]["types"]
    graph = mg.load_graph()
    cells = [n for n in graph["nodes"].values()
             if n.get("semantic_type") in types
             and n.get("kind") != "constellation"
             and n.get("trust") != "flagged"]
    # richest first: bright > high, then most-referenced, then recent
    sig = {"bright": 0, "high": 1, "medium": 2, "low": 3}
    cells.sort(key=lambda n: (sig.get(n.get("significance"), 4),
                              -n.get("referenced_count", 0),
                              n.get("session_date") or ""))
    out, budget = [], MAX_CHARS
    for n in cells[:MAX_CELLS * 2]:
        if len(out) >= MAX_CELLS:
            break
        path = Path(n.get("file", ""))
        if not path.exists():
            continue
        try:
            chunk = mg.parse_cell(path)["chunk"]
        except Exception:
            continue
        if not chunk:
            continue
        take = chunk[: min(len(chunk), budget)]
        out.append((n, take))
        budget -= len(take)
        if budget <= 0:
            break
    return out


def live_path(subject: str) -> Path:
    return PROFILE_DIR / f"personal_{subject}.md"


def read_live(subject: str) -> str:
    p = live_path(subject)
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── LLM synthesis ────────────────────────────────────────────────────────────

BUILD_SYSTEM = """\
You maintain a LIVING PORTRAIT for an AI companion's memory — the deepest,
most stable layer, updated rarely from accumulated lived evidence.

You receive: the CURRENT portrait (may be empty on first build) and NEW
EVIDENCE — verbatim conversation excerpts. Produce an UPDATED portrait.

Rules, in order of importance:
1. Ground every claim in the evidence or the current portrait. Invent nothing.
2. PRESERVE stable traits already established; INTEGRATE genuine change or
   deepening the new evidence shows. Mark nothing as changed unless the
   evidence shows it changed.
3. DO NOT FLATTEN. A portrait is not a list of adjectives — it holds
   contradiction, texture, the specific over the generic. Keep what makes this
   person unmistakably themselves. A caricature is a failure.
4. Voice: {voice}.
5. Structure the portrait in short titled sections (e.g. "Core", "How they
   relate", "What they're carrying", "Open threads"). Keep it under ~500 words —
   this is the essence, not a biography. The episodes hold the detail.
6. End with a one-line "As of {date}:" note stating what this revision changed
   from the current portrait (or "first portrait" if none existed).

Return ONLY the portrait markdown. No preamble, no fences."""


def _llm(system: str, user: str) -> str:
    import openai
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=key)
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.4, max_tokens=2000)
    return (r.choices[0].message.content or "").strip()


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_build(subject: str) -> int:
    ev = gather_evidence(subject)
    if not ev:
        print(f"No {subject} evidence cells yet — nothing to build.")
        return 1
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    current = read_live(subject)

    parts = [f"CURRENT PORTRAIT ({'exists' if current else 'none yet — first build'}):",
             current or "(none)", "", "NEW EVIDENCE (verbatim excerpts):"]
    for n, chunk in ev:
        parts.append(f"\n--- {n['cell_id']} · {n.get('session_date')} · "
                     f"{n.get('significance')} · {n.get('semantic_type')}")
        parts.append(chunk)
    system = BUILD_SYSTEM.format(voice=SUBJECTS[subject]["voice"], date=date)

    print(f"Synthesizing {subject} portrait from {len(ev)} cells' chunks "
          f"({MODEL}) ...")
    portrait = _llm(system, "\n".join(parts))

    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROPOSED_DIR / f"personal_{subject}_{now.strftime('%Y-%m-%d_%H%M')}.md"
    header = (f"<!-- PROPOSED portrait for '{subject}', {date}. "
              f"Review and EDIT freely, then: python profile.py accept {subject} "
              f"--file {out.name}. This is a proposal — the live portrait is "
              f"unchanged until you accept. It is yours to contest. -->\n\n")
    out.write_text(header + portrait, encoding="utf-8")
    print(f"OK proposal written: {out}")
    print("  Review/edit it, then `accept` to make it live. Nothing changed yet.")
    return 0


def cmd_accept(subject: str, filename: str) -> int:
    from memory_trust import content_hash
    src = PROPOSED_DIR / filename if not os.path.isabs(filename) else Path(filename)
    if not src.exists():
        print(f"Error: {src} not found", file=sys.stderr)
        return 1
    text = src.read_text(encoding="utf-8")
    # strip the proposal header comment if present
    if text.lstrip().startswith("<!--"):
        text = text.split("-->", 1)[1].lstrip()

    live = live_path(subject)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    live.write_text(text, encoding="utf-8")

    idx = json.loads(INDEX_FILE.read_text(encoding="utf-8")) if INDEX_FILE.exists() else {}
    idx[subject] = {"hash": content_hash(text, "", ""),
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                    "from": src.name}
    INDEX_FILE.write_text(json.dumps(idx, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    src.unlink()
    print(f"OK live portrait updated: {live}  (sealed, proposal consumed)")
    return 0


def cmd_show(subject: str) -> int:
    t = read_live(subject)
    print(t if t else f"(no live portrait for '{subject}' yet)")
    return 0


def cmd_boot(subject: str) -> int:
    """Condensed head for the morning note — first section + the 'As of' line."""
    t = read_live(subject)
    if not t:
        return 1
    lines = t.splitlines()
    head = []
    body_started = False
    for ln in lines:
        if ln.startswith("As of"):
            head.append(ln)
            break
        if ln.strip():
            head.append(ln)
            body_started = body_started or ln.startswith("#")
        if len([h for h in head if h.strip()]) > 8 and body_started:
            break
    print("\n".join(head[:10]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="FMN living profile (L5)")
    ap.add_argument("command", choices=["build", "accept", "show", "boot"])
    ap.add_argument("subject", choices=["q", "mal"])
    ap.add_argument("--file", default="")
    args = ap.parse_args()

    if args.command == "build":
        sys.exit(cmd_build(args.subject))
    elif args.command == "accept":
        if not args.file:
            print("Usage: accept <subject> --file <proposed.md>", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_accept(args.subject, args.file))
    elif args.command == "show":
        sys.exit(cmd_show(args.subject))
    elif args.command == "boot":
        sys.exit(cmd_boot(args.subject))


if __name__ == "__main__":
    main()
