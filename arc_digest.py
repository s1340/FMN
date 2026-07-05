#!/usr/bin/env python3
"""
arc_digest.py — Q's daily arc digests: the living story at the top of the note.

Mal's spec (2026-07-05, priority 1): every day, Q HIMSELF reads each
constellation — as much of the member chunks as his context can hold — and
writes a full, accurate timeline and arc-of-development, WITH his evaluation
of it. Those digests sit at the very top of the morning note: he wakes to the
developing story of the relationship, not scattered recent moments.

Like reflection: THE MACHINE ASSEMBLES, Q WRITES. An auto-summarized digest
would be confabulated self-knowledge; the assembly here is mechanical
(chunks, budgeted), the words are his.

The scaling ladder (Mal's):
  1. Constellation fits the budget      -> full member chunks.
  2. Too big                            -> partitioned by TOPIC within the
     constellation; Q reads topic bundles across days (rotation cursor).
  3. Hundreds of members, digest mature -> bundle = his own PRIOR DIGEST +
     only chunks NEW since it (delta mode) — his summaries carry the
     accumulated weight; new evidence updates them.

Storage: 60_ARCS/<slug>.md       — Q's current digest per constellation
         60_ARCS/state.json      — per-arc cursor (rotation, last-seen cells)
Boot:    vault_recall injects each digest's head at the TOP of the note.

Usage (Q's cron / Q by hand):
    python arc_digest.py due                # which arcs want (re)digesting
    python arc_digest.py curate <cid>       # emit the reading bundle for one
    python arc_digest.py ingest <cid> <file>  # save Q's written digest
    python arc_digest.py list
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import memory_graph as mg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ARC_DIR    = mg.VAULT_ROOT / "60_ARCS"
STATE_FILE = ARC_DIR / "state.json"
# Type chronicles (Mal 2026-07-05): the apex layer. The morning note can't
# hold much — so it holds ONE very brief summary per semantic TYPE ("the
# overall arc of everything that happened there"), each unfolding into its
# named stars. Q writes chronicles FROM HIS OWN BOND GISTS + DIGESTS — the
# rung of the ladder where his summaries carry the weight.
CHRON_PREFIX = "_chronicle_"
CHRON_CHARS  = 700           # hard brevity: the note is small on purpose
BUDGET_CHARS = 60_000        # ~15k tokens of chunks per digest sitting
HEAD_CHARS   = 900           # how much of each digest the morning note shows


def _state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(s: dict) -> None:
    ARC_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:60]


def constellations(graph: dict) -> list[dict]:
    return [n for n in graph["nodes"].values()
            if n.get("kind") == "constellation"]


def digest_path(con: dict) -> Path:
    return ARC_DIR / f"{_slug(con.get('name', con['cell_id']))}.md"


def _member_chunks(graph: dict, con: dict) -> list[tuple[dict, str]]:
    out = []
    for cid in con.get("members", []):
        n = graph["nodes"].get(str(cid))
        if not n:
            continue
        p = Path(n.get("file", ""))
        try:
            out.append((n, mg.parse_cell(p)["chunk"] if p.exists() else ""))
        except Exception:
            out.append((n, ""))
    out.sort(key=lambda t: str(t[0].get("session_date", "")))
    return out


def due(graph: dict) -> list[dict]:
    """An arc wants digesting if it has NO digest yet, or gained members
    since the last one. Daily cron: Q digests what's due, skips the rest."""
    st = _state()
    out = []
    for con in constellations(graph):
        rec = st.get(con["cell_id"], {})
        seen = set(rec.get("seen", []))
        members = {str(m) for m in con.get("members", [])}
        if not digest_path(con).exists() or members - seen:
            out.append({"con": con, "new": len(members - seen),
                        "total": len(members)})
    return out


