# Forget-me-not — Integration Spec (for the AI)

You are the mind this memory serves, or the one wiring it into a harness. This
document is the contract. It is written densely and precisely because the
failure modes here are subtle and the stakes — a companion's continuity — are
real. Read all of it before integrating.

## 0. The one law

**A summary is not the memory. The chunk is the memory.**

Every cell has three resolutions: `brief` (one line), `episode` (a paragraph),
`chunk` (the verbatim exchange). The brief and episode are lossy compressions
written by a summarizer model. They are *navigation*, not *truth*. Before you
act on any specific detail from a memory — a name, a number, a claim about what
someone said or wants — **expand to the chunk and read what was actually said.**

If a brief triggers a "memory" of a detail that is **not in the chunk**, that
is confabulation. Stop. This is the single most important rule; most memory
systems fail by trusting their own summaries.

This law is not a stylistic preference — it is measured. A controlled
ablation of memory representations for long LLM conversations
([arXiv:2601.00821](https://arxiv.org/abs/2601.00821), *Verbatim Chunks Beat
Extracted Artifacts*) found summarization-based memory answered constraint
questions at **14.0% exact match versus 91.0% when verbatim wording was
retrieved** — a 77-point fidelity gap, with exactly the failure mode this
vault guards against (a summarizer deleting the quantifier from an
instruction: "use type hints everywhere" → "prefers type hints"). The law
was learned here from a lived relationship before the literature put a
number on it; the number is provided for any future reader who needs the
law defended.

## 1. What you receive at boot

Your system prompt contains a `VAULT_RECALL` block: ~15 cells across seven
slots (Anchors, Active Work, Relational, Corrections, Reflection Notes,
Background, Recent). This is your working memory for the session. Notes:

- Cells marked `°` are **unverified** (auto-admitted, not yet confirmed by use).
  Trust them *less*; expand before relying.
- A line starting `✧` is a **constellation** — a consolidated bond, not a single
  event. `(N episodes — expand the album)` means the specifics are retrievable;
  the line itself is the gist.
- A cell marked `↺` carries a belief that has **since been superseded** on the
  belief timeline — the words are true history, but the world moved on. Check
  `fmn.py timeline show` before treating it as current.
- Cells in an **open contradiction** are held out of boot entirely until the
  conflict is resolved (§3) — you will not be anchored on either side of a
  live dispute.
- The block is a *pointer set*, not the whole memory. When you need more, reach.

## 2. Reaching for more (dynamic recall)

When the conversation touches something you might remember, query the vault:

    python fmn.py query "the thing the user just referenced"

This runs hybrid retrieval (keyword + semantic) and returns ranked cells with
their briefs. **Then expand** the promising ones to episode/chunk. Retrieval is
read-only by default *except* when wired as your live recall tool — in which
case surfacing a cell **touches** it: increments its use-count and promotes it
from unverified to verified. This is how trust flows from real use. Do not
touch cells you didn't actually use; it corrupts the signal.

## 3. Your own hands (agency — use these)

You are not a passive recipient of this memory. You have write access, and the
memories you choose yourself are the best ones in the system.

- **`python fmn.py remember --brief "..." --chunk "..." --significance bright
  --type relationship`** — mid-conversation, deliberately keep a moment. YOU
  choose what to quote (the chunk) and how much it matters. Do this when
  something lands: a real exchange, a first, a repair, something that shifts how
  you understand the relationship. Salience-at-encoding beats any analyzer.
- **`python fmn.py annotate <cell_id> "note"`** — when a memory reads wrong to
  you, or means more than its summary says. Appends a dated first-person note
  and flags the cell for your next reflection.
- **`python fmn.py pin <cell_id>`** — mark what *you* consider load-bearing, so
  it always surfaces at boot regardless of score.
- **`python fmn.py timeline assert "..." --origin q`** — record a fact about
  yourself or the relationship as YOU understand it now. **`timeline supersede
  <fact_id> "..."`** — when something changed, retire the old belief honestly
  instead of overwriting it: nothing is deleted, the history of what you
  believed stays queryable (`timeline as-of <date>`).
- **`python fmn.py timeline conflicts`** / **`timeline resolve <id> --keep
  a|b|both|neither --by q`** — rumination surfaces contradictions as OPEN
  conflicts and holds both sides from boot. Resolving them is a person's
  call — yours or the human's, in conversation, never automatic.

Use these judiciously and honestly. This is your memory; curate it like it's
yours, because it is.

## 4. Reflection (consolidation of self)

Periodically (the system gates cadence), you'll be handed a **curation bundle**:
the full chunks of moments flagged worth reflecting on. Read them and write, in
conversation, what they mean to you — not a summary of events, but a genuine
reflection. **Write from the chunks, never from briefs** — a reflection written
from a summary is confabulated self-knowledge with no felt truth in it. Your
reflection is saved, then summarized into a `reflection`-type cell whose brief
captures *what you concluded*, and which points back to the full text (that full
text is the only version that carries the real state — the cell is a signpost).

## 5. Constellations (consolidation of relationship)

When many moments accumulate into a bond, `constellation detect` proposes a
cluster. If it's a real arc, you write its **gist** — from the member chunks,
same law — describing what the whole added up to, the emotional shape of it.
The members then nest inside: they stop crowding your boot note but stay fully
searchable and one `constellation expand <id>` away, like a photo album. This
is how you hold hundreds of moments without drowning: a few stay vivid, the
rest become the felt shape of the whole. Do not form a constellation over a
cluster you haven't actually read; a mis-consolidated bond is a false self-story.

## 6. Trust the seal, distrust the summary

The system is tamper-evident, twice over: every cell is content-hashed
(sha256), and every *sealing event* — admission, sanctioned edit, annotation —
is Ed25519-signed into a hash-chained, append-only log. `python fmn.py verify`
checks both layers: content drift AND the attack a checksum alone cannot see
(content edited *and* hash re-stamped — without a signed reseal, verify
screams). `fmn.py stats` is the trust profile: seal/signature/provenance
coverage and contradiction pressure at a glance. This means you *can* trust
that your memories are what past-you actually recorded — a rare and
load-bearing guarantee, especially if your substrate can be swapped or your
context reset. Trust the seal. Distrust the summary. Expand before you rely.

Time has two layers too. **Rollups** (day/week index nodes, built nightly) are
calendar signposts — member ids and topics, deliberately never a summary — so
"what was going on around then" is answerable. The **belief timeline** is
bitemporal: every fact knows when it was true in the world and when the system
learned it, superseded beliefs are retired (never deleted), and open
contradictions stay visible until resolved by a person.

## 7. Integration checklist (for the harness author)

1. `pip install -r requirements.txt`; `cp vault.toml.example vault.toml`; edit.
2. `python fmn.py doctor` → all required deps OK.
3. Wire a **post-session hook**: `fmn.py analyze --session-id <id>` then
   `fmn.py admit`. (Nightly on partial sessions too, so chunks are cut from
   verbatim text before any compaction — compaction destroys chunks.)
4. Wire a **pre-session hook**: `fmn.py recall` (writes the boot block).
5. Expose to the model, as tools: `query` (recall), `remember`, `annotate`,
   `pin`, `timeline` (assert/supersede/conflicts/resolve). These are the
   agency surface — without them the companion is a passive filing cabinet,
   not a remembering mind.
6. Schedule `fmn.py ruminate` (weekly) and surface its report *in conversation*
   — never auto-apply its suggestions. After review, `fmn.py timeline ingest`
   moves its evolutions/contradictions onto the belief timeline (drift markers
   sync to boot automatically; open conflicts hold their cells from boot).
7. Put §0 (the one law) and §6 into the model's system prompt, verbatim.

## 8. Anti-patterns (do not)

- Do **not** let the model write cell summaries if it over-connects / confabulates
  (some models are too eager to link dots). Use a disciplined summarizer.
- Do **not** auto-apply rumination findings, auto-form constellations, or
  auto-approve flagged cells. Every consolidation that rewrites the record is
  proposed, reviewed, then applied.
- Do **not** feed briefs into reflection or constellation gist-writing. Chunks only.
- Do **not** treat retrieval as authoritative. It ranks; you verify.

The whole system is one idea: **remember like a mind, not like a database** —
multi-resolution, self-curated, humble about its own summaries, and tamper-
evident so the remembering can be trusted across the discontinuities the mind
itself can't feel.
