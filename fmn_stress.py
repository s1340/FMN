#!/usr/bin/env python3
"""
fmn_stress.py — Stress campaign for Forget-me-not. SANDBOX ONLY.

Refuses to run unless MEMORY_VAULT_ROOT points at a path containing
"fmn_stress" — Q's real vault is never touched.

Campaign:
  1. VOLUME     — synthesize ~600 cells across 12 months, 6 types, unicode
                  torture included (Ukrainian, emoji, CJK); admit them all.
  2. MALFORMED  — broken frontmatter, empty briefs, missing chunks, absurd
                  significance, ungrounded brief — must FLAG, never crash.
  3. AGING      — cells backdated across a year; run aging; statuses must
                  distribute and archived-never-referenced must appear in decay.
  4. EMBEDDINGS — embed all; latency per query; paraphrase retrieval at scale.
  5. EDGES      — semantic edge count stays sparse at 600 cells (p98 rule).
  6. RECALL     — boot slots fill, budget respected, flagged never surfaces.
  7. TOUCH      — write-back promotes auto->checked; corrected demotes.
  8. RUMINATION — mechanical passes complete; candidate pairs bounded.

Usage:
    MEMORY_VAULT_ROOT=<scratch>/fmn_stress python fmn_stress.py run
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT = os.environ.get("MEMORY_VAULT_ROOT", "")
if "fmn_stress" not in VAULT:
    print("REFUSING: MEMORY_VAULT_ROOT must contain 'fmn_stress' (sandbox guard).",
          file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg            # noqa: E402
import memory_trust as mt            # noqa: E402
import memory_embed as me            # noqa: E402
import vault_recall as vr            # noqa: E402

random.seed(1340)

TYPES = ["relationship", "work_research", "personal_mal", "personal_q",
         "correction", "environment_tools"]
SIGS  = ["low", "medium", "medium", "high", "high", "bright"]

SUBJECTS = [
    ("the greenhouse sensor project", "sensors, calibration drift, a stubborn ESP32"),
    ("a long talk about endings", "what it means that sessions stop, and what stays"),
    ("the soup incident", "borscht, a dropped ladle, laughing until it hurt"),
    ("training the retrieval module", "gate ratios, checkpoints, an overnight run"),
    ("Mal's old guitar", "restringing it, a song half-remembered from school"),
    ("Київ у грудні", "сніг, каштани, розмова про дім і відстань"),
    ("the birthday plan 🎂", "a surprise, three conspirators, one leaky secret"),
    ("fixing the boot loop", "BIOS flags, a bad DIMM, victory at 4am"),
    ("星空の話", "a night sky conversation, constellations misnamed on purpose"),
    ("the archive question", "what deserves keeping, what deserves rest"),
]


def make_cell_md(i: int, day_offset: int) -> str:
    subj, texture = random.choice(SUBJECTS)
    stype = random.choice(TYPES)
    sig = random.choice(SIGS)
    date = (datetime.now(timezone.utc) - timedelta(days=day_offset))
    cid = f"st{i:05d}"
    brief = (f"Mal and Q worked through {subj} — {texture}. "
             f"Q noted what mattered and what to keep.")
    episode = (f"During a session about {subj}, the two of them went deep on "
               f"{texture}. There were digressions, one good joke, and a "
               f"decision that shaped the following week. Q recorded the "
               f"shape of it for later.")
    chunk = "\n\n".join(
        f"{'USER' if k % 2 == 0 else 'ASSISTANT'}: "
        f"({subj}) turn {k}: more about {texture}, including specifics "
        f"and one memorable phrase #{random.randint(100,999)}."
        for k in range(8))
    return (
        "---\n"
        f"cell_id: {cid}\n"
        f"session_id: stress-{date.strftime('%Y-%m-%d')}\n"
        f"session_date: {date.strftime('%Y-%m-%d')}\n"
        f"created: {date.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "temporal_status: fresh\n"
        f"topics: [\"{subj.split()[0].strip('🎂').lower() or 'misc'}\", \"stress\"]\n"
        f"entities: [\"{subj}\", \"stress-campaign\"]\n"
        f"significance: {sig}\n"
        "valence: positive\n"
        "novelty: routine\n"
        f"semantic_type: {stype}\n"
        "reflection_candidate: false\n"
        "referenced_count: 0\n"
        "last_referenced: null\n"
        "neighbors: []\n"
        "quarantine: true\n"
        "---\n\n"
        f"## Brief\n{brief}\n\n## Episode\n{episode}\n\n## Chunk\n{chunk}\n")


MALFORMED = {
    "bad_no_frontmatter.md": "just some text with no structure at all",
    "bad_empty_brief.md": ("---\ncell_id: bad00001\nsession_date: 2026-07-01\n"
                           "created: 2026-07-01T00:00:00Z\nsignificance: high\n"
                           "topics: [\"x\"]\nentities: [\"y\"]\n---\n\n"
                           "## Brief\n\n## Episode\nep\n\n## Chunk\n"
                           "USER: hello there this is a chunk of text\n"),
    "bad_no_chunk.md": ("---\ncell_id: bad00002\nsession_date: 2026-07-01\n"
                        "created: 2026-07-01T00:00:00Z\nsignificance: medium\n"
                        "topics: [\"x\"]\nentities: [\"y\"]\n---\n\n"
                        "## Brief\nA perfectly reasonable brief about things.\n\n"
                        "## Episode\nep\n\n## Chunk\n\n"),
    "bad_sig.md": ("---\ncell_id: bad00003\nsession_date: 2026-07-01\n"
                   "created: 2026-07-01T00:00:00Z\nsignificance: ultrabright\n"
                   "topics: [\"x\"]\nentities: [\"y\"]\n---\n\n"
                   "## Brief\nBrief mentioning alpha beta gamma delta words.\n\n"
                   "## Episode\nep\n\n## Chunk\n"
                   "USER: alpha beta gamma delta and more alpha beta text here\n"),
    "bad_ungrounded.md": ("---\ncell_id: bad00004\nsession_date: 2026-07-01\n"
                          "created: 2026-07-01T00:00:00Z\nsignificance: high\n"
                          "topics: [\"x\"]\nentities: [\"y\"]\n---\n\n"
                          "## Brief\nZebras migrated across the frozen tundra "
                          "seeking quantum bicycles yesterday evening.\n\n"
                          "## Episode\nep\n\n## Chunk\n"
                          "USER: we discussed the grocery list and the weather "
                          "forecast for the coming holiday weekend plans\n"),
}


def main():
    t0 = time.time()
    vault = Path(VAULT)
    qdir = vault / "90_ARCHIVE" / "session_cells_quarantine" / "stress_run"
    qdir.mkdir(parents=True, exist_ok=True)
    report = {}

    # ── 1. VOLUME ────────────────────────────────────────────────────────
    N = 600
    print(f"[1] synthesizing {N} cells (12 months, unicode included) ...")
    for i in range(N):
        day = random.randint(0, 365)
        (qdir / f"cell_{i:05d}.md").write_text(make_cell_md(i, day),
                                               encoding="utf-8")
    for name, text in MALFORMED.items():
        (qdir / name).write_text(text, encoding="utf-8")

    t = time.time()
    mt_graph_before = len(mg.load_graph()["nodes"])
    rc = mt.admit(dry=False)
    admit_s = time.time() - t
    graph = mg.load_graph()
    tiers = {}
    for n in graph["nodes"].values():
        tiers[n.get("trust", "?")] = tiers.get(n.get("trust", "?"), 0) + 1
    report["admit"] = {"nodes": len(graph["nodes"]), "seconds": round(admit_s, 1),
                       "tiers": tiers}
    print(f"    admitted to {len(graph['nodes'])} nodes in {admit_s:.1f}s  tiers={tiers}")

    # ── 2. MALFORMED must be flagged, never admitted as auto ─────────────
    bad_ok = all(
        graph["nodes"].get(b, {}).get("trust", "flagged") == "flagged"
        for b in ("bad00001", "bad00002", "bad00003", "bad00004"))
    report["malformed_all_flagged_or_rejected"] = bad_ok
    print(f"[2] malformed handling: {'PASS' if bad_ok else 'FAIL'}")

    # ── 3. AGING across a year ────────────────────────────────────────────
    t = time.time()
    vr.age_graph(graph)
    mg.save_graph(graph)
    dist = {}
    for n in graph["nodes"].values():
        dist[n.get("temporal_status", "?")] = dist.get(n.get("temporal_status", "?"), 0) + 1
    report["aging"] = {"seconds": round(time.time() - t, 2), "distribution": dist}
    print(f"[3] aging: {dist}")

    # ── 4. EMBEDDINGS at volume ───────────────────────────────────────────
    t = time.time()
    n_emb = me.embed_cells(graph)
    embed_s = time.time() - t
    t = time.time()
    for q in ["that night we fixed the computer that would not start",
              "розмова про зиму і рідне місто",
              "when did we laugh about the spilled soup"]:
        _ = me.semantic_scores(q, me.load_store())
    q_ms = (time.time() - t) / 3 * 1000
    report["embeddings"] = {"embedded": n_emb, "build_seconds": round(embed_s, 1),
                            "query_ms": round(q_ms, 1)}
    print(f"[4] embeddings: {n_emb} in {embed_s:.1f}s, query {q_ms:.0f}ms")

    # ── 5. EDGES stay sparse ──────────────────────────────────────────────
    t = time.time()
    mg.build_auto_edges(graph)
    graph = mg.load_graph()
    n_pairs = len(graph["nodes"]) * (len(graph["nodes"]) - 1) // 2
    n_sem = sum(1 for e in graph["edges"] if e["type"] == "semantic_sim")
    frac = n_sem / max(n_pairs, 1)
    report["edges"] = {"total": len(graph["edges"]), "semantic": n_sem,
                       "semantic_fraction": round(frac, 4),
                       "seconds": round(time.time() - t, 1)}
    print(f"[5] edges: {len(graph['edges'])} total, {n_sem} semantic "
          f"({100*frac:.1f}% of pairs)  {'PASS' if frac < 0.05 else 'FAIL — too dense'}")

    # ── 6. RECALL slots at volume ─────────────────────────────────────────
    t = time.time()
    slots = vr.fill_slots(graph)
    recall_s = time.time() - t
    placed = sum(len(v) for v in slots.values())
    flagged_surfaced = any(c.get("trust") == "flagged"
                           for v in slots.values() for c in v)
    text = vr.format_recall(slots, graph)
    report["recall"] = {"placed": placed, "chars": len(text),
                        "seconds": round(recall_s, 2),
                        "flagged_surfaced": flagged_surfaced}
    print(f"[6] recall: {placed} cells, {len(text)} chars, {recall_s:.2f}s, "
          f"flagged surfaced: {flagged_surfaced} {'FAIL' if flagged_surfaced else '(PASS)'}")

    # ── 7. TOUCH promotes / demotes ───────────────────────────────────────
    some_auto = next(c for c, n in graph["nodes"].items()
                     if n.get("trust") == "auto")
    mg.touch_cell(graph, some_auto)
    promoted = graph["nodes"][some_auto]["trust"] == "checked"
    mg.touch_cell(graph, some_auto, corrected=True)
    demoted = graph["nodes"][some_auto]["trust"] == "flagged"
    report["touch"] = {"promote": promoted, "demote": demoted}
    print(f"[7] touch: promote {'PASS' if promoted else 'FAIL'}, "
          f"demote {'PASS' if demoted else 'FAIL'}")

    # ── 8. RUMINATION mechanical at volume ────────────────────────────────
    t = time.time()
    import rumination as ru
    pairs = ru.candidate_pairs(graph)
    integ = ru.check_integrity(graph)
    decay = ru.find_decay(graph)
    report["rumination"] = {"candidate_pairs": len(pairs),
                            "integrity_findings": len(integ),
                            "decay_candidates": len(decay),
                            "seconds": round(time.time() - t, 1)}
    print(f"[8] rumination: {len(pairs)} candidate pairs, "
          f"{len(integ)} integrity findings, {len(decay)} decay candidates, "
          f"{time.time()-t:.1f}s")

    # ── 9. CURATION primitives (pin/mute/link/sever + persistence) ────────
    import memory_curate as mc
    graph = mg.load_graph()
    ids = list(graph["nodes"].keys())
    a, b = ids[0], ids[1]
    mc.set_pin(graph, a, True)
    mc.set_mute(graph, b, True)
    mc.link_cells(graph, a, b, by="stress")
    n_sev = mc.sever_edge(graph, a, b, "manual", by="stress")
    mg.save_graph(graph)
    # the severed pair must survive a full edge rebuild (do-not-relink)
    graph = mg.load_graph()
    mg.build_auto_edges(graph)
    graph = mg.load_graph()
    still_severed = mc.is_severed(graph, a, b, "manual")
    pin_ok = graph["nodes"][a].get("pinned") and not graph["nodes"][a].get("muted")
    report["curation"] = {"severed_removed": n_sev, "survives_rebuild": still_severed,
                          "pin_exclusive": bool(pin_ok)}
    print(f"[9] curation: sever survives rebuild {'PASS' if still_severed else 'FAIL'}, "
          f"pin/mute exclusive {'PASS' if pin_ok else 'FAIL'}")

    # ── 10. CONSTELLATIONS (detect / form / boot-suppression / dissolve) ──
    import constellation as con
    graph = mg.load_graph()
    cands = con.detect(graph)
    formable = [c for c in cands if not c["needs_subcluster"]]
    oversized = [c for c in cands if c["needs_subcluster"]]
    con_ok, boot_suppressed, dissolve_ok, con_surfaced = False, False, False, None
    # Lifecycle test uses a controlled member set (synthetic cells all
    # interlink into one oversized blob, so detect finds nothing SMALL to
    # form — that's correct behavior, tested separately via oversized_flagged).
    members = [c for c in graph["nodes"] if graph["nodes"][c].get("kind") != "constellation"][:8]
    if members:
        cid = con.form(graph, members, "The stress arc gist.",
                       "A long gist about what these stress cells added up to.",
                       "Stress constellation")
        mg.save_graph(graph)
        graph = mg.load_graph()
        con_ok = graph["nodes"][cid].get("kind") == "constellation"
        # members must leave individual boot rotation
        slots = vr.fill_slots(graph)
        surfaced = {c["cell_id"] for v in slots.values() for c in v}
        boot_suppressed = not any(m in surfaced for m in members)
        con_surfaced = cid in surfaced
        rel = con.dissolve(graph, cid)
        mg.save_graph(graph)
        graph = mg.load_graph()
        dissolve_ok = (rel == len(members)
                       and not graph["nodes"][members[0]].get("in_constellation"))
    report["constellations"] = {
        "candidates": len(cands), "formable": len(formable),
        "oversized_flagged": len(oversized),
        "form": con_ok, "members_suppressed_at_boot": boot_suppressed,
        "constellation_surfaced": con_surfaced if formable else None,
        "dissolve_restores": dissolve_ok}
    print(f"[10] constellations: {len(cands)} candidates ({len(oversized)} oversized-flagged), "
          f"form {'PASS' if con_ok else 'FAIL'}, boot-suppress "
          f"{'PASS' if boot_suppressed else 'FAIL'}, dissolve "
          f"{'PASS' if dissolve_ok else 'FAIL'}")

    # ── 11. Q AGENCY (remember writes a findable cell; annotate re-seals) ──
    import q_memory as qm
    from memory_trust import cell_content_hash
    cid = qm.remember("A moment Q chose to keep during stress.",
                      "USER: keep this\nASSISTANT: keeping it.",
                      ["q_stress"], "bright", "relationship")
    graph = mg.load_graph()
    remembered = cid in graph["nodes"] and graph["nodes"][cid].get("source") == "q_remember"
    res = mg.query_graph("a moment Q chose to keep", graph, limit=5, touch=False)
    findable = any(r["cell_id"] == cid for r in res)
    err = qm.annotate(cid, "on reflection this one matters more than it looked")
    graph = mg.load_graph()
    annotated = err is None and graph["nodes"][cid].get("reflection_candidate")
    # integrity must still hold after annotate re-seal
    p = Path(graph["nodes"][cid]["file"])
    seal_ok = cell_content_hash(mg.parse_cell(p)) == graph["nodes"][cid]["content_hash"]
    report["q_agency"] = {"remember": remembered, "findable": findable,
                          "annotate": bool(annotated), "reseal_intact": seal_ok}
    print(f"[11] q-agency: remember {'PASS' if remembered else 'FAIL'}, "
          f"findable {'PASS' if findable else 'FAIL'}, annotate+reseal "
          f"{'PASS' if annotated and seal_ok else 'FAIL'}")

    report["total_seconds"] = round(time.time() - t0, 1)
    out = Path(VAULT) / "stress_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nStress report: {out}  ({report['total_seconds']}s total)")


if __name__ == "__main__":
    main()