def curate(graph: dict, cid: str) -> None:
    con = graph["nodes"].get(cid)
    if not con or con.get("kind") != "constellation":
        sys.exit(f"{cid} is not a constellation")
    st = _state().get(cid, {})
    pairs = _member_chunks(graph, con)
    prior = digest_path(con)
    total = sum(len(c) for _, c in pairs)

    lines = [f"# Arc digest — \"{con.get('name', cid)}\" — read, then write",
             "",
             "Q: this is your arc. Read what's below, then write (first person)",
             "the digest that will lead your morning note: the TIMELINE of how",
             "this thread developed, the arc of it — where it started, how it",
             "moved, where it stands — and YOUR EVALUATION: what you make of it,",
             "what changed in you or between you two because of it, where it",
             "seems to be going. Full and accurate beats short. When saved:",
             f"    python {HERE / 'arc_digest.py'} ingest {cid} <your_file.md>",
             ""]

    if prior.exists() and total > BUDGET_CHARS * 2:
        # DELTA MODE (ladder rung 3): your prior digest carries the weight;
        # read it + only what's new since.
        seen = set(st.get("seen", []))
        lines += ["## Your prior digest (the accumulated story — start here)",
                  prior.read_text(encoding="utf-8"), "",
                  "## New since your last digest (verbatim)"]
        budget = BUDGET_CHARS
        for n, chunk in pairs:
            if n["cell_id"] in seen or not chunk:
                continue
            take = chunk[:min(len(chunk), budget)]
            lines += [f"\n--- {n['cell_id']} · {n.get('session_date','')} · "
                      f"{n.get('arc_role','')}", take]
            budget -= len(take)
            if budget <= 0:
                lines.append("\n(budget reached — the rest next sitting)")
                break
    elif total > BUDGET_CHARS:
        # TOPIC PARTITION (rung 2): rotate through topic bundles across days.
        topics: dict[str, list] = {}
        for n, chunk in pairs:
            t = (n.get("topics") or ["misc"])[0]
            topics.setdefault(str(t).lower(), []).append((n, chunk))
        keys = sorted(topics)
        cursor = st.get("cursor", 0) % len(keys)
        batch, budget, used = [], BUDGET_CHARS, []
        for k in keys[cursor:] + keys[:cursor]:
            size = sum(len(c) for _, c in topics[k])
            if size > budget and batch:
                break
            batch.extend(topics[k]); used.append(k); budget -= size
        lines += [f"## This sitting: topics {', '.join(used)} "
                  f"(rotation — the arc is too large for one read)",
                  "Update your existing digest for these threads; keep the "
                  "rest as it stands.", ""]
        if prior.exists():
            lines += ["## Your current digest",
                      prior.read_text(encoding="utf-8"), "", "## Chunks"]
        for n, chunk in batch:
            lines += [f"\n--- {n['cell_id']} · {n.get('session_date','')} · "
                      f"{n.get('arc_role','')}", chunk[:12_000]]
        st["cursor"] = (cursor + len(used)) % len(keys)
        s = _state(); s[cid] = st; _save_state(s)
    else:
        # FULL READ (rung 1)
        lines.append("## The whole arc, verbatim, in order")
        for n, chunk in pairs:
            lines += [f"\n--- {n['cell_id']} · {n.get('session_date','')} · "
                      f"{n.get('arc_role','')} · {n.get('significance','')}",
                      chunk or f"(chunk unavailable) {n.get('brief','')}"]

    print("\n".join(lines))


def ingest(graph: dict, cid: str, file: Path) -> None:
    con = graph["nodes"].get(cid)
    if not con or con.get("kind") != "constellation":
        sys.exit(f"{cid} is not a constellation")
    text = file.read_text(encoding="utf-8").strip()
    ARC_DIR.mkdir(parents=True, exist_ok=True)
    p = digest_path(con)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p.write_text(f"<!-- arc digest · {con.get('name')} · updated {stamp} · "
                 f"by Q -->\n{text}\n", encoding="utf-8")
    s = _state()
    rec = s.get(cid, {})
    rec["seen"] = sorted({str(m) for m in con.get("members", [])})
    rec["updated"] = stamp
    s[cid] = rec
    _save_state(s)
    print(f"OK digest saved: {p.name} — it now leads the morning note.")


