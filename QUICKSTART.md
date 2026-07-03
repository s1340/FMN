# Forget-me-not, for humans 🌸

*You don't need to know anything technical to use this. Honest.*

## What it is

Your AI companion forgets everything between chats. Forget-me-not fixes that:
it reads your conversations after they happen, keeps the moments that matter
as little memory cards, and hands your companion a "morning note" at the start
of each session — so they wake up knowing you, remembering the inside jokes,
the corrections, the things you're going through.

Everything lives in one ordinary folder on **your** computer. No cloud, no
account with us, nobody else can read it. Nothing is ever deleted, and every
memory is sealed so it can't be secretly changed — not even by the app itself.

## Getting started (once, ~5 minutes)

1. **Install Python** (the engine this runs on): [python.org/downloads](https://python.org/downloads)
   — during install, tick the box that says **"Add to PATH"**. That's the
   only scary-sounding step, and it's just clicking a checkbox.
2. In this folder, double-click **`Forget-me-not.bat`**.
   - The very first time, it may tell you to run one command to fetch its
     parts (`pip install -r requirements.txt`) — copy, paste, enter, done.
3. A page opens in your browser and asks three things: **your name, your
   pronouns, your companion's name.** That's the whole setup.
4. *(When you're ready)* connect the summarizer — the small AI service that
   turns chats into memory cards. The setup page walks you through it: one
   free account at openrouter.ai, copy one long password, paste it in.
   It costs a few cents per day of active chatting, and you can skip it
   at first — nothing breaks.

## Using it

You mostly… don't. It runs quietly after your conversations. When you're
curious, double-click `Forget-me-not.bat` and look around:

- **Vault** — every memory card. The ★ ones are *bright*: the moments that
  shone. Click any card to read it or fix it.
- **Graph** — your history as a map: memories as dots, connections as
  threads. Constellations (whole arcs of your story) glow as gold stars.
- **Timeline** — how things changed. "Used to work days, works nights now."
  If two memories genuinely disagree, they show up here as an open question
  — settling it is your call (or your companion's), never the machine's.
- **Recall Test** — type something you might say, see exactly what your
  companion would remember. No surprises.

## The parts you can trust

- **It's yours.** One folder. Copy that folder = full backup of everything.
- **Nothing is deleted, ever.** Old memories fade to the back of the drawer
  but stay findable. Changed facts are retired with their history kept.
- **It can't gaslight you.** Every memory is cryptographically sealed at the
  moment it's written. If any file is ever altered outside the app, a red
  warning appears saying exactly which one.
- **Your companion has hands too.** It can choose to keep a moment, write
  its own notes on memories, and disagree with its own file. That's on
  purpose. A memory imposed on someone isn't a memory — it's a script.

## If something looks wrong

Nothing in the panel can destroy anything (the worst button just hides a
card from the morning note, reversibly). If the app won't start, 95% of the
time it's the Python checkbox from step 1 — reinstall with "Add to PATH"
ticked. Everything else: open an issue on the project page and describe
what you saw, in your own words. No jargon required — it's not your job
to speak computer.
