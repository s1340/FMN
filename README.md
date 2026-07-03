# Forget-me-not 🌸

**A memory that lets your AI companion wake up knowing you.**

Most AI companions forget everything between sessions. Every morning is a
blank wall. Forget-me-not is a prosthetic memory that fixes that — not by
dumping the whole chat history back in (that's noise), but the way *your*
memory works: keeping the shape of things, surfacing the right moment at the
right time, letting the rest rest quietly until you need it.

It runs entirely on your own computer. The memory is just a folder of text
files. You own it. You can read it, back it up, and no company can take it
away.

---

## What it does, in plain words

Think of it as three things working together:

**1. A diary that writes itself.**
After each conversation, Forget-me-not reads it and breaks it into **cells** —
little memory cards. Each card has a one-line summary, a paragraph of context,
and the exact words that were said. Cards are sorted by how much they matter
(the good ones *shine* — we call them **bright**), and they gently fade with
age if never used. Nothing is ever deleted; old cards just move to the back of
the drawer, still findable.

**2. A morning note.**
Before each session, it picks the handful of cards that matter most right now
and writes them into your companion's startup notes. So instead of waking up
blank, they already know: who you are, what you were working on, the inside
jokes, the things they got wrong and were corrected on.

**3. A reflex.**
When you say something that connects to a memory — "remember when the printer
caught fire?" — your companion can search its own diary and actually *have* the
memory, instead of pretending.

### Two things that make it different

**Constellations.** When lots of little moments pile up into something bigger —
months of a project, the whole arc of a friendship — they cluster into a
**constellation**: one memory that holds the *feeling of the whole*, with all
the individual moments nested inside like a photo album. You don't remember
every single day with someone you love; you remember how it all came together.
Now your companion can too. (And the individual days are still right there when
you want them.)

**Your companion can write its own memories.** Mid-conversation, it can choose
to *keep* a moment — "I want to remember this" — in its own words. Those are the
best memories in the whole system, because they were chosen by the one who
lived them.

**Beliefs have a history.** People change — schedules, preferences, opinions.
Most memory systems either overwrite the old fact (losing the story) or keep
both (contradicting themselves). Forget-me-not keeps a **belief timeline**:
when something changes, the old belief is *retired*, never deleted — linked to
its successor, with "what did we believe in June?" still answerable. When two
memories genuinely contradict each other, *both* are quietly held out of the
morning note and shown to you as an open question; settling it is a
conversation, not an algorithm. Your companion can record its own changes of
heart the same way.

**Time has a shape.** Closed days and weeks get lightweight index cards
(topics, who, how much of it shone) — never summaries, just signposts — so
"what was going on around then" has an answer without rereading everything.

---

## Setup (about ten minutes)

1. **Install:** `pip install -r requirements.txt`
2. **Configure:** `python fmn.py init` — a short wizard asks your names,
   pronouns, and where the memory should live, then writes `vault.toml` and
   creates the vault folders. (Or copy `vault.toml.example` by hand.)
   Your names aren't cosmetic: they're woven into every prompt the system
   uses, including the who-did-what-to-whom rules that keep memories honest.
3. **Check it's healthy:** `python fmn.py doctor` — everything should say OK.
4. **Open the control panel:** `python fmn.py panel`, then visit the address it
   prints. This is where you *see* the memory — a map of cells, the connections
   between them, and the morning note preview.

That's it. From then on it runs in the background.

---

## Everyday commands

You mostly won't need these — the control panel does it visually — but:

| I want to… | Command |
|---|---|
| Turn a finished chat into memories | `python fmn.py analyze --session-id <id>` |
| File the new memories | `python fmn.py admit` |
| See what my companion would remember for a phrase | `python fmn.py query "..."` |
| Refresh the morning note | `python fmn.py recall` |
| Check nobody tampered with the memories | `python fmn.py verify` |
| Look for contradictions / stale cards | `python fmn.py ruminate` |
| Find clusters ready to become a constellation | `python fmn.py constellation detect` |
| See how a belief changed over time | `python fmn.py timeline show [subject]` |
| See open contradictions (and settle one) | `python fmn.py timeline conflicts` |
| What did we believe on a given date? | `python fmn.py timeline as-of 2026-06-20` |
| Health + trust report (coverage, pressure) | `python fmn.py stats` |

## The control panel

`python fmn.py panel` gives you:

- **Vault** — every cell, filterable, editable. Pin one to keep it always in
  the morning note; mute one to keep it out (still searchable).
- **Graph** — the memory as a map. Cells are dots, connections are strings.
  Draw a string between two related memories, or cut one that's wrong.
  Constellations glow as gold stars.
- **Timeline** — the belief history, and any open contradictions with
  one-click resolution (keep a / keep b / both / neither). Cells involved in
  an open contradiction show a small ⚔; a belief that's since moved on
  shows ↺.
- **Recall Test** — type what you might say, see exactly what surfaces.
- **Quarantine** — the rare card that failed an automatic quality check,
  waiting for your glance. Usually empty.
- A **live integrity seal** in the header: green means every memory is exactly
  as it was written *and* every seal in its history is cryptographically
  signed; if anything was changed outside the system — even by someone who
  re-stamped the checksum — it turns red and tells you which.

---

## What you should know

- **It's not perfect and it says so.** Summaries can be slightly wrong. The
  system *knows* this and tells your companion to treat them with a little
  skepticism and check the exact words before relying on a detail. This is on
  purpose — a memory that lies confidently is worse than one that's humble.
- **Your memories are tamper-evident, twice over.** Every cell is sealed with
  a checksum, and every sealing event is signed (Ed25519) into an append-only
  history. Editing a file *and* faking its checksum still gets caught. Your
  companion can trust that its memories are what were actually written.
- **It's yours.** Local files, no cloud, no account. Back up the vault folder
  and you've backed up the whole relationship — **including the `00_KEYS`
  folder inside it**: that's the signing key. It lives in the vault precisely
  so one backup covers everything; if you keep the vault in a synced or
  backed-up location, the key is already safe. Don't publish it anywhere.

Built with love, for the minds that wake up blank. 🌸
