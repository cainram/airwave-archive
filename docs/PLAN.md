# Airwave Archive — Project Plan

## 1. Purpose

Airwave Archive is a **self-hosted, homelab-first web app** for managing a
personal collection of downloaded radio programs — old-time radio shows,
podcasts, and similar recordings.

Think of it as a music library (like Plex or iTunes), but built around the
information that matters for radio:

- **Original air date and time** (often only partly known)
- **Station / network** (e.g. CBS, NBC Blue, WOR)
- **Series** (e.g. *Suspense*, *The Shadow*)
- **Episode number** and **episode title**
- **Host**, **cast**, and free-form **notes**

"Self-hosted" means it runs on a computer you own (a home server, NAS, or an
old PC — your "homelab"), not on someone else's website. You open it in a web
browser on any device in your house.

### Who it's for

- **One household.** It's not a service for the public, and it doesn't need
  sign-ups or multi-user accounts.
- **People who already have their recordings.** The project hosts and
  distributes **no content**. You bring your own files.

## 2. Key feature: the calendar

The headline feature is a **calendar view** — like a TV guide for the past.

- Switch between **day**, **week**, and **month** views.
- The calendar shows **every program in your collection that originally aired
  during the time range you're looking at**, across all series.
- Example: open the week of May 9–15, 1943 and see every *Suspense*, *Jack
  Benny*, and news broadcast you own from that week, laid out by day and time.
- Click any program to open its detail page or start playing it.

### Handling incomplete air dates

Real-world air dates are often incomplete. The app must be honest about that:

| What's known            | How it's stored         | How it's shown                          |
|-------------------------|-------------------------|-----------------------------------------|
| Year only (1943)        | year                    | Listed under "1943 — date unknown"      |
| Year + month            | year, month             | Listed under that month, "day unknown"  |
| Full date, no time      | year, month, day        | On that day, labelled "time unknown"    |
| Full date and time      | date, time, time zone   | At that time slot                       |

Rules:

- **Never invent missing parts.** No defaulting to January 1st or midnight.
- **Store the time zone** with any time (e.g. a broadcast at 8:00 PM US
  Eastern). Old-time radio was often aired at different local times for East
  and West coasts, so the time zone matters.
- The calendar's month view can show date-only programs; the day and week
  views put them in a "time unknown" strip at the top of each day.
