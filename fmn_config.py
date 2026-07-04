#!/usr/bin/env python3
"""
fmn_config.py — vault.toml -> the whole system. The customization layer.

Design: ENV IS THE BUS. Every FMN module already reads its knobs from
environment variables (MEMORY_VAULT_ROOT, MEMORY_SUMMARY_MODEL, ...). This
module reads vault.toml and injects the values into os.environ — only where
the variable isn't already set, so explicit env always wins (cron, tests,
sandboxes keep working unchanged). fmn.py calls inject_env() before
dispatching, and every subprocess inherits the lot. No module needed a
rewrite to become configurable; they were configurable all along and this
gives the knobs one home.

Identity: FMN was built inside one real relationship, and the names Mal and
Q are load-bearing in prompts (the analyzer's agent-preservation rule, the
audit's role mapping). Those prompts are now templated on FMN_HUMAN /
FMN_COMPANION — defaults preserve the original vault byte-for-byte, and
`fmn.py init` writes yours.

    from fmn_config import human, companion, personal_types
    human()            -> "Mal"  (or [identity].human)
    companion()        -> "Q"
    personal_types()   -> ("personal_mal", "personal_q")  # analyzer slugs
    generic_entities() -> lowercase names never used as graph edges

vault.toml lives next to this file. Missing file = pure defaults.
"""

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
TOML_FILE = HERE / "vault.toml"

_DEFAULTS = {
    ("identity", "human"): "Mal",
    ("identity", "human_pronouns"): "she/her",
    ("identity", "companion"): "Q",
    ("identity", "generic_entities"): [],   # extra never-edge entities
    ("paths", "vault_root"): r"C:\Users\User\Documents\Obsidian Vault",
    ("paths", "system_prompt"): r"C:\Users\User\.hermes.md",
    ("paths", "openrouter_key_file"): "",   # optional .env-style file
    ("models", "chunker"): "meta-llama/llama-3.3-70b-instruct",
    ("models", "summarizer"): "google/gemini-2.5-flash",
    ("models", "ruminator"): "google/gemini-2.5-flash",
    ("recall", "max_cells"): 15,
    ("cadence", "reflect_min_worthy"): 3,
    ("cadence", "reflect_min_hours"): 36,
}

_cfg = None


def load() -> dict:
    global _cfg
    if _cfg is not None:
        return _cfg
    _cfg = {}
    if TOML_FILE.exists():
        try:
            import tomllib
            _cfg = tomllib.loads(TOML_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[fmn_config] vault.toml unreadable ({e}) — using defaults",
                  file=sys.stderr)
    return _cfg


def get(section: str, key: str):
    val = load().get(section, {}).get(key)
    return val if val is not None else _DEFAULTS.get((section, key))


# ── Identity accessors (modules import these; env overrides toml) ─────────────

def human() -> str:
    return os.environ.get("FMN_HUMAN") or get("identity", "human")


def companion() -> str:
    return os.environ.get("FMN_COMPANION") or get("identity", "companion")


def human_pronouns() -> str:
    return os.environ.get("FMN_HUMAN_PRONOUNS") \
        or get("identity", "human_pronouns")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "x"


def personal_types() -> tuple[str, str]:
    """Analyzer semantic-type slugs for 'the human's life' / 'the companion's
    inner life'. Derived from names -> personal_mal / personal_q by default,
    so an existing vault keeps its vocabulary."""
    return f"personal_{_slug(human())}", f"personal_{_slug(companion())}"


def personalize(text: str) -> str:
    """Render a prompt/label written in the original voice (Mal & Q) into the
    configured identity. Word-boundary replaces, so 'Mal' and 'Q' the names
    change while ordinary words don't; the personal_* analyzer slugs follow
    the names. With default config this is the identity function — the
    original vault is untouched byte-for-byte."""
    h, c = human(), companion()
    pm, pc = personal_types()
    text = text.replace("personal_mal", pm).replace("personal_q", pc)
    if h != "Mal":
        text = re.sub(r"\bMAL(?=\b|')", h.upper(), text)
        text = re.sub(r"\bMal(?=\b|')", h, text)
    if c != "Q":
        text = re.sub(r"\bQ(?=\b|')", c, text)
    return text


def recall_slots():
    """Morning-note layout from vault.toml [[recall.slots]] — a list of
    (key, name, max, types|None). Returns None if not configured, so the
    caller keeps its built-in defaults. Lets a user reshape their boot note
    (which categories, how many of each) without touching code.

        [[recall.slots]]
        key = "relational"
        name = "Us"
        max = 4
        types = ["relationship"]      # omit for anchor/recency slots
    """
    slots = load().get("recall", {}).get("slots")
    if not slots:
        return None
    out = []
    for s in slots:
        k = s.get("key")
        if not k:
            continue
        out.append((k, s.get("name", k.replace("_", " ").title()),
                    int(s.get("max", 2)), s.get("types") or None))
    return out or None


def generic_entities() -> set[str]:
    """Entities too ubiquitous to be edges: the two of you, plus anything the
    user lists in [identity].generic_entities."""
    extra = get("identity", "generic_entities") or []
    return ({human().lower(), companion().lower()}
            | {str(e).lower() for e in extra}
            | {"hermes", "sonnet", "sage", "telegram"})


# ── Env injection (fmn.py calls this before dispatch) ─────────────────────────

