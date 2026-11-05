# CLAUDE.md — Working on Airwave Archive

This file is read automatically at the start of every Claude Code session. It
tells you (Claude) how to work on this project and with its owner.

## About the project

Airwave Archive is a self-hosted web app for managing a personal collection of
downloaded radio programs (old-time radio, podcasts). The full plan lives in
[`docs/PLAN.md`](docs/PLAN.md). **Read it before starting any work**, and keep
it up to date if a decision changes.

## Guiding principle: a personal project

The owner is building Airwave Archive **for themselves**. The GitHub
repository is **private** for now. If it's ever shared, others are welcome to
use it under the GPL v3, but the owner is **not running a
community project**. In practice:

- Make decisions that suit the owner's setup and needs. Don't add options,
  settings, or support for other platforms "in case someone wants them."
- Don't add community scaffolding: contribution guides, issue/PR templates,
  codes of conduct, release processes, changelogs for outsiders, badges.
- Public docs (README, INSTALL) should be accurate and usable, but needn't
  cover every setup or promise support.

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

- **Never move, rename, or change files in the library.** The app reads
  recordings where they sit. The only way files enter the library is when the
  owner approves uploads from the **inbox** (`/srv/airwave/inbox`). Writing
  tags back into files is only allowed as an explicit, opt-in action.
  Exports (PLAN.md section 5a) write tags into **copies** only, never the
  originals.
- **Never invent air dates or times.** If only the year is known, store only
  the year. If the time is unknown, show "time unknown".
- **Single household, self-hosted.** No user accounts for strangers, no cloud
  services, no tracking.
- **The project hosts no content.** Don't add sample recordings or links to
  download them.
- **Keep the front end light.** Server-rendered pages with htmx; no big
  JavaScript framework.
- **Keep it easy to package for Windows and Mac later.** Folder locations
  and settings come from configuration (environment variables or a
  settings page), never hard-coded Linux paths. Avoid Linux-only
  dependencies. Docker is the main way to run it, but not the only one.
- **License:** GPL v3. New source files are covered by the `LICENSE` file.

## Tech stack (summary — details in docs/PLAN.md)

- Python + FastAPI (the web server)
- Jinja2 HTML templates + htmx (the pages)
- SQLite (the database), via SQLAlchemy, with indexes, WAL mode, and FTS5
  search, so it stays fast for very large libraries
- Docker Compose (how it's installed and run)

## Project layout and commands

- `app/main.py`: the web app (pages and routes)
- `app/db.py`: the database tables (SQLite file `airwave.db` in the data
  folder, `AIRWAVE_DATA`). Changing the layout means bumping
  `SCHEMA_VERSION` and adding a step to `_migrate` (a backup copy is made
  automatically). Never just delete the owner's database: it holds edits.
- `app/episodes.py`: episode fields, edit-form checks, air-date display,
  filling in from tags, saving confirmed guesses
- `app/filenames.py`: guesses series/date/title from file names (for the
  review page)
- `app/parts.py`: episodes made of several files ("Hour 1", "Hour 2"):
  grouping, splitting, numbering the parts
- `app/series.py`: the series list (with totals and pictures) and one
  series' episodes in air-date order
- `app/artwork.py`: finds pictures in files and folders, and keeps small
  copies in the data folder (`artwork/`)
- `app/bulk.py`: editing many episodes at once, and renaming a series
- `app/csvimport.py`: importing episode logs from CSV spreadsheets
  (reading, matching rows to episodes, preview, saving)
- `app/inbox.py`: the upload inbox (saving uploads safely, listing what
  waits, guessing details, rejecting, adding approved files to the
  library next to their series)
- `app/search.py`: the search index (SQLite FTS5) and searching. The
  index updates itself after every save (an `after_flush` hook); changes
  made with raw SQL or bulk `delete()` must update it themselves.
- `app/scanner.py`: finds audio files, reads length and tags, and keeps the
  database in step with the library
- `app/templates/`: HTML page templates. Pages use `{% extends layout %}`:
  `base.html` for a full page, `_partial.html` when htmx asks for just the
  middle (so the player bar keeps playing).
- `app/static/`: stylesheet, `player.js` (the player bar), `pages.js`
  (small page helpers such as "tick all"), and
  `htmx.min.js` (a copy of htmx 2.0.11; never load it from the internet)
- `tests/`: automated tests. Run them with `pytest` after
  `pip install -r requirements-dev.txt`. On the owner's VM, `python3-venv`
  isn't installed, so run them in a throwaway container instead:
  `docker run --rm -v "$PWD":/src:ro python:3.13-slim sh -c 'cp -r /src /w && cd /w && pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider'`
- `Dockerfile`, `compose.yaml`: how the app is packaged and started
- `docs/INSTALL.md`: owner-facing install guide. Keep it accurate when
  setup steps change.

**Navigation links are buttons** (owner's choice): "← Back", "← All
series", "← Back without saving" and similar links are written as
`<nav class="back"><a class="button" href="...">← ...</a></nav>`, never
as plain text links. Do the same for any new page.

**Browser checks against the owner's running app** must not save anything
into their real database (it holds their edits, and test values could be
invented dates). Use a throwaway copy of the app with its own data folder
instead, or only look and click without saving.

**Testing Docker builds in a cloud session:** the sandbox's network proxy
breaks `pip install` inside `docker build`. Don't change the real Dockerfile
for this. Build from a temporary copy that adds
`COPY --from=ca ca-bundle.crt /ca.crt` and `ENV PIP_CERT=/ca.crt` before the
`pip install` line, using `--build-context ca=/root/.ccr`. Start the daemon
with `dockerd &` if it isn't running.

## Owner's test setup

Public docs give general recommendations. The owner's own test machine is:

- An Ubuntu Server VM on Proxmox: 128 GB boot disk, 2 TB thin-provisioned
  recordings disk mounted at `/srv/airwave`
  (`library/`, `inbox/`, `data/`).
- Reached over the LAN via remote desktop, and over Tailscale. Their phone
  can open the app over Tailscale, which is useful for testing the phone layout.
- Libraries are expected to get very large. For example: an Art Bell archive of
  about 70 GB / 147 days of audio at ~44 kbps, plus a sizeable old-time radio
  collection (usually low bitrate).

## Git and GitHub

- Commit work with clear messages that describe the change in plain words.
- Explain what a commit, branch, or pull request is the first time it comes up
  in a session, and give click-by-click instructions for anything the owner
  must do on GitHub (reviewing, merging).