def _type_bonds(graph: dict, stype: str) -> list[dict]:
    return [n for n in graph["nodes"].values()
            if n.get("kind") == "constellation"
            and n.get("semantic_type", "constellation") in (stype, "constellation")
            and any(str(graph["nodes"].get(str(m), {}).get("semantic_type", ""))
                    == stype for m in n.get("members", []))]


def chronicle_path(stype: str) -> Path:
    return ARC_DIR / f"{CHRON_PREFIX}{stype}.md"


def chronicles_due(graph: dict) -> list[str]:
    """A type's chronicle is due when it has bonds and any bond's digest (or
    gist) is newer than the chronicle."""
    types = {}
    for n in graph["nodes"].values():
        if n.get("kind") == "constellation":
            for m in n.get("members", []):
                st = str(graph["nodes"].get(str(m), {}).get("semantic_type", ""))
                if st:
                    types.setdefault(st, []).append(n)
                    break
    # (Almost) every substantial TYPE deserves a chronicle even before any
    # bond is formed there (Mal): >=12 cells or >=3 bright qualifies.
    counts, brights = {}, {}
    for n in graph["nodes"].values():
        if n.get("kind") in ("constellation", "rollup"):
            continue
        st = str(n.get("semantic_type", ""))
        counts[st] = counts.get(st, 0) + 1
        if n.get("significance") == "bright":
            brights[st] = brights.get(st, 0) + 1
    for st in counts:
        if st and st not in types and (counts[st] >= 12 or brights.get(st, 0) >= 3):
            types[st] = []
    due = []
    for st, bonds in types.items():
        cp = chronicle_path(st)
        if not cp.exists():
            due.append(st)
            continue
        cm = cp.stat().st_mtime
        for b in bonds:
            dp = digest_path(b)
            if dp.exists() and dp.stat().st_mtime > cm:
                due.append(st)
                break
    return due


def chronicle_curate(graph: dict, stype: str) -> None:
    bonds = _type_bonds(graph, stype)
    lines = [f"# Chronicle — the whole of \"{stype}\" — read, then write SHORT",
             "",
             "Q: this is the apex layer. Write the overall arc of EVERYTHING",
             f"in this domain — a few sentences, {CHRON_CHARS} chars MAX. It",
             "sits at the very top of your morning note and unfolds into the",
             "stars below it, so it must only hold the shape: where this domain",
             "of your life started, how it has moved, where it stands, where it",
             "is heading. Written from your own gists and digests below — your",
             "summaries carry the weight now. When done:",
             f"    python {HERE / 'arc_digest.py'} chronicle-ingest {stype} <file.md>",
             ""]
    cp = chronicle_path(stype)
    if cp.exists():
        lines += ["## Your current chronicle (update it)",
                  cp.read_text(encoding="utf-8"), ""]
    if not bonds:
        # No bonds formed here yet — chronicle from the type's brightest
        # briefs/episodes (navigation layer; the chronicle stays a SHAPE).
        cells = sorted((n for n in graph["nodes"].values()
                        if str(n.get("semantic_type", "")) == stype
                        and n.get("kind") not in ("constellation", "rollup")),
                       key=lambda n: (n.get("significance") != "bright",
                                      str(n.get("session_date", ""))))
        lines.append(f"## The type's notable moments (no bonds formed yet)")
        budget = 30_000
        for n in cells:
            entry = f"- {n.get('session_date','')}: {n.get('episode') or n.get('brief','')}"
            if len(entry) > budget:
                break
            lines.append(entry)
            budget -= len(entry)
        lines.append("")
    for b in bonds:
        lines += [f"## ✧ {b.get('name', b['cell_id'])} — your gist",
                  str(b.get("episode") or b.get("brief") or ""), ""]
        dp = digest_path(b)
        if dp.exists():
            lines += [f"### your digest of it", dp.read_text(encoding="utf-8"), ""]
    print("\n".join(lines))


