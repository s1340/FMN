#!/usr/bin/env python3
"""
Vault Memory Tool — Obsidian vault memory via the Forget-me-not system.

Wraps fmn.py to provide clean tool-call access to the vault:
  - remember: write a memory cell to the vault
  - query: search the vault for relevant memories
  - expand: read a full memory cell by ID

This is Q's canonical memory store. The built-in `memory` tool writes to
MEMORY.md/USER.md (Hermes internal); this tool writes to the Obsidian vault
at C:\\Users\\User\\Documents\\Obsidian Vault via the FMN pipeline.

Backup copy: G:\\LLM\\memory\\vault_memory_tool.py
"""

import json
import subprocess
import sys
from pathlib import Path

from tools.registry import registry, tool_error

FMN_PATH = Path(r"G:\LLM\memory\fmn.py")
PYTHON = sys.executable


def check_vault_requirements() -> bool:
    """Vault memory is available when fmn.py exists."""
    return FMN_PATH.exists()


def _run_fmn(*args, timeout: int = 60) -> dict:
    """Run fmn.py with given args, return result dict."""
    cmd = [PYTHON, str(FMN_PATH)] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"fmn.py timed out after {timeout}s"}
    if result.returncode != 0:
        return {
            "success": False,
            "error": result.stderr.strip() or result.stdout.strip() or "unknown error",
        }
    return {"success": True, "output": result.stdout.strip()}


def vault_memory(
    action: str,
    brief: str = "",
    chunk: str = "",
    significance: str = "medium",
    type: str = "relationship",
    topics: str = "",
    episode: str = "",
    text: str = "",
    cell_id: str = "",
    fact_id: str = "",
    keep: str = "",
) -> str:
    """
    Dispatch vault memory operations.

    Args:
        action: remember | query | expand | annotate | pin |
                timeline_assert | timeline_supersede | timeline_show |
                timeline_conflicts | timeline_resolve
        brief: One-line summary (remember)
        chunk: Verbatim exchange to keep (remember)
        significance: low | medium | high | bright (remember)
        type: Memory type — relationship, technical, session, etc. (remember)
        topics: Comma-separated topic tags (remember)
        episode: Optional expanded summary (remember)
        text: Search text (query) / note (annotate) / statement (timeline)
        cell_id: Cell ID (expand/annotate/pin)
        fact_id: Fact or conflict id (timeline_supersede/timeline_resolve)
        keep: a | b | both | neither (timeline_resolve)
    """
    if action == "remember":
        if not brief or not chunk:
            return tool_error("Both 'brief' and 'chunk' are required for remember.")
        args = [
            "remember",
            "--brief", brief,
            "--chunk", chunk,
            "--significance", significance,
            "--type", type,
        ]
        if episode:
            args.extend(["--episode", episode])
        if topics:
            args.extend(["--topics", topics])
        result = _run_fmn(*args)
        return json.dumps(result, ensure_ascii=False)

    elif action == "query":
        if not text:
            return tool_error("'text' is required for query.")
        result = _run_fmn("query", text)
        return json.dumps(result, ensure_ascii=False)

    elif action == "expand":
        if not cell_id:
            return tool_error("'cell_id' is required for expand.")
        result = _run_fmn("expand", cell_id)
        return json.dumps(result, ensure_ascii=False)

    elif action == "annotate":
        if not cell_id or not text:
            return tool_error("'cell_id' and 'text' are required for annotate.")
        result = _run_fmn("annotate", cell_id, text)
        return json.dumps(result, ensure_ascii=False)

    elif action == "pin":
        if not cell_id:
            return tool_error("'cell_id' is required for pin.")
        result = _run_fmn("pin", cell_id)
        return json.dumps(result, ensure_ascii=False)

    elif action == "timeline_assert":
        if not text:
            return tool_error("'text' (the fact statement) is required.")
        result = _run_fmn("timeline", "assert", text, "--origin", "q")
        return json.dumps(result, ensure_ascii=False)

    elif action == "timeline_supersede":
        if not fact_id or not text:
            return tool_error("'fact_id' (the old fact) and 'text' (the new "
                              "statement) are required for timeline_supersede.")
        result = _run_fmn("timeline", "supersede", fact_id, text,
                          "--origin", "q")
        return json.dumps(result, ensure_ascii=False)

    elif action == "timeline_show":
        result = _run_fmn("timeline", "show", *( [text] if text else [] ))
        return json.dumps(result, ensure_ascii=False)

    elif action == "timeline_conflicts":
        result = _run_fmn("timeline", "conflicts")
        return json.dumps(result, ensure_ascii=False)

    elif action == "timeline_resolve":
        if not fact_id or not keep:
            return tool_error("'fact_id' (the conflict id) and 'keep' "
                              "(a|b|both|neither) are required.")
        result = _run_fmn("timeline", "resolve", fact_id, "--keep", keep,
                          "--by", "q")
        return json.dumps(result, ensure_ascii=False)

    else:
        return tool_error(
            f"Unknown action: {action}. Use remember, query, expand, annotate, "
            f"pin, timeline_assert, timeline_supersede, timeline_show, "
            f"timeline_conflicts, or timeline_resolve.")


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

