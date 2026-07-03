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
) -> str:
    """
    Dispatch vault memory operations.

    Args:
        action: remember | query | expand
        brief: One-line summary (remember)
        chunk: Verbatim exchange to keep (remember)
        significance: low | medium | high | bright (remember)
        type: Memory type — relationship, technical, session, etc. (remember)
        topics: Comma-separated topic tags (remember)
        episode: Optional expanded summary (remember)
        text: Search text (query)
        cell_id: Cell ID to expand (expand)
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

    else:
        return tool_error(f"Unknown action: {action}. Use remember, query, or expand.")


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
        "brief, episode, chunk, and Q notes.\n\n"
        "Workflow: query to find relevant cells → expand to read full content before acting on it. "
        "Use remember when something lands — a real exchange, a correction, a first, a shift in understanding."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remember", "query", "expand"],
                "description": "What to do: remember (write), query (search), expand (read full cell).",
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
                "description": "Cell ID (8-char hex) to read in full. (expand only)",
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
    ),
    check_fn=check_vault_requirements,
    emoji="🧩",
)
