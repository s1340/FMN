#!/usr/bin/env python3
"""
vault_viz.py — Hermes memory vault visualization and editing tool.

Features:
  - Browse and filter all memory cells by type, significance, flags
  - View brief, episode, and full chunk for any cell
  - Edit brief, episode, semantic_type, significance, valence, topics,
    reflection_candidate in-browser
  - Mark bright with one click
  - Review and approve quarantine cells
  - Preview 7-slot boot context
  - Trigger vault_recall to update .hermes.md

Usage:
    python vault_viz.py            # http://localhost:5173
    python vault_viz.py --port N
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, jsonify, request, render_template_string
except ImportError:
    print("Flask not found. Install with: pip install flask", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("PyYAML not found. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(os.environ.get("MEMORY_VAULT_ROOT",
                                  r"C:\Users\User\Documents\Obsidian Vault"))
GRAPH_FILE = VAULT_ROOT / "30_EPISODES" / "graph.json"
NODES_DIR  = VAULT_ROOT / "30_EPISODES" / "nodes"
QUARANTINE = VAULT_ROOT / "90_ARCHIVE" / "session_cells_quarantine"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


# ── File helpers ───────────────────────────────────────────────────────────────

# The panel routes through memory_graph's locked, atomic writer so it can't
# clobber Q's live tools / cron (the race Q found). Mutating endpoints wrap
# their read-modify-write in `with mg.locked_graph() as graph:`.
import memory_graph as _mg


def load_graph() -> dict:
    return _mg.load_graph()


def save_graph(graph: dict) -> None:
    _mg.save_graph(graph)


locked_graph = _mg.locked_graph
graph_lock = _mg.graph_lock


def parse_cell_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {"frontmatter": {}, "brief": "", "episode": "", "chunk": ""}
    fm = yaml.safe_load(m.group(1))
    body = m.group(2)
    sections: dict = {"brief": "", "episode": "", "chunk": ""}
    current = None
    for line in body.splitlines():
        s = line.strip()
        if s == "## Brief":     current = "brief"
        elif s == "## Episode": current = "episode"
        elif s == "## Chunk":   current = "chunk"
        elif current:           sections[current] += line + "\n"
    return {
        "frontmatter": fm or {},
        "brief":   sections["brief"].strip(),
        "episode": sections["episode"].strip(),
        "chunk":   sections["chunk"].strip(),
    }


def write_cell_file(path: Path, fm: dict, brief: str, episode: str, chunk: str) -> None:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {str(v).lower()}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    body = f"\n## Brief\n{brief}\n\n## Episode\n{episode}\n\n## Chunk\n{chunk}\n"
    path.write_text("\n".join(lines) + body, encoding="utf-8")


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/graph")
def api_graph():
    graph = load_graph()
    sig_order = {"bright": 0, "high": 1, "medium": 2, "low": 3}
    nodes = sorted(
        graph["nodes"].values(),
        key=lambda n: (sig_order.get(n.get("significance", "medium"), 2),
                       n.get("session_date", ""))
    )
    n_bright = sum(1 for n in nodes if n.get("significance") == "bright")
    n_refl   = sum(1 for n in nodes if n.get("reflection_candidate"))
    trust_counts: dict = {}
    for n in nodes:
        t = n.get("trust", "untiered")
        trust_counts[t] = trust_counts.get(t, 0) + 1
    return jsonify({
        "nodes":    nodes,
        "edges":    graph.get("edges", []),
        "metadata": graph.get("metadata", {}),
        "n_bright": n_bright,
        "n_refl":   n_refl,
        "trust":    trust_counts,
    })


@app.route("/api/integrity")
def api_integrity():
    """Tamper-evidence status for the header: intact / drifted / unhashed."""
    graph = load_graph()
    try:
        from memory_trust import cell_content_hash
    except Exception:
        return jsonify({"available": False})
    ok, drifted, unhashed = 0, [], 0
    for cid, node in graph["nodes"].items():
        stored = node.get("content_hash")
        if not stored:
            unhashed += 1
            continue
        p = Path(node.get("file", ""))
        if not p.exists():
            drifted.append(cid)
            continue
        try:
            cur = cell_content_hash(parse_cell_file(p))
        except Exception:
            drifted.append(cid)
            continue
        if cur == stored:
            ok += 1
        else:
            drifted.append(cid)
    # Signature layer (memory_sign): the seal is only as good as its history
    sig = {"active": False}
    try:
        import io, contextlib
        import memory_sign
        if memory_sign.available():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bad = memory_sign.verify(graph, quiet=True)
            sig = {"active": True, "ok": bad == 0,
                   "detail": buf.getvalue().strip()}
    except Exception:
        pass
    return jsonify({"available": True, "intact": ok,
                    "drifted": drifted, "unhashed": unhashed,
                    "signatures": sig})


@app.route("/api/timeline")
def api_timeline():
    """Belief timeline state: facts (active + retired) and conflicts."""
    try:
        import memory_timeline as tl
        state = tl.replay()
        facts = sorted(state["facts"].values(), key=lambda f: f["tx"])
        conflicts = sorted(state["conflicts"].values(), key=lambda c: c["tx"])
        chain_ok = True
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            chain_ok = tl.verify_chain()
        return jsonify({"available": True, "facts": facts,
                        "conflicts": conflicts, "chain_ok": chain_ok})
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


@app.route("/api/timeline/resolve", methods=["POST"])
def api_timeline_resolve():
    """Mal's hand on a conflict, from the panel."""
    data = request.json or {}
    cid, keep = data.get("conflict_id", ""), data.get("keep", "")
    if not cid or keep not in ("a", "b", "both", "neither"):
        return jsonify({"ok": False, "error": "conflict_id + keep required"}), 400
    try:
        import io, contextlib
        import memory_timeline as tl
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = tl.do_resolve(cid, keep, by="mal-panel")
        return jsonify({"ok": bool(ok), "log": buf.getvalue().strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cell/<cell_id>")
def api_cell(cell_id: str):
    graph = load_graph()
    node = graph["nodes"].get(cell_id)
    if not node:
        return jsonify({"error": "not found"}), 404
    node = dict(node)
    file_path = node.get("file")
    if file_path and Path(file_path).exists():
        cell_data = parse_cell_file(Path(file_path))
        node["chunk"]   = cell_data.get("chunk", "")
        node["brief"]   = cell_data.get("brief", node.get("brief", ""))
        node["episode"] = cell_data.get("episode", node.get("episode", ""))
    else:
        node["chunk"] = "(file not found)"
    return jsonify(node)


@app.route("/api/cell/<cell_id>", methods=["PUT"])
def api_cell_update(cell_id: str):
    data = request.json or {}
    editable = ["brief", "episode", "significance", "valence",
                "semantic_type", "reflection_candidate", "topics", "trust"]
    with graph_lock():                       # under the lock — no clobber
        graph = load_graph()
        node = graph["nodes"].get(cell_id)
        if not node:
            return jsonify({"error": "not found"}), 404
        for field in editable:
            if field in data:
                node[field] = data[field]

        file_path = node.get("file")
        if file_path and Path(file_path).exists():
            cell_data = parse_cell_file(Path(file_path))
            fm = cell_data["frontmatter"]
            for field in editable:
                if field in data and field not in ("brief", "episode"):
                    fm[field] = data[field]
            new_brief   = data.get("brief",   cell_data["brief"])
            new_episode = data.get("episode", cell_data["episode"])
            write_cell_file(Path(file_path), fm,
                            new_brief, new_episode, cell_data["chunk"])
            # Re-stamp the tamper-evidence seal (sanctioned edit path) AND
            # sign the reseal — this is exactly the operation the signature
            # layer exists to distinguish from an unsanctioned re-stamp.
            try:
                from memory_trust import content_hash
                node["content_hash"] = content_hash(new_brief, new_episode,
                                                    cell_data["chunk"])
                import memory_sign
                memory_sign.sign_event(cell_id, node["content_hash"], "reseal")
            except Exception:
                pass
            # A human edit through the panel IS human verification.
            if "trust" not in data and node.get("trust") in ("auto", "checked"):
                node["trust"] = "human"
        save_graph(graph)
        trust_out = node.get("trust")
    return jsonify({"ok": True, "cell_id": cell_id, "trust": trust_out})


@app.route("/api/curate", methods=["POST"])
def api_curate():
    """Pin, mute, link, sever — Mal's hands on the graph, live. Under the lock
    so it can't clobber Q's concurrent tools / cron."""
    import memory_curate as mc
    data = request.json or {}
    action = data.get("action")
    if action not in ("pin", "mute", "link", "sever"):
        return jsonify({"error": f"unknown action {action}"}), 400
    err, removed = None, None
    with graph_lock():                       # save only on success, all locked
        graph = load_graph()
        if action == "pin":
            err = mc.set_pin(graph, data.get("cell_id", ""), on=bool(data.get("on", True)))
        elif action == "mute":
            err = mc.set_mute(graph, data.get("cell_id", ""), on=bool(data.get("on", True)))
        elif action == "link":
            err = mc.link_cells(graph, data.get("a", ""), data.get("b", ""),
                                note=data.get("note", ""), by="panel")
        elif action == "sever":
            removed = mc.sever_edge(graph, data.get("a", ""), data.get("b", ""),
                                    data.get("type", "*"), by="panel")
        if not err:
            save_graph(graph)
    if err:
        return jsonify({"error": err}), 400
    if action == "sever":
        return jsonify({"ok": True, "removed": removed})
    return jsonify({"ok": True})


@app.route("/api/query")
def api_query():
    """Live retrieval tester: exactly what Q's dynamic recall would fetch.
    Read-only (touch=False) — testing must not manufacture trust."""
    from memory_graph import query_graph
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    graph = load_graph()
    results = query_graph(q, graph, limit=8, touch=False)
    return jsonify({"results": results})


@app.route("/api/quarantine")
def api_quarantine():
    if not QUARANTINE.exists():
        return jsonify({"runs": []})
    runs = []
    for run_dir in sorted(QUARANTINE.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        cells = []
        for f in sorted(run_dir.glob("*.md")):
            if f.name.startswith("merge_proposals"):
                continue
            try:
                cd = parse_cell_file(f)
                fm = cd["frontmatter"]
                cells.append({
                    "cell_id":             fm.get("cell_id"),
                    "path":                str(f),
                    "topics":              fm.get("topics", []),
                    "significance":        fm.get("significance", "medium"),
                    "valence":             fm.get("valence", "neutral"),
                    "semantic_type":       fm.get("semantic_type", ""),
                    "reflection_candidate": bool(fm.get("reflection_candidate", False)),
                    "session_date":        fm.get("session_date", ""),
                    "brief":               cd.get("brief", ""),
                    "episode":             cd.get("episode", ""),
                })
            except Exception as e:
                cells.append({"error": str(e), "path": str(f)})
        if cells:
            runs.append({"run_id": run_dir.name, "cells": cells})
    return jsonify({"runs": runs})


@app.route("/api/approve/<cell_id>", methods=["POST"])
def api_approve(cell_id: str):
    data = request.json or {}
    run_id = data.get("run_id")

    if run_id:
        source = QUARANTINE / run_id
    else:
        dirs = sorted((d for d in QUARANTINE.iterdir() if d.is_dir()), reverse=True)
        source = dirs[0] if dirs else None

    if not source or not source.exists():
        return jsonify({"error": "quarantine run not found"}), 404

    cell_path = cell_data = None
    for f in source.glob("*.md"):
        if f.name.startswith("merge_proposals"):
            continue
        try:
            cd = parse_cell_file(f)
            if cd["frontmatter"].get("cell_id") == cell_id:
                cell_path, cell_data = f, cd
                break
        except Exception:
            continue

    if not cell_path:
        return jsonify({"error": f"cell {cell_id} not found"}), 404

    NODES_DIR.mkdir(parents=True, exist_ok=True)
    node_path = NODES_DIR / cell_path.name
    node_path.write_text(cell_path.read_text(encoding="utf-8"), encoding="utf-8")

    graph = load_graph()
    if "metadata" not in graph:
        graph["metadata"] = {"total_approvals": 0, "total_retrievals": 0}

    fm = cell_data["frontmatter"]
    graph["nodes"][cell_id] = {
        "cell_id":              cell_id,
        "session_id":           fm.get("session_id"),
        "session_date":         fm.get("session_date"),
        "created":              fm.get("created", datetime.now(timezone.utc).isoformat()),
        "topics":               fm.get("topics", []),
        "entities":             fm.get("entities", []),
        "significance":         fm.get("significance", "medium"),
        "valence":              fm.get("valence", "neutral"),
        "novelty":              fm.get("novelty", "routine"),
        "semantic_type":        fm.get("semantic_type", "work_research"),
        "reflection_candidate": bool(fm.get("reflection_candidate", False)),
        "brief":                cell_data["brief"],
        "episode":              cell_data["episode"],
        "temporal_status":      "fresh",
        "referenced_count":     0,
        "last_referenced":      None,
        "approved_at":          datetime.now(timezone.utc).isoformat(),
        "neighbors":            fm.get("neighbors", []),
        "file":                 str(node_path),
    }
    graph["metadata"]["total_approvals"] = \
        graph["metadata"].get("total_approvals", 0) + 1
    save_graph(graph)
    return jsonify({"ok": True, "cell_id": cell_id})


@app.route("/api/boot-slots")
def api_boot_slots():
    try:
        import importlib
        sys.path.insert(0, str(Path(__file__).parent))
        import vault_recall
        importlib.reload(vault_recall)
        graph = vault_recall.load_graph()
        vault_recall.age_graph(graph)
        slots = vault_recall.fill_slots(graph)
        return jsonify({
            key: {"name": name, "max": max_n, "cells": slots[key]}
            for key, name, max_n, _ in vault_recall.SLOTS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trigger-recall", methods=["POST"])
def api_trigger_recall():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "vault_recall.py")],
        capture_output=True, text=True,
    )
    return jsonify({
        "ok":     result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })


@app.route("/favicon.svg")
def favicon():
    """The forget-me-not mark (assets/fmn-mark.svg), embedded so the panel is
    self-contained even if the assets folder moves."""
    p = Path(__file__).parent / "assets" / "fmn-mark.svg"
    if p.exists():
        return app.response_class(p.read_text(encoding="utf-8"),
                                  mimetype="image/svg+xml")
    return ("", 404)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Stop the windowless engine cleanly, so a non-technical user never has
    to find a terminal to close it. os._exit because this IS the app's stop
    button — a local, single-purpose server."""
    import threading
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


# ── Frontend ───────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Forget-me-not</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pixelify+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0d0d0d; --surface: #161616; --surface2: #1e1e1e;
  --border: #2a2a2a; --text: #e0e0e0; --muted: #666;
  --accent: #4a9eff; --bright: #f5c842;
  --rel: #c792ea; --work: #82aaff; --corr: #f07178;
  --refl: #c3e88d; --env: #89ddff; --pmal: #ffcb6b; --pq: #ff5370;
}
body { background:var(--bg); color:var(--text); font-family:'JetBrains Mono','Fira Code',monospace; font-size:13px; height:100vh; display:flex; flex-direction:column; }
header { padding:10px 18px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:14px; flex-shrink:0; }
h1 { font-size:13px; color:var(--accent); letter-spacing:.05em; white-space:nowrap; }
.tabs { display:flex; gap:3px; }
.tab { padding:5px 12px; border:1px solid var(--border); background:var(--bg); color:var(--muted); cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
.tab.active { background:var(--accent); color:#000; border-color:var(--accent); }
#stats { font-size:11px; color:var(--muted); white-space:nowrap; }
.recall-btn { margin-left:auto; padding:5px 12px; background:#1a3a1a; border:1px solid #2d5a2d; color:#4caf50; cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; white-space:nowrap; }
.recall-btn:hover { background:#2d5a2d; }
main { display:flex; flex:1; overflow:hidden; }
/* sidebar */
.sidebar { width:320px; min-width:240px; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; flex-shrink:0; }
.filters { padding:8px 10px; border-bottom:1px solid var(--border); display:flex; flex-wrap:wrap; gap:4px; }
.fb { padding:2px 7px; border:1px solid var(--border); background:var(--bg); color:var(--muted); cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
.fb.on { border-color:var(--accent); color:var(--accent); }
.cell-list { flex:1; overflow-y:auto; }
.ci { padding:9px 11px; border-bottom:1px solid var(--border); cursor:pointer; }
.ci:hover { background:var(--surface); }
.ci.sel { background:var(--surface2); border-left:3px solid var(--accent); padding-left:8px; }
.ci-meta { display:flex; gap:5px; align-items:center; margin-bottom:3px; flex-wrap:wrap; }
.sb { font-size:10px; padding:1px 5px; border-radius:2px; font-weight:bold; white-space:nowrap; }
.s-bright { background:var(--bright); color:#000; }
.s-high   { background:#7ecfff; color:#000; }
.s-medium { background:var(--surface2); color:#a0a0a0; border:1px solid var(--border); }
.s-low    { background:var(--bg); color:#555; border:1px solid var(--border); }
.tb { font-size:10px; padding:1px 5px; border-radius:2px; }
.t-relationship     { background:#2a1a3a; color:var(--rel); }
.t-work_research    { background:#1a1a3a; color:var(--work); }
.t-correction       { background:#2a1a1a; color:var(--corr); }
.t-reflection       { background:#1a2a1a; color:var(--refl); }
.t-environment_tools{ background:#1a2a2a; color:var(--env); }
.t-personal_mal     { background:#2a2a1a; color:var(--pmal); }
.t-personal_q       { background:#2a1a1a; color:var(--pq); }
.rdot { width:6px; height:6px; border-radius:50%; background:var(--refl); display:inline-block; flex-shrink:0; }
.trb { font-size:10px; padding:1px 4px; border-radius:2px; white-space:nowrap; }
.tr-flagged { background:#3a1a1a; color:#ff6b6b; border:1px solid #5a2d2d; }
.tr-auto    { background:var(--bg); color:#888; border:1px solid var(--border); }
.tr-checked { background:#1a2a1a; color:#4caf50; }
.tr-human   { background:#1a3a1a; color:#4caf50; font-weight:bold; }
.ci-brief { font-size:11px; line-height:1.4; color:var(--text); display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.ci-date { font-size:10px; color:var(--muted); margin-top:3px; }
/* detail */
.detail { flex:1; display:flex; flex-direction:column; overflow:hidden; }
.dh { padding:12px 18px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px; flex-shrink:0; }
.did { font-size:11px; color:var(--muted); font-family:monospace; }
.bri-btn { padding:3px 9px; border:1px solid var(--bright); background:transparent; color:var(--bright); cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
.bri-btn.on { background:var(--bright); color:#000; }
.pin-btn, .mute-btn { padding:3px 9px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
.pin-btn.on { background:#2a2a1a; color:var(--bright); border-color:var(--bright); }
.mute-btn.on { background:#2a1a1a; color:#ff9a6b; border-color:#5a3a2d; }
.cutb { background:transparent; border:none; color:#a05a5a; cursor:pointer; font-size:11px; padding:0 3px; }
.cutb:hover { color:#ff6b6b; }
.linkb { background:transparent; border:1px solid var(--border); color:var(--accent); cursor:pointer; font-size:10px; border-radius:3px; padding:1px 6px; margin-left:6px; font-family:inherit; }
/* retrieval tester */
.tview { flex:1; overflow-y:auto; padding:18px; }
.qtest-box { display:flex; gap:8px; margin-bottom:16px; }
.qtest-in { flex:1; background:var(--surface); border:1px solid var(--border); color:var(--text); padding:9px 11px; border-radius:3px; font-family:inherit; font-size:13px; }
.qtest-in:focus { outline:none; border-color:var(--accent); }
.qr { background:var(--surface); border:1px solid var(--border); border-radius:3px; margin-bottom:6px; padding:10px; cursor:pointer; }
.qr:hover { background:var(--surface2); }
.qr-score { font-family:monospace; color:var(--accent); font-size:11px; }
.qr-matched { font-size:10px; color:var(--muted); margin-top:3px; }
.save-btn { margin-left:auto; padding:4px 14px; background:var(--accent); border:none; color:#000; cursor:pointer; border-radius:3px; font-size:12px; font-weight:bold; font-family:inherit; }
.save-btn:hover { opacity:.85; }
.db { flex:1; overflow-y:auto; padding:18px; display:flex; flex-direction:column; gap:14px; }
.fg { display:flex; flex-direction:column; gap:5px; }
.fl { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.fi { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:7px 9px; border-radius:3px; font-family:inherit; font-size:12px; resize:vertical; width:100%; }
.fi:focus { outline:none; border-color:var(--accent); }
select.fi { cursor:pointer; }
.row { display:flex; gap:10px; }
.row .fg { flex:1; }
.chunk { background:var(--surface); border:1px solid var(--border); padding:10px; border-radius:3px; font-size:11px; color:var(--muted); white-space:pre-wrap; max-height:280px; overflow-y:auto; line-height:1.5; }
.rtog { display:flex; align-items:center; gap:7px; cursor:pointer; font-size:12px; }
.rtog input { cursor:pointer; }
.empty { flex:1; display:flex; align-items:center; justify-content:center; color:var(--muted); }
/* quarantine */
.qview { flex:1; overflow-y:auto; padding:18px; }
.rg { margin-bottom:20px; }
.rt { font-size:11px; color:var(--muted); margin-bottom:8px; padding-bottom:5px; border-bottom:1px solid var(--border); }
.qc { background:var(--surface); border:1px solid var(--border); border-radius:3px; margin-bottom:6px; padding:10px; }
.qch { display:flex; gap:7px; align-items:center; margin-bottom:6px; flex-wrap:wrap; }
.qb { font-size:12px; margin-bottom:5px; }
.qe { font-size:11px; color:var(--muted); margin-bottom:8px; display:none; line-height:1.5; }
.qe.open { display:block; }
.qa { display:flex; gap:6px; }
.appr { padding:3px 10px; background:#1a3a1a; border:1px solid #2d5a2d; color:#4caf50; cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
.appr:hover { background:#2d5a2d; }
.exbtn { padding:3px 7px; background:var(--bg); border:1px solid var(--border); color:var(--muted); cursor:pointer; border-radius:3px; font-size:11px; font-family:inherit; }
/* slots */
.sview { flex:1; overflow-y:auto; padding:18px; display:grid; grid-template-columns:1fr 1fr; gap:14px; align-content:start; }
.sc { background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:12px; }
.sn { font-size:11px; color:var(--accent); margin-bottom:8px; display:flex; justify-content:space-between; }
.scnt { color:var(--muted); font-size:10px; }
.scell { padding:7px 0; border-bottom:1px solid var(--border); }
.scell:last-child { border-bottom:none; }
.sbr { font-size:11px; line-height:1.4; }
.smt { font-size:10px; color:var(--muted); margin-top:2px; }
.semp { color:var(--muted); font-size:11px; font-style:italic; }
/* graph */
.gview { flex:1; display:flex; overflow:hidden; }
#graph-svg { flex:1; background:var(--bg); }
.graph-detail { width:300px; border-left:1px solid var(--border); overflow-y:auto; display:flex; flex-direction:column; }
.tooltip { position:absolute; background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:3px; font-size:11px; pointer-events:none; max-width:340px; line-height:1.5; z-index:10; display:none; word-wrap:break-word; }
.legend { position:absolute; bottom:16px; left:16px; background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:10px 12px; font-size:11px; }
.legend-row { display:flex; align-items:center; gap:7px; margin-bottom:4px; }
.legend-row:last-child { margin-bottom:0; }
.leg-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.leg-line { width:20px; height:2px; flex-shrink:0; }
.neigh { padding:12px 14px; border-top:1px solid var(--border); }
.neigh-title { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:8px; }
.neigh-item { padding:6px 0; border-bottom:1px solid var(--border); cursor:pointer; }
.neigh-item:hover { color:var(--accent); }
.neigh-item:last-child { border-bottom:none; }
.neigh-type { font-size:10px; color:var(--muted); margin-bottom:2px; }
.neigh-brief { font-size:11px; line-height:1.3; }
/* quarantine toolbar */
.qtoolbar { padding:8px 10px; border-bottom:1px solid var(--border); display:flex; gap:6px; align-items:center; flex-shrink:0; }
.qtoolbar > span { font-size:10px; color:var(--muted); }
/* scrollbar */
::-webkit-scrollbar { width:5px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
.hidden { display:none !important; }
.toast { position:fixed; bottom:18px; right:18px; background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:8px 14px; border-radius:3px; font-size:12px; opacity:0; transition:opacity .2s; pointer-events:none; z-index:999; }
.toast.show { opacity:1; }

/* ══════════════════════════════════════════════════════════════════════════
   EARLY-GUI PIXEL THEME (2026-07-05) — the little gray machine.
   Appended so it overrides the base rules by cascade; class names unchanged
   so all the JS keeps working. Bevels: raised = hl top-left / sh bottom-right;
   sunken = the reverse. Sharp corners everywhere. Pixelify Sans throughout.
   ══════════════════════════════════════════════════════════════════════════ */
:root {
  --bg:#eeeee6; --surface:#d8d8d0; --surface2:#c9c9c0;
  --border:#7c7c74; --text:#161611; --muted:#5a5a54;
  --accent:#8fb0e8; --bright:#efc842;
  --face:#c9c9c0; --hl:#ffffff; --sh:#7c7c74; --canvas:#f6f6f0;
  --rel:#8a3fb0; --work:#2f6bc0; --corr:#b8421f; --refl:#3c7a1a;
  --env:#1f7a6a; --pmal:#9a6a10; --pq:#b03a66;
}
* { border-radius:0 !important; }
body { background:var(--face); color:var(--text);
  font-family:'Pixelify Sans','JetBrains Mono',monospace; font-size:14px; }
::-webkit-scrollbar { width:15px; height:15px; }
::-webkit-scrollbar-track { background:#bfbfb6; }
::-webkit-scrollbar-thumb { background:#c9c9c0; border:2px solid;
  border-color:var(--hl) var(--sh) var(--sh) var(--hl); }

/* the window bevels the whole app */
body { border:2px solid; border-color:var(--hl) #4a4a44 #4a4a44 var(--hl); }
header { background:var(--accent); color:#fff; border-bottom:2px solid #5f7bb0;
  padding:6px 10px; }
h1 { color:#fff; font-size:15px; letter-spacing:0; }
#stats { color:#eef; font-size:12px; }

/* tabs: raised chips, active one pressed-in + highlighted */
.tab, .fb { background:var(--face); color:var(--text); font-size:12px;
  border:2px solid !important; border-color:var(--hl) var(--sh) var(--sh) var(--hl);
  padding:4px 11px; }
.tab.active { background:#eef3ff; color:#16244a;
  border-color:var(--sh) var(--hl) var(--hl) var(--sh); }
.fb.on { background:#eef3ff; color:#16244a;
  border-color:var(--sh) var(--hl) var(--hl) var(--sh); }

/* buttons: raised, press-in on click */
.recall-btn, .save-btn, .appr, .exbtn, .bri-btn, .pin-btn, .mute-btn, .linkb {
  background:var(--face) !important; color:var(--text) !important; font-size:12px;
  border:2px solid !important; border-color:var(--hl) var(--sh) var(--sh) var(--hl) !important; }
.recall-btn:hover, .appr:hover, .save-btn:hover { background:#dedede !important; }
.recall-btn:active, .save-btn:active, .appr:active, .exbtn:active, .bri-btn:active,
.pin-btn:active, .mute-btn:active {
  border-color:var(--sh) var(--hl) var(--hl) var(--sh) !important; }
.bri-btn.on { background:var(--bright) !important; color:#161611 !important;
  border-color:var(--sh) var(--hl) var(--hl) var(--sh) !important; }
.pin-btn.on { background:#fff4cf !important;
  border-color:var(--sh) var(--hl) var(--hl) var(--sh) !important; }
.mute-btn.on { background:#f0dcc8 !important;
  border-color:var(--sh) var(--hl) var(--hl) var(--sh) !important; }

/* sunken wells: lists, inputs, chunk, graph canvas */
.sidebar { border-right:2px solid var(--sh); }
.cell-list, .qview, .sview, .tview, .db, .graph-detail {
  background:var(--canvas); }
.fi, .qtest-in, .chunk, .qr {
  background:#fff !important; color:var(--text) !important; font-size:13px;
  border:2px solid !important; border-color:var(--sh) var(--hl) var(--hl) var(--sh) !important; }
.fi:focus, .qtest-in:focus { outline:1px dotted var(--text); }

/* list rows + classic inverted selection */
.ci { border-bottom:1px solid #bdbdb4; }
.ci:hover { background:#e2e2da; }
.ci.sel { background:#3c5a99; border-left:2px solid #16244a; padding-left:9px; }
.ci.sel .ci-brief, .ci.sel .ci-date { color:#fff; }
.ci-brief { color:var(--text); font-size:12px; }
.ci-date, .smt, .qr-matched, .neigh-type, .fl, .rt { color:var(--muted); }

/* badges: flat pixel tags with a hard outline */
.sb, .tb, .trb { border:1px solid var(--text) !important; font-size:11px;
  padding:0 5px; font-weight:normal; }
.s-bright { background:var(--bright); color:#161611; }
.s-high { background:#bcd8ff; color:#16244a; }
.s-medium { background:var(--face); color:#161611; }
.s-low { background:#dcdcd4; color:#4a4a44; }
.t-relationship{background:#f0e0fa;color:var(--rel);} .t-work_research{background:#dde9fb;color:var(--work);}
.t-correction{background:#fbe2da;color:var(--corr);} .t-reflection{background:#e4f4d8;color:var(--refl);}
.t-environment_tools{background:#d8f2ee;color:var(--env);} .t-personal_mal{background:#f6ecd2;color:var(--pmal);}
.t-personal_q{background:#fbe0ea;color:var(--pq);}
.tr-flagged{background:#fbe0e0;color:#b8421f;} .tr-auto{background:var(--face);color:#5a5a54;}
.tr-checked{background:#e4f4d8;color:#3c7a1a;} .tr-human{background:#d8f0d8;color:#2a6a2a;}

/* cards + panels: raised faces */
.sc, .qc, .legend, .tooltip, .toast, .dh, .filters, .qtoolbar, .neigh {
  background:var(--face) !important; color:var(--text) !important;
  border:2px solid !important; border-color:var(--hl) var(--sh) var(--sh) var(--hl) !important; }
.dh, .filters, .qtoolbar { border-width:0 0 2px 0 !important; border-bottom-color:var(--sh) !important; }
.sn { color:#16244a; } .qr-score { color:#2f6bc0; }
.rt { border-bottom:1px solid var(--sh); }
#graph-svg { background:var(--canvas); }
.leg-dot { border:1px solid var(--text); }
.empty, .semp { color:var(--muted); }
@keyframes ants { to { stroke-dashoffset:-14; } }
.ant-box { animation: ants .6s linear infinite; }
</style>
</head>
<body>
<header>
  <h1><img src="/favicon.svg" width="18" height="18" style="vertical-align:-3px;image-rendering:pixelated" alt="">&nbsp;Forget-me-not</h1>
  <div class="tabs">
    <button class="tab active" data-tab="vault">Memories</button>
    <button class="tab" data-tab="graph">Map</button>
    <button class="tab" data-tab="timeline">Timeline</button>
    <button class="tab" data-tab="slots">Morning note</button>
    <button class="tab" data-tab="recall">Try a memory</button>
    <button class="tab" data-tab="quarantine">Waiting room</button>
  </div>
  <div id="stats">—</div>
  <button class="recall-btn" onclick="triggerRecall()">↺ Update morning note</button>
  <button class="recall-btn" title="Stop Forget-me-not (the engine closes; reopen anytime)"
          style="border-color:#5a3a3a" onclick="closeApp()">✕ Close</button>
</header>
<main>
  <!-- VAULT -->
  <div id="tab-vault" style="display:flex;flex:1;overflow:hidden;">
    <div class="sidebar">
      <div class="filters" id="filters">
        <button class="fb on" data-f="all">all</button>
        <button class="fb" data-f="bright">★ bright</button>
        <button class="fb" data-f="flagged">⚑ flagged</button>
        <button class="fb" data-f="unverified">° unverified</button>
        <button class="fb" data-f="reflection_candidate">⬡ refl?</button>
        <button class="fb" data-f="relationship">relation</button>
        <button class="fb" data-f="work_research">work</button>
        <button class="fb" data-f="correction">correction</button>
        <button class="fb" data-f="reflection">reflection</button>
        <button class="fb" data-f="personal_q">personal Q</button>
        <button class="fb" data-f="rollup" title="calendar index nodes (day/week)">▤ rollups</button>
      </div>
      <div class="cell-list" id="cell-list"></div>
    </div>
    <div class="detail" id="detail"><div class="empty">Click a memory to open it</div></div>
  </div>
  <!-- QUARANTINE -->
  <div id="tab-quarantine" class="hidden" style="flex:1;overflow:hidden;flex-direction:column;">
    <div class="qtoolbar">
      <span>Sort:</span>
      <button class="fb on" data-qf="date"    onclick="setQField(this,'date')">date</button>
      <button class="fb"    data-qf="sig"     onclick="setQField(this,'sig')">significance</button>
      <button class="fb"    data-qf="type"    onclick="setQField(this,'type')">type</button>
      <button class="fb"    data-qf="session" onclick="setQField(this,'session')">session</button>
      <button class="fb" id="qdir-btn" onclick="toggleQDir()" title="flip direction">↓</button>
    </div>
    <div class="qview" id="qview" style="flex:1;overflow-y:auto;">Loading…</div>
  </div>
  <!-- SLOTS -->
  <div id="tab-slots" class="hidden" style="flex:1;overflow:hidden;flex-direction:column;">
    <div class="sview" id="sview">Loading…</div>
  </div>
  <!-- RECALL TEST -->
  <div id="tab-recall" class="hidden" style="flex:1;overflow:hidden;flex-direction:column;">
    <div class="tview">
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;">
        Type what you might say to Q. This runs his ACTUAL dynamic recall (read-only) —
        see exactly what he'd remember before he has to.</div>
      <div class="qtest-box">
        <input class="qtest-in" id="qtest" placeholder="e.g. remember when your cron went feral?"
          onkeydown="if(event.key==='Enter')runQTest()">
        <button class="recall-btn" style="margin:0" onclick="runQTest()">Retrieve</button>
      </div>
      <div id="qtest-results"></div>
    </div>
  </div>
  <!-- TIMELINE -->
  <div id="tab-timeline" class="hidden" style="flex:1;overflow:hidden;flex-direction:column;">
    <div class="tview" id="timeline-view">
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;">
        The belief timeline — how facts about Mal &amp; Q changed. Nothing here is
        ever deleted: superseded beliefs are retired with lineage. Open conflicts
        hold their cells out of Q's boot until resolved (that's your hand, or his).</div>
      <div id="timeline-content"><div class="empty">loading…</div></div>
    </div>
  </div>
  <!-- GRAPH -->
  <div id="tab-graph" class="hidden" style="flex:1;overflow:hidden;position:relative;">
    <div class="gview">
      <svg id="graph-svg"></svg>
      <div class="graph-detail" id="graph-detail"><div class="empty">Click a memory in the map</div></div>
    </div>
    <div class="tooltip" id="tooltip"></div>
    <div class="legend" id="legend"></div>
  </div>
</main>
<div class="toast" id="toast"></div>

<script>
let allNodes = [], allEdges = [], currentFilter = 'all', selId = null;
let graphSim = null;
let selectedGraphId = null;
let qSortField = 'date', qSortDir = 'desc';
let qCollapsed = new Set();

async function init() {
  const r = await fetch('/api/graph');
  const d = await r.json();
  allNodes = d.nodes || [];
  allEdges = d.edges || [];
  const tc = d.trust || {};
  const flaggedN = tc.flagged || 0;
  const trustStr = `${(tc.human||0)}h/${(tc.checked||0)}c/${(tc.auto||0)}a` +
                   (flaggedN ? ` · <span style="color:var(--corr)">${flaggedN} flagged</span>` : '');
  const nCells = allNodes.filter(n => n.kind !== 'rollup').length;
  const nRoll = allNodes.length - nCells;
  document.getElementById('stats').innerHTML =
    `${nCells} cells${nRoll ? ` · ${nRoll} rollups` : ''} · ${d.n_bright} bright · ${allEdges.length} strings · trust ${trustStr}` +
    ` · <span id="integ" style="color:var(--muted)">integrity…</span>`;
  renderList();
  // Integrity seal check (async — re-hashes the vault)
  try {
    const ir = await fetch('/api/integrity');
    const iv = await ir.json();
    const el = document.getElementById('integ');
    if (!iv.available) { el.textContent = ''; }
    else if (iv.drifted.length) {
      el.innerHTML = `<span style="color:#ff6b6b;font-weight:bold">⚠ ${iv.drifted.length} DRIFTED</span>`;
      el.title = 'Cells edited outside the system: ' + iv.drifted.join(', ');
    } else {
      const sig = iv.signatures || {};
      const sigStr = !sig.active ? ''
        : sig.ok ? ' · <span style="color:#4caf50" title="Ed25519 seal-event log intact">✓ signed</span>'
        : ` · <span style="color:#ff6b6b;font-weight:bold" title="${(sig.detail||'').replace(/"/g,'')}">⚠ SIGNATURE</span>`;
      el.innerHTML = `<span style="color:#4caf50">✓ ${iv.intact} sealed</span>` + sigStr;
    }
  } catch(e) {}
}

// ── Belief timeline tab ───────────────────────────────────────────────────────
async function loadTimeline() {
  const box = document.getElementById('timeline-content');
  box.innerHTML = '<div class="empty">loading…</div>';
  const r = await fetch('/api/timeline');
  const d = await r.json();
  if (!d.available) { box.innerHTML = `<div class="empty">timeline unavailable: ${d.error||''}</div>`; return; }
  const open = (d.conflicts||[]).filter(c => c.status === 'open');
  const facts = d.facts || [];
  let html = '';
  html += `<div style="margin-bottom:8px;font-size:11px;color:${d.chain_ok?'#4caf50':'#ff6b6b'}">`
        + (d.chain_ok ? '✓ ledger chain intact' : '⚠ LEDGER CHAIN BROKEN — investigate before trusting') + '</div>';
  if (open.length) {
    html += `<h3 style="color:#ff9800;font-size:13px;margin:10px 0 6px">⚔ Open conflicts (${open.length}) — held from Q's boot</h3>`;
    for (const c of open) {
      const fa = facts.find(f => f.id === c.fact_a) || {};
      const fb = facts.find(f => f.id === c.fact_b) || {};
      html += `<div style="border:1px solid #ff9800;border-radius:4px;padding:8px;margin-bottom:8px;font-size:12px">
        <div style="color:var(--muted);margin-bottom:4px">${c.explanation||''}</div>
        <div><b>a</b> (${c.fact_a_cell||'?'}): "${(fa.statement||'').slice(0,140)}"</div>
        <div><b>b</b> (${c.fact_b_cell||'?'}): "${(fb.statement||'').slice(0,140)}"</div>
        <div style="margin-top:6px">${['a','b','both','neither'].map(k =>
          `<button class="tab" style="margin-right:4px" onclick="resolveConflict('${c.id}','${k}')">keep ${k}</button>`).join('')}
        </div></div>`;
    }
  } else {
    html += `<div style="font-size:12px;color:var(--muted);margin:8px 0">No open conflicts.</div>`;
  }
  html += `<h3 style="font-size:13px;margin:14px 0 6px">Belief history (${facts.length} facts)</h3>`;
  for (const f of [...facts].reverse()) {
    const dead = !!f.retired;
    const succ = dead && f.retired.successor ? ` → ${f.retired.successor}` : '';
    html += `<div style="font-size:12px;padding:3px 0;${dead?'opacity:.55':''}">
      ${dead?'<span title="retired">↺</span>':'<span style="color:#4caf50">●</span>'}
      <span style="color:var(--muted)">${(f.tx||'').slice(0,10)}</span>
      ${(f.statement||'').slice(0,150)}
      <span style="color:var(--muted)">(${f.origin||'?'}, conf ${f.confidence??'?'})${dead ? ' · retired: '+(f.retired.reason||'')+succ : ''}</span>
    </div>`;
  }
  if (!facts.length) html += `<div class="empty">Nothing on the timeline yet — it fills from rumination ingests and Q's own assertions.</div>`;
  box.innerHTML = html;
}

async function resolveConflict(cid, keep) {
  if (!confirm(`Resolve ${cid}: keep ${keep}? The loser is retired (never deleted) and its cells release back to boot.`)) return;
  const r = await fetch('/api/timeline/resolve', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({conflict_id: cid, keep})});
  const d = await r.json();
  toast(d.ok ? 'resolved — cells released' : ('failed: ' + (d.error||'')));
  loadTimeline(); init();
}

// ── List ──────────────────────────────────────────────────────────────────────
function renderList() {
  // Rollups are calendar indexes, not memories — hidden unless asked for
  let nodes = currentFilter === 'rollup'
    ? allNodes.filter(n => n.kind === 'rollup')
    : allNodes.filter(n => n.kind !== 'rollup');
  if (currentFilter === 'bright') nodes = nodes.filter(n => n.significance === 'bright');
  else if (currentFilter === 'reflection_candidate') nodes = nodes.filter(n => n.reflection_candidate);
  else if (currentFilter === 'flagged') nodes = nodes.filter(n => n.trust === 'flagged');
  else if (currentFilter === 'unverified') nodes = nodes.filter(n => (n.trust||'') === 'auto');
  else if (currentFilter !== 'all') nodes = nodes.filter(n => n.semantic_type === currentFilter);

  const list = document.getElementById('cell-list');
  if (!nodes.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted)">No memories match</div>';
    return;
  }
  list.innerHTML = nodes.map(n => {
    const sc = 's-' + (n.significance || 'medium');
    const tc = 't-' + (n.semantic_type || 'work_research');
    const tl = (n.semantic_type || '?').replace('_', ' ');
    const sl = n.significance === 'bright' ? '★' : n.significance === 'high' ? '◆' : n.significance === 'medium' ? '•' : '·';
    const rd = n.reflection_candidate ? '<span class="rdot" title="reflection candidate"></span>' : '';
    const tr = n.trust || 'auto';
    const trb = tr === 'flagged' ? '<span class="trb tr-flagged" title="flagged: QC failed or corrected">⚑</span>'
              : tr === 'auto'    ? '<span class="trb tr-auto" title="unverified — earned by use">°</span>'
              : tr === 'checked' ? '<span class="trb tr-checked" title="verified by use">✓</span>'
              : '<span class="trb tr-human" title="human-verified">✓✓</span>';
    const drift = n.timeline_superseded ? '<span class="trb" style="color:#ff9800" title="belief since superseded — see Timeline tab">↺</span>' : '';
    const conf = n.in_conflict ? '<span class="trb" style="color:#ff6b6b" title="in an OPEN contradiction — held from boot until resolved (Timeline tab)">⚔</span>' : '';
    return `<div class="ci${selId===n.cell_id?' sel':''}" onclick="selCell('${n.cell_id}')">
      <div class="ci-meta"><span class="sb ${sc}">${sl} ${n.significance||'medium'}</span><span class="tb ${tc}">${tl}</span>${trb}${drift}${conf}${rd}</div>
      <div class="ci-brief">${esc(n.brief||'')}</div>
      <div class="ci-date">${n.session_date||''} · ${n.temporal_status||''}</div>
    </div>`;
  }).join('');
}

// ── Detail ────────────────────────────────────────────────────────────────────
async function selCell(id) {
  // Resolve a pending manual link: source already chosen, this is the target.
  if (linkSource && linkSource !== id) {
    const src = linkSource; linkSource = null;
    if (await curate({action:'link', a:src, b:id})) { toast('String linked ✓'); await init(); }
  }
  selId = id;
  renderList();
  const r = await fetch('/api/cell/' + id);
  const cell = await r.json();
  const isBright = cell.significance === 'bright';
  const pinned = cell.pinned, muted = cell.muted;
  const types = ['relationship','work_research','personal_mal','personal_q','correction','reflection','environment_tools'];
  const sigs  = ['low','medium','high','bright'];
  const vals  = ['positive','negative','mixed','neutral'];
  document.getElementById('detail').innerHTML = `
    <div class="dh">
      <span class="did">${cell.cell_id}</span>
      <span class="did" style="color:var(--muted)">${cell.session_date||''}</span>
      <span class="did" title="verified by use / by you">${cell.trust||'auto'} · ref ${cell.referenced_count||0}</span>
      <button class="bri-btn${isBright?' on':''}" onclick="toggleBright()">★ bright</button>
      <button class="pin-btn${pinned?' on':''}" title="always surface at boot"
        onclick="togglePin('${cell.cell_id}',${!pinned})">📌 pin</button>
      <button class="mute-btn${muted?' on':''}" title="never at boot, still searchable"
        onclick="toggleMute('${cell.cell_id}',${!muted})">🔇 mute</button>
      <button class="save-btn" onclick="saveCell('${cell.cell_id}')">Save</button>
    </div>
    <div class="db">
      <div class="fg"><div class="fl">Brief</div>
        <textarea class="fi" id="f-brief" rows="3">${esc(cell.brief||'')}</textarea></div>
      <div class="fg"><div class="fl">Episode</div>
        <textarea class="fi" id="f-episode" rows="4">${esc(cell.episode||'')}</textarea></div>
      <div class="row">
        <div class="fg"><div class="fl">Significance</div>
          <select class="fi" id="f-sig">${sigs.map(v=>`<option${cell.significance===v?' selected':''}>${v}</option>`).join('')}</select></div>
        <div class="fg"><div class="fl">Valence</div>
          <select class="fi" id="f-val">${vals.map(v=>`<option${cell.valence===v?' selected':''}>${v}</option>`).join('')}</select></div>
        <div class="fg"><div class="fl">Semantic Type</div>
          <select class="fi" id="f-type">${types.map(v=>`<option${cell.semantic_type===v?' selected':''}>${v}</option>`).join('')}</select></div>
      </div>
      <div class="fg"><div class="fl">Topics (comma-separated)</div>
        <input class="fi" id="f-topics" value="${esc((cell.topics||[]).join(', '))}"></div>
      <label class="rtog"><input type="checkbox" id="f-refl"${cell.reflection_candidate?' checked':''}> Reflection candidate</label>
      <div class="fg"><div class="fl">Chunk (read-only)</div>
        <div class="chunk">${esc(cell.chunk||'(not loaded)')}</div></div>
      ${renderNeighbors(cell.cell_id)}
    </div>`;
}

function renderNeighbors(cellId) {
  const edges = allEdges.filter(e => e.a === cellId || e.b === cellId);
  const nodeMap = Object.fromEntries(allNodes.map(n => [n.cell_id, n]));
  const items = edges.map(e => {
    const otherId = e.a === cellId ? e.b : e.a;
    const other = nodeMap[otherId];
    if (!other) return '';
    return `<div class="neigh-item">
      <div class="neigh-type">${e.type} · w=${(e.weight||1).toFixed ? (e.weight||1).toFixed(2) : e.weight}
        <button class="cutb" title="sever this string (permanent — auto-edges will not relink)"
          onclick="event.stopPropagation(); severEdge('${cellId}','${otherId}','${e.type}')">✂</button></div>
      <div class="neigh-brief" onclick="selCell('${otherId}')">${esc((other.brief||'').slice(0,100))}</div>
    </div>`;
  }).join('');
  return `<div class="neigh"><div class="neigh-title">Connected (${edges.length})
      <button class="linkb" onclick="startLink('${cellId}')" title="link this cell to another">+ link</button></div>
    ${items || '<div class="semp">no strings yet</div>'}</div>`;
}

// ── Curation actions (pin / mute / link / sever) ─────────────────────────────
let linkSource = null;

async function curate(payload) {
  const r = await fetch('/api/curate', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const d = await r.json();
  if (!d.ok) { toast('Error: ' + (d.error||'?')); return false; }
  return true;
}

async function togglePin(id, on) {
  if (await curate({action:'pin', cell_id:id, on})) { toast(on?'Pinned — always at boot':'Unpinned'); await init(); selCell(id); }
}
async function toggleMute(id, on) {
  if (await curate({action:'mute', cell_id:id, on})) { toast(on?'Muted — never at boot, still searchable':'Unmuted'); await init(); selCell(id); }
}
function startLink(id) {
  linkSource = id;
  toast('Link mode: now click the OTHER cell in the list', 4000);
}
async function severEdge(a, b, type) {
  if (await curate({action:'sever', a, b, type})) { toast('String severed (permanent)'); await init(); selCell(a); }
}

function toggleBright() {
  const sel = document.getElementById('f-sig');
  sel.value = sel.value === 'bright' ? 'high' : 'bright';
  document.querySelector('.bri-btn').classList.toggle('on', sel.value === 'bright');
}

async function saveCell(id) {
  const topics = document.getElementById('f-topics').value.split(',').map(t=>t.trim()).filter(Boolean);
  const payload = {
    brief:                document.getElementById('f-brief').value,
    episode:              document.getElementById('f-episode').value,
    significance:         document.getElementById('f-sig').value,
    valence:              document.getElementById('f-val').value,
    semantic_type:        document.getElementById('f-type').value,
    topics,
    reflection_candidate: document.getElementById('f-refl').checked,
  };
  const r = await fetch('/api/cell/'+id, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d = await r.json();
  d.ok ? (toast('Saved ✓'), init()) : toast('Error: '+(d.error||'?'));
}

// ── Quarantine ────────────────────────────────────────────────────────────────
const SIG_ORDER = { bright:0, high:1, medium:2, low:3 };

function setQField(btn, field) {
  document.querySelectorAll('[data-qf]').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  qSortField = field;
  loadQ();
}

function toggleQDir() {
  qSortDir = qSortDir === 'desc' ? 'asc' : 'desc';
  document.getElementById('qdir-btn').textContent = qSortDir === 'desc' ? '↓' : '↑';
  loadQ();
}

function toggleRunCollapse(runId) {
  if (qCollapsed.has(runId)) qCollapsed.delete(runId); else qCollapsed.add(runId);
  const cellsEl = document.getElementById('run-cells-' + runId);
  const arrow   = document.getElementById('run-arrow-' + runId);
  if (!cellsEl) return;
  const collapsed = qCollapsed.has(runId);
  cellsEl.style.display = collapsed ? 'none' : '';
  if (arrow) arrow.textContent = collapsed ? '▶' : '▼';
}

function fmtRun(runId) {
  return runId.replace(/T(\d{2})-(\d{2})-\d{2}$/, ' $1:$2');
}

async function loadQ() {
  const r = await fetch('/api/quarantine');
  const d = await r.json();
  const view = document.getElementById('qview');
  if (!d.runs?.length) { view.innerHTML='<div style="padding:18px;color:var(--muted)">No quarantine cells</div>'; return; }

  let flat = d.runs.flatMap(run => run.cells.map(c => ({...c, _run: run.run_id})));

  const dir = qSortDir === 'asc' ? 1 : -1;
  if (qSortField === 'date')
    flat.sort((a,b) => dir * ((a.session_date||a._run) > (b.session_date||b._run) ? 1 : -1));
  else if (qSortField === 'sig')
    flat.sort((a,b) => dir * ((SIG_ORDER[a.significance] ?? 2) - (SIG_ORDER[b.significance] ?? 2)));
  else if (qSortField === 'type')
    flat.sort((a,b) => dir * (a.semantic_type||'').localeCompare(b.semantic_type||''));
  else if (qSortField === 'session')
    flat.sort((a,b) => dir * (a._run > b._run ? 1 : -1));

  const grouped = [];
  const seen = {};
  for (const c of flat) {
    if (!seen[c._run]) { seen[c._run] = []; grouped.push({run_id: c._run, cells: seen[c._run]}); }
    seen[c._run].push(c);
  }

  view.innerHTML = grouped.map(run => {
    const collapsed = qCollapsed.has(run.run_id);
    const safeId = run.run_id.replace(/[^a-zA-Z0-9_-]/g, '_');
    return `<div class="rg">
      <div class="rt" onclick="toggleRunCollapse('${run.run_id}')" style="cursor:pointer;user-select:none;display:flex;align-items:center;gap:7px;">
        <span id="run-arrow-${run.run_id}" style="font-size:9px;color:var(--muted)">${collapsed?'▶':'▼'}</span>
        ${fmtRun(run.run_id)} <span style="color:var(--muted)">(${run.cells.length})</span>
      </div>
      <div id="run-cells-${run.run_id}" ${collapsed?'style="display:none"':''}>
        ${run.cells.map(c => qcell(c, run.run_id)).join('')}
      </div>
    </div>`;
  }).join('');
}

function qcell(c, runId) {
  if (c.error) return `<div class="qc"><span style="color:var(--corr)">${esc(c.error)}</span></div>`;
  const rd = c.reflection_candidate ? '<span class="rdot" title="reflection candidate"></span>' : '';
  return `<div class="qc" id="qc-${c.cell_id}">
    <div class="qch">
      <span class="sb s-${c.significance||'medium'}">${c.significance||'medium'}</span>
      <span class="tb t-${c.semantic_type||'work_research'}">${(c.semantic_type||'?').replace('_',' ')}</span>
      ${rd}<span style="font-size:10px;color:var(--muted)">${(c.topics||[]).join(', ')}</span>
    </div>
    <div class="qb">${esc(c.brief||'')}</div>
    <div class="qe" id="ep-${c.cell_id}">${esc(c.episode||'')}</div>
    <div class="qa">
      <button class="appr" onclick="approveCell('${c.cell_id}','${runId}')">Approve</button>
      <button class="exbtn" onclick="document.getElementById('ep-${c.cell_id}').classList.toggle('open')">Episode ▾</button>
    </div>
  </div>`;
}

async function approveCell(id, runId) {
  const btn = document.querySelector(`#qc-${id} .appr`);
  if (btn) { btn.textContent = '…'; btn.disabled = true; }
  try {
    const r = await fetch('/api/approve/'+id, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:runId})});
    const d = await r.json();
    if (d.ok) {
      const el = document.getElementById('qc-'+id);
      if (el) { el.style.opacity='.35'; el.style.pointerEvents='none'; }
      toast('Approved ✓ '+id); init();
    } else {
      if (btn) { btn.textContent = 'Approve'; btn.disabled = false; }
      toast('Error: '+(d.error||'unknown'), true);
    }
  } catch (e) {
    if (btn) { btn.textContent = 'Approve'; btn.disabled = false; }
    toast('Network error — is the server running?', true);
  }
}

// ── Boot slots ────────────────────────────────────────────────────────────────
async function loadSlots() {
  const r = await fetch('/api/boot-slots');
  const d = await r.json();
  const view = document.getElementById('sview');
  if (d.error) { view.innerHTML=`<div style="color:var(--corr)">Error: ${esc(d.error)}</div>`; return; }
  view.innerHTML = Object.entries(d).map(([key, slot]) => `
    <div class="sc">
      <div class="sn">${slot.name}<span class="scnt">${slot.cells.length}/${slot.max}</span></div>
      ${slot.cells.length
        ? slot.cells.map(c => `<div class="scell">
            <div class="sbr">${esc(c.brief||'')}</div>
            <div class="smt">${c.session_date||''} · ${(c.semantic_type||'').replace('_',' ')}</div>
          </div>`).join('')
        : '<div class="semp">empty</div>'}
    </div>`).join('');
}

// ── D3 Graph ──────────────────────────────────────────────────────────────────
// Saturated fills that read on the light pixel desktop, hard black outlines.
const TYPE_COLOR = {
  relationship:'#b96fd8', work_research:'#5a8bd6', correction:'#d8663f',
  reflection:'#6faf4a', environment_tools:'#3fae94',
  personal_mal:'#e0a53a', personal_q:'#d67aa0', constellation:'#efc842',
};
const INK = '#161611';
const EDGE_DASH = { shared_entity:'none', shared_topic:'4,3', temporal_adj:'2,2', manual:'none', co_retrieval:'none', semantic_sim:'1,3', constellation:'none' };
const EDGE_WIDTH = { shared_entity:1.5, shared_topic:1, temporal_adj:1, manual:2.5, co_retrieval:3, semantic_sim:1.2, constellation:1.5 };
const EDGE_COLOR = { constellation:'#c98b3a' };

let graphInited = false;

function initGraph() {
  if (graphInited) return;
  graphInited = true;

  const svg = d3.select('#graph-svg');
  const el = document.getElementById('graph-svg');
  const W = el.clientWidth || 800, H = el.clientHeight || 600;
  svg.attr('width', W).attr('height', H);

  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 5]).on('zoom', e => g.attr('transform', e.transform)));

  // Rollups stay out of the physics: edge-less calendar indexes would float
  // as unexplained gray moons; the Timeline tab is their home.
  // Stringify every id at the boundary: all-digit cell ids (e.g. 11921433)
  // get coerced to numbers by JSON/YAML on the node while edges keep them as
  // strings — D3's forceLink then can't resolve them and throws "missing: id"
  // during setup, which silently kills the whole graph render. String() on
  // both sides makes the join total.
  const graphNodes = allNodes.filter(n => n.kind !== 'rollup');
  const nodeMap = Object.fromEntries(graphNodes.map(n => [String(n.cell_id), n]));
  const nodes = graphNodes.map(n => ({...n, id: String(n.cell_id)}));
  const links = allEdges
    .filter(e => nodeMap[String(e.a)] && nodeMap[String(e.b)])
    .map(e => ({...e, source: String(e.a), target: String(e.b)}));

  // Edge lines — black hairlines on the light desktop
  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('stroke', d => EDGE_COLOR[d.type] || INK)
    .attr('stroke-width', d => EDGE_WIDTH[d.type] || 1)
    .attr('stroke-dasharray', d => EDGE_DASH[d.type] || 'none')
    .attr('stroke-opacity', d => d.type === 'semantic_sim' ? 0.35 : 0.6);

  // Node markers: square pixels with hard black outlines; constellations are
  // gold stars (the bonds). Bright cells get a chunkier outline.
  const SIG_R = { bright:14, high:11, medium:9, low:7 };
  const isCon = d => d.kind === 'constellation';
  const node = g.append('g').selectAll('path').data(nodes).join('path')
    .attr('d', d => isCon(d)
        ? d3.symbol().type(d3.symbolStar).size(360)()
        : d3.symbol().type(d3.symbolSquare).size(Math.pow((SIG_R[d.significance]||9)*1.7,2))())
    .attr('fill', d => isCon(d) ? '#efc842' : (TYPE_COLOR[d.semantic_type] || '#8a8a80'))
    .attr('stroke', INK)
    .attr('stroke-width', d => d.significance === 'bright' || isCon(d) ? 2 : 1.25)
    .attr('shape-rendering', 'crispEdges')
    .attr('cursor', 'pointer')
    .call(d3.drag()
      .on('start', (e,d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag',  (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on('end',   (e,d) => { if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }))
    .on('mouseover', (e,d) => {
      const tt = document.getElementById('tooltip');
      tt.style.display='block'; tt.style.left=(e.pageX+12)+'px'; tt.style.top=(e.pageY-8)+'px';
      tt.textContent = d.brief || d.cell_id;
    })
    .on('mousemove', e => {
      document.getElementById('tooltip').style.left=(e.pageX+12)+'px';
      document.getElementById('tooltip').style.top=(e.pageY-8)+'px';
    })
    .on('mouseout', () => { document.getElementById('tooltip').style.display='none'; })
    .on('click', (e,d) => { selectedGraphId = d.id; loadGraphDetail(d.cell_id); });

  // Marching-ants selection box (classic pixel-GUI selection), tracks the
  // clicked node each tick.
  const selBox = g.append('rect').attr('class','ant-box').attr('fill','none')
    .attr('stroke', INK).attr('stroke-width', 1.5).attr('stroke-dasharray','4 3')
    .attr('pointer-events','none').style('display','none');

  // Node labels — bright + high get a name; it HUGS the node (radius-aware
  // offset set per-tick, not a fixed dy that floats loose). One-word: the
  // primary topic, underscores→spaces. Pinned nodes get a 📌.
  const shortLabel = d => {
    const t = ((d.topics||[])[0]||'').replace(/_/g,' ');
    return (d.pinned?'📌 ':'') + (t.length>16 ? t.slice(0,15)+'…' : t);
  };
  const label = g.append('g').selectAll('text')
    .data(nodes.filter(n => n.kind==='constellation' || n.significance==='bright' || n.significance==='high'))
    .join('text')
    .attr('fill', d => d.kind==='constellation' ? '#8a5a00' : INK)
    .attr('font-size', d => d.kind==='constellation' ? '12px' : '11px')
    .attr('font-family', "'Pixelify Sans', monospace")
    .attr('text-anchor','middle').attr('pointer-events','none')
    .text(d => d.kind==='constellation' ? ('✧ ' + (d.name||'constellation')) : shortLabel(d));

  // Density-adaptive layout (survives vault growth — tuned once, scales).
  // The clump failure at 101 nodes: 149 weak semantic springs pulled everyone
  // together while capped repulsion couldn't push back. Fix: weak associations
  // (semantic_sim) are LOOSE long springs, structural bonds (manual/entity/
  // topic) are tight; repulsion strengthens with node count.
  const N = nodes.length || 1;
  const chargeStr = -150 * Math.max(1, Math.sqrt(N / 60));
  const isSem = d => d.type === 'semantic_sim';
  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d=>d.id)
        .distance(d => isSem(d) ? 100 : 55 + (d.weight||1)*12)
        .strength(d => isSem(d) ? 0.02 : 0.14))
    .force('charge', d3.forceManyBody().strength(chargeStr).distanceMax(600))
    .force('center', d3.forceCenter(W/2, H/2))
    // Gentle gravity so weakly-connected cells don't drift to the void, but
    // light enough not to pile everyone onto the center.
    .force('gx', d3.forceX(W/2).strength(0.025))
    .force('gy', d3.forceY(H/2).strength(0.025))
    .force('collision', d3.forceCollide().radius(d => (SIG_R[d.significance]||9) + 7))
    .on('tick', () => {
      link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
          .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
      // paths position via transform (circles used cx/cy)
      node.attr('transform', d => `translate(${d.x},${d.y})`);
      // label sits just under the node edge, tracking exactly
      label.attr('x',d=>d.x).attr('y',d=>d.y + (isCon(d)?18:(SIG_R[d.significance]||9)) + 12);
      // marching-ants box hugs the selected node
      const sel = nodeMap[selectedGraphId] && nodes.find(n => n.id === selectedGraphId);
      if (sel) {
        const r = (SIG_R[sel.significance]||9) + 6;
        selBox.style('display',null)
          .attr('x', sel.x - r).attr('y', sel.y - r)
          .attr('width', r*2).attr('height', r*2);
      } else { selBox.style('display','none'); }
    });

  graphSim = sim;
  renderLegend();
}

function renderLegend() {
  const types = Object.entries(TYPE_COLOR);
  const edgeTypes = [['shared_entity','solid'],['shared_topic','dashed'],['manual','thick']];
  document.getElementById('legend').innerHTML =
    '<div style="color:var(--muted);font-size:10px;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em">Types</div>' +
    types.map(([t,c])=>`<div class="legend-row"><span class="leg-dot" style="background:${c}"></span><span>${t.replace('_',' ')}</span></div>`).join('') +
    '<div style="color:var(--muted);font-size:10px;margin:8px 0 6px;text-transform:uppercase;letter-spacing:.05em">Edges</div>' +
    edgeTypes.map(([t,s])=>`<div class="legend-row"><span class="leg-line" style="background:#666;opacity:.7;${s==='dashed'?'background:repeating-linear-gradient(90deg,#666 0,#666 4px,transparent 4px,transparent 7px)':''}${s==='thick'?'height:3px':''}"></span><span>${t.replace('_',' ')}</span></div>`).join('');
}

async function loadGraphDetail(cellId) {
  // Resolve a pending link (works from the graph tab too)
  if (linkSource && linkSource !== cellId) {
    const src = linkSource; linkSource = null;
    if (await curate({action:'link', a:src, b:cellId})) { toast('String linked ✓'); await refreshGraph(); }
  }
  const r = await fetch('/api/cell/'+cellId);
  const cell = await r.json();
  const edges = allEdges.filter(e=>e.a===cellId||e.b===cellId);
  const nodeMap = Object.fromEntries(allNodes.map(n=>[n.cell_id,n]));
  const neighborItems = edges.map(e=>{
    const oid = e.a===cellId?e.b:e.a;
    const o = nodeMap[oid]; if(!o) return '';
    return `<div class="neigh-item">
      <div class="neigh-type">${e.type} · w=${(e.weight||1).toFixed?(e.weight||1).toFixed(2):e.weight}
        <button class="cutb" title="sever (permanent)" onclick="severEdgeG('${cellId}','${oid}','${e.type}')">✂</button></div>
      <div class="neigh-brief" onclick="loadGraphDetail('${oid}')">${esc((o.brief||'').slice(0,90))}</div>
    </div>`;
  }).join('');
  const pinned = cell.pinned, muted = cell.muted;
  document.getElementById('graph-detail').innerHTML = `
    <div style="padding:12px 14px;border-bottom:1px solid var(--border);">
      <div style="font-size:10px;color:var(--muted);margin-bottom:4px">${cell.cell_id} · ${cell.session_date||''} · ${cell.trust||'auto'} · ref ${cell.referenced_count||0}</div>
      <div class="ci-meta"><span class="sb s-${cell.significance||'medium'}">${cell.significance||'medium'}</span><span class="tb t-${cell.semantic_type||'work_research'}">${(cell.semantic_type||'?').replace('_',' ')}</span>${cell.reflection_candidate?'<span class="rdot"></span>':''}</div>
      <div style="font-size:12px;margin-top:8px;line-height:1.5">${esc(cell.brief||'')}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:8px;line-height:1.4">${esc(cell.episode||'')}</div>
      <div style="display:flex;gap:5px;margin-top:10px;flex-wrap:wrap">
        <button class="pin-btn${pinned?' on':''}" onclick="togglePinG('${cell.cell_id}',${!pinned})">📌 pin</button>
        <button class="mute-btn${muted?' on':''}" onclick="toggleMuteG('${cell.cell_id}',${!muted})">🔇 mute</button>
        <button class="linkb" onclick="startLink('${cell.cell_id}')">＋ link →</button>
        <button class="linkb" onclick="toggleChunk('${cell.cell_id}')">👁 chunk</button>
      </div>
      <div class="chunk" id="gchunk-${cell.cell_id}" style="display:none;margin-top:10px;max-height:240px;">${esc(cell.chunk||'(not loaded)')}</div>
    </div>
    ${edges.length?`<div class="neigh"><div class="neigh-title">Connected (${edges.length})</div>${neighborItems}</div>`
      : '<div class="semp" style="padding:12px 14px">no strings yet · use ＋ link then click another node</div>'}`;
}

// Graph-tab curation variants: mutate then refresh the graph in place
async function refreshGraph() {
  const r = await fetch('/api/graph'); const d = await r.json();
  allNodes = d.nodes||[]; allEdges = d.edges||[];
  graphInited = false;
  d3.select('#graph-svg').selectAll('*').remove();
  initGraph();
}
function toggleChunk(id){ const el=document.getElementById('gchunk-'+id); if(el) el.style.display = el.style.display==='none'?'block':'none'; }
async function togglePinG(id,on){ if(await curate({action:'pin',cell_id:id,on})){toast(on?'Pinned':'Unpinned');await refreshGraph();loadGraphDetail(id);} }
async function toggleMuteG(id,on){ if(await curate({action:'mute',cell_id:id,on})){toast(on?'Muted':'Unmuted');await refreshGraph();loadGraphDetail(id);} }
async function severEdgeG(a,b,type){ if(await curate({action:'sever',a,b,type})){toast('String severed');await refreshGraph();loadGraphDetail(a);} }

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');
  const id = tab.dataset.tab;
  ['vault','quarantine','slots','graph','timeline','recall'].forEach(t => {
    const el = document.getElementById('tab-'+t);
    el.classList.toggle('hidden', t !== id);
    el.style.display = t === id ? 'flex' : '';
  });
  if (id === 'quarantine') loadQ();
  if (id === 'slots') loadSlots();
  if (id === 'graph') { el_graph_fix(); }
  if (id === 'timeline') loadTimeline();
  if (id === 'recall') setTimeout(() => document.getElementById('qtest').focus(), 50);
}));

async function runQTest() {
  const q = document.getElementById('qtest').value.trim();
  const box = document.getElementById('qtest-results');
  if (!q) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="semp">retrieving…</div>';
  const r = await fetch('/api/query?q=' + encodeURIComponent(q));
  const d = await r.json();
  const res = d.results || [];
  if (!res.length) { box.innerHTML = '<div class="semp">nothing retrieved — Q would recall nothing here</div>'; return; }
  box.innerHTML = res.map(x => `<div class="qr" onclick="switchToVault('${x.cell_id}')">
    <div><span class="qr-score">${(x.score||0).toFixed(1)}</span> · ${x.significance||'?'} · ${x.temporal_status||'?'}</div>
    <div style="font-size:12px;margin-top:3px;">${esc((x.brief||'').slice(0,140))}</div>
    <div class="qr-matched">${(x.matched||[]).join(' · ')}</div></div>`).join('');
}

function switchToVault(id) {
  document.querySelector('.tab[data-tab="vault"]').click();
  selCell(id);
}

function el_graph_fix() {
  const el = document.getElementById('tab-graph');
  el.style.display = 'flex';
  // defer so DOM has correct size
  setTimeout(() => { if (allNodes.length) initGraph(); }, 50);
}

// ── Filters ───────────────────────────────────────────────────────────────────
document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.fb'); if (!btn) return;
  document.querySelectorAll('.fb').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  currentFilter = btn.dataset.f;
  renderList();
});

// ── Recall ────────────────────────────────────────────────────────────────────
async function triggerRecall() {
  const btn = document.querySelector('.recall-btn');
  btn.textContent = '↺ Running…'; btn.disabled = true;
  const r = await fetch('/api/trigger-recall',{method:'POST'});
  const d = await r.json();
  btn.textContent = '↺ Update morning note'; btn.disabled = false;
  toast(d.ok ? 'Morning note updated ✓' : 'Failed: ' + d.stderr.slice(0,80));
}

async function closeApp() {
  if (!confirm("Close Forget-me-not?\n\nThe memory engine stops. Your memories "
    + "are safe on disk — reopen anytime by double-clicking Forget-me-not.")) return;
  try { await fetch('/api/quit', {method:'POST'}); } catch(e) {}
  document.body.innerHTML = '<div style="display:flex;height:100vh;'
    + 'align-items:center;justify-content:center;color:var(--muted);'
    + 'font-family:system-ui;font-size:16px;text-align:center">'
    + '<div><img src="/favicon.svg" width="40" style="image-rendering:pixelated"><br><br>'
    + 'Forget-me-not is closed.<br>You can close this tab.</div></div>';
}

// Live refresh: the panel should feel alive (Q remembers, the nightly runs) —
// but never yank a card out from under someone mid-edit. Poll only when no
// input is focused and the vault list is showing.
let _liveTimer = setInterval(async () => {
  const editing = ['INPUT','TEXTAREA','SELECT'].includes(
    (document.activeElement||{}).tagName);
  const vaultVisible = !document.getElementById('tab-vault').classList.contains('hidden');
  if (editing || !vaultVisible || document.hidden) return;
  try {
    const r = await fetch('/api/graph'); const d = await r.json();
    const fresh = (d.nodes||[]).filter(n => n.kind !== 'rollup').length;
    const known = allNodes.filter(n => n.kind !== 'rollup').length;
    if (fresh !== known) { await init(); toast('Memory updated ✓'); }
  } catch(e) {}
}, 8000);

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function toast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = isError ? 'var(--corr)' : 'var(--border)';
  t.style.color = isError ? 'var(--corr)' : 'var(--text)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), isError ? 5000 : 2500);
}

init();
</script>
</body>
</html>"""


# ── First-run setup (the friendly path: no terminal, no jargon) ───────────────

SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Forget-me-not · welcome</title>
<style>
  :root { --bg:#12131a; --card:#1b1d27; --ink:#e8e6f0; --muted:#8a8798;
          --accent:#7f9cf5; --soft:#2a2d3d; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink); font:15px/1.6 system-ui,
         -apple-system,'Segoe UI',sans-serif; display:flex; min-height:100vh;
         align-items:center; justify-content:center; padding:24px; }
  .card { background:var(--card); border-radius:16px; padding:36px 40px;
          max-width:560px; width:100%; box-shadow:0 8px 40px #0008; }
  h1 { font-size:22px; font-weight:600; margin-bottom:6px; }
  .sub { color:var(--muted); margin-bottom:26px; }
  label { display:block; font-size:13px; color:var(--muted); margin:16px 0 5px; }
  input { width:100%; background:var(--soft); border:1px solid #3a3d4f;
          color:var(--ink); border-radius:8px; padding:10px 12px; font-size:15px; }
  input:focus { outline:none; border-color:var(--accent); }
  .hint { font-size:12px; color:var(--muted); margin-top:4px; }
  .hint a { color:var(--accent); }
  details { margin-top:18px; }
  summary { color:var(--muted); font-size:13px; cursor:pointer; }
  button { margin-top:26px; width:100%; background:var(--accent); color:#101018;
           border:0; border-radius:10px; padding:13px; font-size:16px;
           font-weight:600; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  .quiet { font-size:12px; color:var(--muted); margin-top:16px; text-align:center; }
  .done { text-align:center; padding:30px 0; }
  .done h2 { font-size:20px; margin-bottom:10px; }
</style></head><body>
<div class="card" id="card">
  <h1>Forget-me-not 🌸</h1>
  <div class="sub">A memory for your companion, living entirely on your own
  computer. Two minutes of setup, no technical knowledge needed.</div>

  <label>Your name — the way your companion says it</label>
  <input id="h" placeholder="e.g. Sam">
  <label>Your pronouns</label>
  <input id="hp" placeholder="e.g. she/her, he/him, they/them">
  <label>Your companion's name</label>
  <input id="c" placeholder="e.g. Nova">

  <details><summary>Where should the memories live? (a folder is chosen
  for you — change it only if you care)</summary>
    <label>Memory folder</label>
    <input id="v" placeholder="">
    <div class="hint">Just a folder of ordinary text files. Back it up and
    you've backed up everything.</div>
  </details>

  <details><summary>Connect the summarizer (optional now, needed before
  memories can be written)</summary>
    <label>OpenRouter key</label>
    <input id="k" placeholder="sk-or-...">
    <div class="hint">Forget-me-not uses a small AI service to turn
    conversations into memory cards — it costs a few cents per day of chatting.
    Create a free account at <a href="https://openrouter.ai/keys"
    target="_blank">openrouter.ai/keys</a>, click "Create Key", and paste the
    long password here. It's stored on your computer only. You can also do
    this later.</div>
  </details>

  <button onclick="go()">Begin remembering</button>
  <div class="quiet">Everything stays on this machine. Nothing is ever
  deleted. Memories are sealed so they can't be secretly changed — not even
  by the app itself.</div>
</div>
<script>
async function go() {
  const h = document.getElementById('h').value.trim();
  const c = document.getElementById('c').value.trim();
  if (!h || !c) { alert('The two names are the only required part 🌸'); return; }
  const body = { h, c,
    hp: document.getElementById('hp').value.trim() || 'they/them',
    vault: document.getElementById('v').value.trim(),
    key: document.getElementById('k').value.trim() };
  const r = await fetch('/api/setup', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (!d.ok) { alert('Something went wrong: ' + (d.error||'')); return; }
  document.getElementById('card').innerHTML = `<div class="done">
    <h2>${c} can start remembering 🌸</h2>
    <p style="color:var(--muted)">Memory home: <code>${d.vault}</code></p>
    <p style="margin-top:14px">Close this tab and open Forget-me-not again —
    it will wake up as the memory panel.</p></div>`;
}
</script></body></html>"""


def _fresh_install() -> bool:
    """First run = no vault.toml AND no existing graph. An installed vault
    (like the original) never sees the setup page."""
    try:
        import fmn_config
        if fmn_config.TOML_FILE.exists():
            return False
    except Exception:
        return False
    return not GRAPH_FILE_EXISTS()


def GRAPH_FILE_EXISTS() -> bool:
    from memory_graph import GRAPH_FILE
    return GRAPH_FILE.exists()


@app.route("/setup")
def setup_page():
    return render_template_string(SETUP_HTML)


@app.route("/api/setup", methods=["POST"])
def api_setup():
    data = request.json or {}
    h, c = data.get("h", "").strip(), data.get("c", "").strip()
    if not h or not c:
        return jsonify({"ok": False, "error": "both names are required"}), 400
    hp = data.get("hp", "they/them").strip()
    vault = data.get("vault", "").strip() \
        or str(Path.home() / "Documents" / f"{c} Vault")
    try:
        import fmn_config
        fmn_config.write_config(h, hp, c, vault,
                                api_key=data.get("key", "").strip())
        return jsonify({"ok": True, "vault": vault})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    if _fresh_install():
        return render_template_string(SETUP_HTML)
    return render_template_string(HTML)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hermes Vault visualization")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Hermes Vault  →  http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