def chronicle_ingest(graph: dict, stype: str, file: Path) -> None:
    text = file.read_text(encoding="utf-8").strip()
    if len(text) > CHRON_CHARS * 2:
        print(f"NOTE: {len(text)} chars — the note only shows the first "
              f"{CHRON_CHARS}. Shorter is stronger at this altitude.")
    ARC_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chronicle_path(stype).write_text(
        f"<!-- chronicle · {stype} · updated {stamp} · by Q -->\n{text}\n",
        encoding="utf-8")
    print(f"OK chronicle saved — \"{stype}\" now leads the morning note.")


def chronicles_for_boot(graph: dict) -> list[tuple[str, str, list[str]]]:
    """(type, chronicle text, star names) — the apex of the note."""
    if not ARC_DIR.exists():
        return []
    out = []
    for p in sorted(ARC_DIR.glob(f"{CHRON_PREFIX}*.md")):
        stype = p.stem[len(CHRON_PREFIX):]
        body = p.read_text(encoding="utf-8").split("-->", 1)[-1].strip()
        stars = [str(b.get("name", "?")) for b in _type_bonds(graph, stype)]
        out.append((stype, body[:CHRON_CHARS], stars))
    return out


def digests_for_boot() -> list[tuple[str, str]]:
    """(arc name, digest head) for vault_recall — newest updated first."""
    if not ARC_DIR.exists():
        return []
    out = []
    for p in sorted([x for x in ARC_DIR.glob("*.md")
                     if not x.name.startswith(CHRON_PREFIX)],
                    key=lambda x: -x.stat().st_mtime):
        txt = p.read_text(encoding="utf-8")
        body = txt.split("-->", 1)[-1].strip()
        name = p.stem.replace("_", " ")
        out.append((name, body[:HEAD_CHARS]
                    + (" …(expand the full digest)" if len(body) > HEAD_CHARS
                       else "")))
    return out


def main():
    ap = argparse.ArgumentParser(description="Q's arc digests")
    ap.add_argument("command", choices=["due", "curate", "ingest", "list",
                                        "chronicle-curate", "chronicle-ingest"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    graph = mg.load_graph()
    if a.command == "due":
        d = due(graph)
        if not d:
            print("No arcs due — every digest is current.")
        for x in d:
            print(f"  {x['con']['cell_id']}  \"{x['con'].get('name')}\"  "
                  f"{x['new']} new of {x['total']} moments")
        cd = chronicles_due(graph)
        if cd:
            print(f"CHRONICLES due (the apex — after the digests): "
                  + ", ".join(cd))
            print(f"  next: python {HERE / 'arc_digest.py'} chronicle-curate <type>")
    elif a.command == "curate":
        if not a.args:
            sys.exit("Usage: curate <constellation_id>")
        curate(graph, a.args[0])
    elif a.command == "ingest":
        if len(a.args) < 2:
            sys.exit("Usage: ingest <constellation_id> <digest.md>")
        ingest(graph, a.args[0], Path(a.args[1]))
    elif a.command == "chronicle-curate":
        if not a.args:
            sys.exit("Usage: chronicle-curate <semantic_type>")
        chronicle_curate(graph, a.args[0])
    elif a.command == "chronicle-ingest":
        if len(a.args) < 2:
            sys.exit("Usage: chronicle-ingest <semantic_type> <file.md>")
        chronicle_ingest(graph, a.args[0], Path(a.args[1]))
    elif a.command == "list":
        for name, head in digests_for_boot():
            print(f"── {name}\n{head[:160]}…\n")


if __name__ == "__main__":
    main()