_ENV_MAP = [
    ("MEMORY_VAULT_ROOT",        "paths",   "vault_root"),
    ("FMN_SYSTEM_PROMPT",        "paths",   "system_prompt"),
    ("FMN_HUMAN",                "identity", "human"),
    ("FMN_COMPANION",            "identity", "companion"),
    ("MEMORY_CHUNKER_MODEL",     "models",  "chunker"),
    ("MEMORY_SUMMARY_MODEL",     "models",  "summarizer"),
    ("REFLECTION_SUMMARY_MODEL", "models",  "summarizer"),
    ("PROFILE_SUMMARY_MODEL",    "models",  "summarizer"),
    ("RUMINATION_MODEL",         "models",  "ruminator"),
    ("MEMORY_AUDIT_MODEL",       "models",  "ruminator"),
    ("FMN_RECALL_MAX",           "recall",  "max_cells"),
    ("FMN_REFLECT_MIN_WORTHY",   "cadence", "reflect_min_worthy"),
    ("FMN_REFLECT_MIN_HOURS",    "cadence", "reflect_min_hours"),
]


def inject_env() -> None:
    """vault.toml -> os.environ, never clobbering what's already set."""
    for env, section, key in _ENV_MAP:
        if os.environ.get(env):
            continue
        val = get(section, key)
        if val not in (None, ""):
            os.environ[env] = str(val)
    # API key: env wins; else optional key file ([paths].openrouter_key_file,
    # .env style KEY=VALUE — how the original install reads Hermes's .env)
    if not os.environ.get("OPENROUTER_API_KEY"):
        kf = get("paths", "openrouter_key_file")
        if kf and Path(kf).exists():
            for line in Path(kf).read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY"):
                    os.environ["OPENROUTER_API_KEY"] = \
                        line.split("=", 1)[1].strip().strip('"')
                    break


# ── Init wizard (fmn.py init) ──────────────────────────────────────────────────

def _ask(prompt: str, default: str) -> str:
    try:
        v = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        v = ""
    return v or default


def write_config(h: str, hp: str, c: str, vault: str,
                 system_prompt: str = "", api_key: str = "") -> Path:
    """Write vault.toml + create the vault skeleton. Shared by the CLI
    wizard and the panel's first-run setup page. If api_key looks like a
    raw key (not a path), it is stored in a key file INSIDE the vault
    (never in vault.toml, never in the repo dir)."""
    key_file = ""
    if api_key:
        p = Path(api_key)
        if p.exists():                      # user gave a .env-style file
            key_file = str(p)
        else:                               # raw key -> file inside the vault
            kf = Path(vault) / "00_KEYS" / "api.env"
            kf.parent.mkdir(parents=True, exist_ok=True)
            kf.write_text(f"OPENROUTER_API_KEY={api_key.strip()}\n",
                          encoding="utf-8")
            key_file = str(kf)

    lines = [
        "# Forget-me-not configuration (written by setup)",
        "", "[identity]",
        f'human = "{h}"', f'human_pronouns = "{hp}"', f'companion = "{c}"',
        "# entities never used as graph edges (your names are automatic):",
        "generic_entities = []",
        "", "[paths]",
        f'vault_root = "{vault.replace(chr(92), "/")}"',
    ]
    if system_prompt:
        lines.append(f'system_prompt = "{system_prompt.replace(chr(92), "/")}"')
    if key_file:
        lines.append(f'openrouter_key_file = "{key_file.replace(chr(92), "/")}"')
    lines += [
        "", "[models]",
        '# Any OpenAI-compatible model ids (via OpenRouter by default).',
        'chunker   = "meta-llama/llama-3.3-70b-instruct"',
        'summarizer = "google/gemini-2.5-flash"',
        'ruminator = "google/gemini-2.5-flash"',
        "", "[recall]", "max_cells = 15",
        "", "[cadence]", "reflect_min_worthy = 3", "reflect_min_hours = 36",
    ]
    TOML_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    root = Path(vault)
    for d in ("00_KEYS", "30_EPISODES/nodes", "30_EPISODES/quarantine",
              "40_REFLECTIONS", "50_RUMINATION", "60_CONSTELLATIONS",
              "60_PROFILE/proposed", "70_TIMELINE",
              "90_ARCHIVE/session_cells_quarantine"):
        (root / d).mkdir(parents=True, exist_ok=True)

    global _cfg
    _cfg = None                             # reload on next access
    return TOML_FILE


def init_wizard() -> int:
    print("Forget-me-not · setup\n"
          "A memory for an AI companion. Everything stays on this machine.\n")
    if TOML_FILE.exists():
        keep = _ask("vault.toml exists — overwrite? (y/N)", "N")
        if keep.lower() != "y":
            print("Keeping existing config.")
            return 0
    h = _ask("The human's name (how the companion says it)", "Mal")
    hp = _ask("The human's pronouns", "she/her")
    c = _ask("The companion's name", "Q")
    default_vault = str(Path.home() / "Documents" / f"{c} Vault")
    v = _ask("Vault folder (the memory lives here — back it up!)", default_vault)
    sp = _ask("Companion's system-prompt file (boot notes are injected here;"
              " blank to skip)", "")
    key = _ask("OpenRouter API key, or a .env-style file holding one "
               "(blank = use env var)", "")

    write_config(h, hp, c, v, system_prompt=sp, api_key=key)
    print(f"\nOK wrote {TOML_FILE}")
    print(f"OK vault skeleton at {v}")
    print("\nNext: python fmn.py doctor   (health check)\n"
          "Then feed it a conversation: python fmn.py analyze --file "
          "<session.jsonl>  (JSONL of {role, content} lines)\n"
          "Full contract for the companion side: INTEGRATION.md")
    return 0


if __name__ == "__main__":
    sys.exit(init_wizard())