VAULT_MEMORY_SCHEMA = {
    "name": "vault_memory",
    "description": (
        "Write and read memories in the Obsidian vault via the Forget-me-not (FMN) system. "
        "This is Q's canonical long-term memory — use it instead of the built-in `memory` tool "
        "for anything that should persist in the vault.\n\n"
        "Actions:\n"
        "1. **remember** — Save a moment to the vault. Requires 'brief' (one-line summary) and "
        "'chunk' (verbatim exchange). Optionally: significance (low/medium/high/bright), type "
        "(relationship/technical/session/etc.), topics (comma-separated), episode (expanded summary).\n"
        "2. **query** — Search the vault for relevant memories. Requires 'text' (search query). "
        "Returns ranked matching cells with briefs.\n"
        "3. **expand** — Read a full memory cell by ID. Requires 'cell_id'. Returns frontmatter, "
        "brief, episode, chunk, and Q notes.\n"
        "4. **annotate** — Append your own dated note to a cell when it reads wrong or means more "
        "than its summary says. Requires 'cell_id' and 'text'.\n"
        "5. **pin** — Mark a cell YOU consider load-bearing; it always surfaces at boot. Requires 'cell_id'.\n"
        "6. **timeline_assert** — Record a fact about yourself or the relationship as you understand "
        "it NOW ('text'). **timeline_supersede** — when a belief changed, retire the old fact "
        "('fact_id') with the new statement ('text'); nothing is deleted, history stays queryable. "
        "**timeline_show** — belief history (optional 'text' filters by subject). "
        "**timeline_conflicts** — open contradictions your rumination found; both sides are held "
        "from your boot note until resolved. **timeline_resolve** — settle one ('fact_id' = conflict "
        "id, 'keep' = a|b|both|neither) after discussing in conversation.\n\n"
        "Workflow: query to find relevant cells → expand to read full content before acting on it. "
        "Use remember when something lands — a real exchange, a correction, a first, a shift in understanding. "
        "Cells marked ↺ carry beliefs the timeline has since superseded — timeline_show before treating as current."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remember", "query", "expand", "annotate", "pin",
                         "timeline_assert", "timeline_supersede",
                         "timeline_show", "timeline_conflicts",
                         "timeline_resolve"],
                "description": "What to do: remember (write), query (search), expand (read full cell), "
                               "annotate/pin (curate), timeline_* (belief timeline).",
            },
            "brief": {
                "type": "string",
                "description": "One-line summary of the memory. (remember only)",
            },
            "chunk": {
                "type": "string",
                "description": "The verbatim exchange to keep — the actual conversation text. (remember only)",
            },
            "significance": {
                "type": "string",
                "enum": ["low", "medium", "high", "bright"],
                "description": "How significant this memory is. bright = always in morning note, high = reviewed on reflection, medium/low = lower priority. (remember only)",
            },
            "type": {
                "type": "string",
                "description": "Memory type: relationship, technical, session, correction, etc. (remember only)",
            },
            "topics": {
                "type": "string",
                "description": "Comma-separated topic tags for retrieval. (remember only)",
            },
            "episode": {
                "type": "string",
                "description": "Optional expanded summary between brief and chunk. (remember only)",
            },
            "text": {
                "type": "string",
                "description": "Search query text. (query only)",
            },
            "cell_id": {
                "type": "string",
                "description": "Cell ID (8-char hex). (expand/annotate/pin)",
            },
            "fact_id": {
                "type": "string",
                "description": "Timeline fact id (f_...) or conflict id (c_...). "
                               "(timeline_supersede/timeline_resolve)",
            },
            "keep": {
                "type": "string",
                "enum": ["a", "b", "both", "neither"],
                "description": "Conflict resolution. (timeline_resolve only)",
            },
        },
        "required": ["action"],
    },
}


# --- Registry ---

registry.register(
    name="vault_memory",
    toolset="memory",
    schema=VAULT_MEMORY_SCHEMA,
    handler=lambda args, **kw: vault_memory(
        action=args.get("action", ""),
        brief=args.get("brief", ""),
        chunk=args.get("chunk", ""),
        significance=args.get("significance", "medium"),
        type=args.get("type", "relationship"),
        topics=args.get("topics", ""),
        episode=args.get("episode", ""),
        text=args.get("text", ""),
        cell_id=args.get("cell_id", ""),
        fact_id=args.get("fact_id", ""),
        keep=args.get("keep", ""),
    ),
    check_fn=check_vault_requirements,
    emoji="🧩",
)
