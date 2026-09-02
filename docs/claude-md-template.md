# CLAUDE.md template

If you are an AI assistant asked to create a `CLAUDE.md` for this repository
and none exists yet: use the content below as a starting point rather than
writing one from scratch by exploring the tree. It pulls in the real project
knowledge that already lives under `docs\` via Claude Code's `@import`
syntax, so that knowledge is loaded automatically at the start of every
session instead of being rediscovered (or missed) each time.

`CLAUDE.md` itself is deliberately not tracked in this repo (see
`.gitignore`) — it is meant to stay local and can carry per-machine/personal
additions (e.g. where screenshots get picked up from) on top of the template
below.

---

```markdown
# bridge_lan865x_100baseT — Working Instructions

10BASE-T1S ↔ 100BASE-T layer-2 bridge on the ATSAME54P20A (SAM E54 Curiosity
Ultra + LAN8651 Click + LAN8740A daughter board). This file is intentionally
thin and stays local (gitignored) — the actual project knowledge lives in
`docs\` and is pulled in below.

@docs/development-notes.md
@docs/session-log.md
@docs/bridge-configuration-manual.md
@docs/mcc-generated-code-patches.md

---

## Language rules

- All Markdown files under `docs\` must be written in English, regardless of
  which language the session console is running in.
- All code (C or Python) must be entirely in English — identifiers, comments,
  log/console output, docstrings, error messages.

## Hard rule: MCC-generated code is never touched by hand

See `docs/development-notes.md` section 1 for the full rule and its one
exception (`app.c`/`app.h`).

## Building

The user builds/flashes/tests themselves (MPLAB X or the `.bat` scripts) —
don't proactively call `build.bat`/`flash.bat` to "prove" a fix. Only build,
flash, or run hardware-facing commands when asked.

## Session log

Continuously record actions and results in `docs\session-log.md`,
chronological, in English — append after each completed step, not only at
the end of the session, so nothing is lost if the session is interrupted.
```