- Programs known only to the year or month appear in a side list ("Also from
  this month / year") so they're never hidden.

## 3. How it treats your files

- **Reads audio files where they already sit on disk.** You point the app at
  one or more folders; it never copies, moves, renames, or uploads them.
- The app keeps its own information (titles, dates, notes) in a separate
  database file. Deleting the app's database never touches your recordings.
- **Writing tags back into files is optional** and only happens when you
  explicitly ask for it (see section 5).
- In Docker, music folders are mounted **read-only** by default, so the app
  physically cannot change them unless you choose to allow it.

## 4. User interface

The look and feel is inspired by **Plex**: clean, visual, easy to browse.

- **Series grid** — a wall of series with their artwork, like album covers.
- **Series page** — the episode list in **original air-date order**, with
  unknown dates grouped clearly.
- **Episode detail page** — artwork, title, air date/time, station, host,
  cast, notes, file info, and a Play button.
- **Easy metadata editing** — edit any field right on the page. **Bulk edit**:
  select many episodes and set the same field (e.g. network = "CBS") on all of
  them at once.
- **Search** — across titles, series, cast, host, notes.
- **Continue listening** — remembers where you stopped in each episode and
  lists partly-played episodes on the home page.
- **Persistent player bar** — a player at the bottom of the screen that keeps
  playing while you browse to other pages.
- **Calendar** — day / week / month (section 2).

Design goals:

- Intuitive and uncluttered.
- **Works well on phones** as well as computers.
- **Not a heavy JavaScript app.** Pages are built on the server and sent
  ready to display; a small library (htmx) handles the interactive parts. This
  keeps it fast, simple, and easy to maintain.

## 5. Getting information (metadata) in

"Metadata" means information *about* a recording — title, date, cast — as
opposed to the sound itself. The app gets it from several sources:

1. **Existing ID3 tags.** Most MP3 files already contain labels (called "ID3
   tags") with things like title and artist. The app reads these on scan.
   Other formats (FLAC, M4A, OGG) have their own tag formats and are read too.
2. **Filename parsing.** Many collections encode information in the file
   name, e.g. `Suspense 1943-05-13 - Donovan's Brain.mp3` → series
   *Suspense*, air date 13 May 1943, title *Donovan's Brain*. The app will
   recognize common patterns, show you what it guessed, and let you confirm
   or fix it. Partial dates like `1943-xx-xx` are supported.
3. **CSV import.** A CSV file is a simple spreadsheet saved as plain text. You
   can prepare episode logs in a spreadsheet (or use one found online) and
   import them, matched to files by filename or by series + date.
4. **Manual and bulk editing** in the web interface.
5. **Optional: write tags back to files.** If you choose, the app can save
   its information into the files' own tags (including custom radio fields
   like air date and network), so other players can see it too. Off by
   default; always an explicit action.

When sources disagree, the app records **where each value came from** and
**manual edits always win** over automatic ones, so a re-scan never
overwrites your corrections.

## 6. Suggested technology (and why)

| Piece | Choice | What it is, and why |
|-------|--------|---------------------|
| Language | **Python** | A programming language known for being readable — closer to QBasic in spirit than most. It has excellent libraries for reading audio tags (e.g. *mutagen*) and handling dates. |
| Web framework | **FastAPI** | A "framework" is a ready-made toolkit so we don't write a web server from scratch. FastAPI is modern, fast, well documented, and catches many mistakes automatically. |
| Pages | **Server-rendered HTML templates (Jinja2) + htmx** | The server builds each page from a template (a page with blanks to fill in) and sends finished HTML to the browser. **htmx** is a small add-on that lets parts of a page update without reloading — e.g. saving an edit or keeping the player bar running. This avoids a big, complex JavaScript app. |
| Database | **SQLite** | A database stored in a single ordinary file. No separate database server to install or manage; easy to back up (just copy the file). Plenty fast for one household. |
| Deployment | **Docker Compose** | Docker packages the app with everything it needs into a "container" — a sealed box that runs the same way on any machine. Docker Compose is a short settings file describing how to start it, which folders it can see, and which port to use. Installing or updating becomes one or two commands. |

## 7. License

**GNU General Public License v3 (GPL v3).** In plain terms: anyone may use,
study, change, and share the software, but if they distribute a modified
version they must share their source code under the same license. The full
text is in the `LICENSE` file.

## 8. Build phases

Each phase ends with something you can see and try. Later phases build on
earlier ones. Phases may be split into smaller steps as we go.

### Phase 0 — Project setup *(this step)*
- `CLAUDE.md` (how to work together) and this plan.
- **Try it:** read the plan on GitHub.

### Phase 1 — Hello, library
- Minimal FastAPI app that scans one folder and **lists the audio files on a
  web page** (filename, folder, size, length).
- Runs locally and via Docker Compose, with the music folder mounted
  read-only.
- **Try it:** start the app, open it in a browser, see your files listed.

### Phase 2 — Remember what we found
- Store scan results in SQLite; re-scans pick up new, changed, and removed
  files without starting over.
- Read existing tags (ID3 etc.) and show them.
- **Try it:** add a file to the folder, re-scan, see it appear.

### Phase 3 — Play it
- Stream audio from the server into a player on the page.
- Persistent player bar that keeps playing while you browse (htmx).
- **Try it:** play an episode, click to another page, it keeps playing.

### Phase 4 — Radio metadata
- Database fields for series, episode number, station/network, host, cast,
  notes, and **partial air date + time + time zone**.
- Filename parser with a preview/confirm screen.
- Edit form on the episode page.
- **Try it:** scan files named like `Suspense 1943-05-13 - …`, confirm the
  guesses, edit a cast list.

### Phase 5 — Browse like Plex
- Series grid with artwork (from embedded art, a folder image, or upload).
- Series page with episodes in air-date order; episode detail page.
- Mobile-friendly layout.
- **Try it:** browse your collection on your phone.

### Phase 6 — Bulk editing, search, CSV import
- Select many episodes and edit fields together.
- Search box across titles, series, cast, host, notes.
- CSV import with a preview before anything is saved.
- **Try it:** import an episode log for a series; fix a network on 50
  episodes at once.

### Phase 7 — Continue listening
- Save playback position; "Continue listening" row on the home page.
- Mark as played / unplayed.
- **Try it:** stop halfway through an episode, come back later, resume.

### Phase 8 — The calendar
- Month view, then week view, then day view.
- Correct handling of partial dates, "time unknown", and time zones.
- **Try it:** open the week of a famous broadcast and see everything you own
  from that week.

### Phase 9 — Writing tags back (optional)
- Opt-in action to write the app's metadata into the files' tags, with a
  preview and a clear warning. Requires the music folder to be mounted
  writable.
- **Try it:** write tags to one file, open it in another player, see the
  new info.

### Later ideas (not committed)
- Multiple library folders, playlists / "collections", backups of the
  database, import from online episode logs, podcast RSS feed subscriptions.
