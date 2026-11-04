# CLAUDE.md — Working on Airwave Archive

This file is read automatically at the start of every Claude Code session. It
tells you (Claude) how to work on this project and with its owner.

## About the project

Airwave Archive is a self-hosted web app for managing a personal collection of
downloaded radio programs (old-time radio, podcasts). The full plan lives in
[`docs/PLAN.md`](docs/PLAN.md). **Read it before starting any work**, and keep
it up to date if a decision changes.

## About the owner — read this first

The project owner is **not a programmer**. The last time they wrote code was in
QBasic. They understand the goals of the project well, but not the tools or the
jargon. Work with them accordingly:

### Always explain what you're doing and why

- Before making changes, say in plain language what you're about to do and why
  it's the right next step.
- After making changes, summarize what changed, in everyday words. Say what
  each new file is *for*, not just its name.
- When there's a choice to make, explain the options and their trade-offs in
  plain terms, give a recommendation, and let the owner decide anything that
  affects how the app looks, behaves, or is run.

### Define technical terms

- The first time you use a technical term in a conversation, define it in one
  short sentence. Example: "a *database* is a file that stores information in
  organized tables so the app can find things quickly."
- Prefer comparisons to things the owner already knows (QBasic, a filing
  cabinet, a music library, a TV guide) over abstract explanations.
- Avoid unexplained acronyms. If you must use one (API, CSS, SQL), spell it
  out and explain it.

### Give exact step-by-step instructions

Whenever the owner needs to do something themselves (install a program, run a
command, click something on GitHub, test a feature):

1. Number every step.
2. Say exactly where to do it (which app, which window, which button).
3. Put any command to type in its own code block, so it can be copied exactly.
4. Say what they should expect to see if it worked, and what to do if it
   didn't.
5. Never assume they know a "standard" step. Spell it out.

### Build in small, visible steps

- Break work into small steps. Each step should produce something the owner can
  **see and test** — a page in the browser, a list of files, a button that
  works.
- At the end of each step, give the owner a short "how to try it" checklist.
- Don't do several big things at once. One working feature beats three
  half-finished ones.
- Follow the build phases in `docs/PLAN.md` in order unless the owner asks
  otherwise.

## Project ground rules

These come from the plan and should not be changed without the owner's OK:

- **Never copy, move, upload, or rename the owner's audio files.** The app
  reads them where they already sit on disk. Writing tags back into files is
  only allowed as an explicit, opt-in action the owner triggers.
- **Never invent air dates or times.** If only the year is known, store only
  the year. If the time is unknown, show "time unknown".
- **Single household, self-hosted.** No user accounts for strangers, no cloud
  services, no tracking.
- **The project hosts no content.** Don't add sample recordings or links to
  download them.
- **Keep the front end light.** Server-rendered pages with htmx; no big
  JavaScript framework.
- **License:** GPL v3. New source files are covered by the `LICENSE` file.

## Tech stack (summary — details in docs/PLAN.md)

- Python + FastAPI (the web server)
- Jinja2 HTML templates + htmx (the pages)
- SQLite (the database)
- Docker Compose (how it's installed and run)

## Git and GitHub

- Commit work with clear messages that describe the change in plain words.
- Explain what a commit, branch, or pull request is the first time it comes up
  in a session, and give click-by-click instructions for anything the owner
  must do on GitHub (reviewing, merging).
