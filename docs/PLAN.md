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

### A personal project

Airwave Archive is built for its owner's own use. It's free software (GPL v3)
and anyone may use or adapt it, but it isn't run as a community project:
there's no promise of support, feature requests, or releases on a schedule.

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
- **Dates are shown the American way** (owner's choice): "Friday, April
  14th, 1939, 8:00 PM US Eastern"; short form "Apr 14, 1939". Typed dates
  on the review page stay in the 1939-04-14 form, which can't be misread.
- **Store the time zone** with any time (e.g. a broadcast at 8:00 PM US
  Eastern). Old-time radio was often aired at different local times for East
  and West coasts, so the time zone matters.
- The calendar's month view can show date-only programs; the day and week
  views put them in a "time unknown" strip at the top of each day.
- Programs known only to the year or month appear in a side list ("Also from
  this month / year") so they're never hidden.

### Overnight shows and multi-part broadcasts

- **Shows that cross midnight.** Overnight talk shows (e.g. Art Bell's
  *Coast to Coast AM*, roughly 10 PM–3 AM Pacific) are stored with a start
  time and a length, and appear on the calendar as **one** broadcast spanning
  both dates — not split in two, not on the wrong day.
- **One broadcast, several files.** Long shows are often saved as "Hour 1",
  "Hour 2", and so on. The app can group those parts into a single episode
  that plays straight through, while still remembering each separate file.

## 3. Files and storage

### Ground rule

**The app never moves, renames, or changes files in your library.** The only
ways files ever get added to the library are when *you* approve uploads from
the inbox (below). Writing tags into files is a separate, opt-in action
(section 5).

### Where things live

The recommended layout on the server is one folder, `/srv/airwave`, ideally
on its own disk:

| Folder | What's in it |
|--------|--------------|
| `/srv/airwave/library/` | Your recordings, in whatever folder structure you like. |
| `/srv/airwave/inbox/` | Web uploads waiting for review (see below). |
| `/srv/airwave/data/` | The app's database and artwork cache. Small, but holds all your edits — back this up. |

- The app reads recordings **where they sit**; it never makes copies.
- Deleting the app or its database never touches your recordings.
- In Docker, the library is mounted **writable** (owner's choice, Phase 7)
  so approved uploads can be added to it. The app still never changes,
  renames or deletes the recordings already there; the owner keeps backups
  and snapshots.
- Network shares (NFS/SMB) aren't a goal for now. Anything mounted into
  `/srv/airwave/library` by the operating system will simply work, though.

### Uploads and the inbox

You can upload recordings through the web page (useful for small batches;
for large collections, copying files onto the server is faster).

1. **Upload** — files land in the **inbox**, a holding area separate from the
   library. Only audio file types are accepted, and nothing is ever
   overwritten (duplicate names get a number added).
2. **Review** — the inbox page shows each upload batch with what the app
   worked out from tags and filenames (series, date, title). You fix
   anything that's wrong.
3. **Add to library** — approved files are moved into the library, filed in
   a folder for their series (e.g. `library/Suspense/`).
4. **Or reject** — rejected files are deleted from the inbox. The library is
   never touched.

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
- **Series totals** — number of episodes, total listening time, and total
  size for each series.
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
   *Suspense*, air date May 13th, 1943, title *Donovan's Brain*. The app will
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

### Airwave tags

When the app writes information into files (tag write-back, section 5 item 5,
or exports, section 5a), it uses a documented set of **custom tags** for the
radio-specific fields: air date (including partial dates), air time, time
zone, station/network, series, episode number, host, cast, notes, and a
stable episode ID. Each audio format stores custom tags its own way (MP3: ID3
"TXXX" frames; FLAC/OGG: Vorbis comments; M4A: freeform atoms), but the field
names are the same everywhere. Another Airwave Archive server can then read
them back exactly.

When sources disagree, the app records **where each value came from** and
**manual edits always win** over automatic ones, so a re-scan never
overwrites your corrections.

## 5a. Export and sharing

You can select episodes (or a whole series) and **export** them as a zip
file to share with someone else who runs Airwave Archive.

- The export works on **copies**. Each copy gets the database information
  written into its tags (standard and Airwave tags). **Library files are
  never modified.**
- The zip also includes a small **manifest** file listing every episode and
  its information, plus series artwork. That way nothing is lost even for
  formats with limited tag support.
- Audio files are already compressed, so the zip stores them as-is ("store"
  mode). This makes exports fast, and large exports (tens of GB) are
  supported.
- **Importing:** the receiving server accepts the zip into its **inbox**.
  Every episode arrives already filled in, ready to review and add to the
  library. Episode IDs let it spot episodes you already have.
- The project itself provides no sharing service. Moving the zip between
  people (and having the right to share the recordings) is up to them.

## 6. Suggested technology (and why)

| Piece | Choice | What it is, and why |
|-------|--------|---------------------|
| Language | **Python** | A programming language known for being readable — closer to QBasic in spirit than most. It has excellent libraries for reading audio tags (e.g. *mutagen*) and handling dates. |
| Web framework | **FastAPI** | A "framework" is a ready-made toolkit so we don't write a web server from scratch. FastAPI is modern, fast, well documented, and catches many mistakes automatically. |
| Pages | **Server-rendered HTML templates (Jinja2) + htmx** | The server builds each page from a template (a page with blanks to fill in) and sends finished HTML to the browser. **htmx** is a small add-on that lets parts of a page update without reloading — e.g. saving an edit or keeping the player bar running. This avoids a big, complex JavaScript app. |
| Database | **SQLite** | A database stored in a single ordinary file. No separate database server to install or manage; easy to back up (just copy the file). Plex and Jellyfin use it for the same kind of library. See "Large libraries" below. |
| Deployment | **Docker Compose** | Docker packages the app with everything it needs into a "container" — a sealed box that runs the same way on any machine. Docker Compose is a short settings file describing how to start it, which folders it can see, and which port to use. Installing or updating becomes one or two commands. |

### Large libraries

The app should stay fast with very large collections (hundreds of thousands
of episodes, multiple terabytes of audio). For scale: talk radio at ~44 kbps
is about 20 MB per hour; a 50,000-episode old-time radio collection is
roughly 500 GB. The **database stays small** — a few hundred MB even for
100,000 episodes — because it stores information *about* the files, not the
audio.

Speed comes from how the app is built:

- **Indexes** — like the index at the back of a book, so "everything that
  aired in May 1943" is looked up directly instead of reading every row.
- **Full-text search** (SQLite's built-in FTS5) for fast search across titles,
  cast, and notes.
- **Incremental scanning** — re-scans only examine new or changed files.
- **WAL mode** — a SQLite setting that lets pages keep loading while a scan is
  saving changes.
- **SQLAlchemy** — a standard Python library the code uses to talk to the
  database, so a move to PostgreSQL (a larger, separate database server)
  would be a contained job if it's ever needed.

## 7. Recommended server

Airwave Archive is light. Recommended starting point (e.g. a Proxmox VM):

- **OS:** Ubuntu Server LTS (26.04 or 24.04).
- **CPU / RAM:** 2 cores, 4 GB RAM (2 GB minimum).
- **Boot disk:** 32 GB — operating system, Docker, and the app.
- **Recordings disk:** a second disk mounted at `/srv/airwave`, sized for
  your collection plus room to grow (500 GB is a reasonable start).
- **Docker:** installed from Docker's own package repository, not the Ubuntu
  "snap" version (which restricts which folders containers can see).

Step-by-step setup is in [`INSTALL.md`](INSTALL.md).

## 8. License

**GNU General Public License v3 (GPL v3).** In plain terms: anyone may use,
study, change, and share the software, but if they distribute a modified
version they must share their source code under the same license. The full
text is in the `LICENSE` file.

## 9. Build phases

Each phase ends with something you can see and try. Later phases build on
earlier ones. Phases may be split into smaller steps as we go.

### Phase 0 — Project setup ✅ *done*
- `CLAUDE.md` (how to work together) and this plan.
- **Try it:** read the plan on GitHub.

### Phase 1 — Hello, library ✅ *done*
- Minimal FastAPI app that scans one folder and **lists the audio files on a
  web page** (filename, folder, size, length), with a Rescan button.
- Runs locally and via Docker Compose, with the music folder mounted
  read-only.
- **Try it:** start the app, open it in a browser, see your files listed.

### Phase 2 — Remember what we found ✅ *done*
- Store scan results in SQLite (with indexes and WAL mode from day one);
  re-scans pick up new, changed, and removed files without starting over.
- Read existing tags (ID3 etc.) and show them.
- **Try it:** add a file to the folder, re-scan, see it appear.
- Notes: the database is `airwave.db` in the data folder. A re-scan only
  opens files whose size or modified time changed. Files that disappear are
  dropped from the database; once Phase 4 adds manual edits, they should be
  marked "missing" instead, so edits aren't lost if a disk is unplugged. If
  the library folder itself is missing, a scan changes nothing.

### Phase 3 — Play it ✅ *done*
- Stream audio from the server into a player on the page.
- Persistent player bar that keeps playing while you browse (htmx).
- **Try it:** play an episode, click to another page, it keeps playing.
- Notes: a simple file page (`/files/<id>`) was added so there's somewhere
  to browse to; Phase 5 grows it into the full episode page. htmx is kept
  in the app (`app/static/htmx.min.js`, version 2.0.11), not loaded from
  the internet. Formats a browser can't play (often WMA, sometimes AIFF)
  show a message in the player bar.

### Phase 4 — Radio metadata
Split into five smaller steps, done in order.

#### 4a — Episode details and an edit form ✅ *done*
- Database fields for series, episode number, title, station/network,
  host, cast, notes, and **partial air date + time + time zone**.
- Episodes are stored separately from files (one episode can later have
  several files, for 4e). Every file gets an episode automatically.
- Edit form for the episode details. Manual edits are recorded as manual,
  and a re-scan never overwrites them.
- Files that disappear but carry edits are marked **missing** instead of
  being forgotten; if they come back (or turn up in another folder with
  the same name and size), they're reconnected. Missing files can be
  forgotten by hand.
- **Try it:** add the cast and air date to *The Patriot*, re-scan, see the
  edits are still there.
- Notes: the database layout is now version 2. When the app updates an
  older database it first saves a copy (`airwave.db.before-v2`). Time
  zones offered: US Eastern/Central/Mountain/Pacific, UK, UTC.

#### 4b — Fill in from tags ✅ *done*
- Pre-fill episode details from the file's own tags (e.g. artist → series),
  marked as automatic. Manual edits always win.
- **Try it:** Campbell Playhouse files show series and title with no typing.
- Notes: series comes from the artist tag (or album if there's no artist),
  title from the title tag, the air date from the date tag (only clear
  forms: `1939`, `1939-04`, `1939-04-14`; any time is ignored as it has no
  zone), and the whole comment goes into Notes (owner's choice). Pulling
  cast and exact dates out of comments is left for 4c, where guesses are
  confirmed first. Database version 3 fills in existing episodes once.

#### 4c — Filename parser with a confirm screen ✅ *done*
- Recognise common naming patterns (`Suspense 1943-05-13 - Donovan's
  Brain.mp3`, `CampbellPlayhouse39-04-14ThePatriot.mp3`, partial dates like
  `1943-xx-xx`).
- Review screen showing each guess; accept, fix, or skip. Nothing is saved
  until confirmed.
- **Try it:** confirm air dates for all five Campbell Playhouse episodes at
  once.
- Notes: the reader looks for a date (`1943-05-13`, `1943.05.13`,
  `19430513`, `43-05-13` read as 19xx, `xx` for unknown parts); text before
  it is the series, after it the title; an episode number only when marked
  (`#42`, `Ep 42`). Month names work too (`Mar 01 2010`, `March 2nd,
  1996`, `01-Mar-2010`, added in 4e), and a part marker at the end
  (`Hour 1`, `Part 2`) isn't taken as a title. The review page (`/review/filenames`) shows only files
  offering something for a field the owner hasn't set; cards that would
  replace the owner's own values start unticked. Confirmed values count as
  manual. Pulling cast lists out of comments is not done (could be a later
  idea).

#### 4d — Overnight broadcasts ✅ *done*
- Start time + length, so a broadcast that crosses midnight is one
  broadcast, shown with its end on the next day.
- **Try it:** set a start time on a long file and see the end time on the
  next day.
- Notes: the episode stores only its start (date, time, zone); the length
  is the recording's length (no separate "length on air" field for now; in
  4e a grouped episode's length is the sum of its parts). The end is shown
  as "until about 3:00 AM on Friday, May 3rd", worked out in the episode's
  time zone so nights when the clocks change come out right. The `tzdata`
  package carries the time zone rules so this also works on Windows.

#### 4e — Multi-part broadcasts ✅ *done*
- Group files ("Hour 1", "Hour 2", …) into one episode that plays straight
  through, while still remembering each file.
- **Try it:** group three files and play them as one.
- Notes: grouping is by hand, from a file's page ("Group with other
  files…"), choosing among nearby files in the same folder. Parts play in
  file-name order ("Hour 2" before "Hour 10"); each file gets a
  `part_number` (database version 4). The group keeps the details of the
  episode it was made from; series, title and date from tags come from
  part 1, and Notes gather the descriptions of all parts (each different
  one once, labelled "Part 1:", "Parts 2 and 3:"; owner's choice). The
  library shows a group as one row with its total length; the overnight end
  time uses the total too (owner's choice: the recordings' length decides
  the end time, even if commercials were cut; no separate "length on
  air"). Grouping counts as a manual edit, so a missing
  part is kept (and skipped when playing). Not done: guessing groups
  automatically from file names (could be added to the review page later).

### Phase 5 — Browse like Plex
Split into four smaller steps, done in order.

#### 5a — Series list and series pages ✅ *done*
- A list of series with totals (episodes, listening time, size, years).
- Series page with its episodes in air-date order (each links to its
  episode page, which already exists).
- **Try it:** open the Series list, pick a series, and see its episodes
  in the order they aired.
- Notes: a series is every episode with the same Series name, exactly
  as typed (joining two spellings means renaming; bulk renaming is Phase
  6). Names sort ignoring "The/A/An". Episodes without a series are
  listed last as "No series yet". Within a series: full dates in order,
  then within each year those known only to the month or year, then
  undated ones by episode number (as a number) and title. A menu (Library
  · Series) is in the top bar; series names in the library and on
  episode pages link to the series.

#### 5b — Artwork from the files and folders ✅ *done*
- Pictures stored inside the audio files, or an image in the folder
  (`cover.jpg`, `folder.jpg`...), shown as tiles in a series grid.
- **Try it:** see the series grid with pictures.
- Notes: pictures are read from MP3/WAV/AIFF (ID3), FLAC, M4A and
  Ogg/Opus files (not WMA), and from `cover`, `folder`, `front`,
  `poster`, `album`, `artwork` + `.jpg/.jpeg/.png/.webp` in a folder (or
  a folder above it, but not the very top of the library). The app keeps
  600-pixel JPEG copies in `data/artwork/`, named by a fingerprint of the
  picture, so a logo shared by many files is kept once. A series uses its
  main folder's picture first, then one from inside its files; an episode
  uses its own (first part's) picture first, then its folder's. Series
  without a picture get a coloured square with their initials. The
  Series page shows tiles, or a list. Database version 6 adds the
  pictures and makes the next scan read every file once more.
- Found while trying 5b: a browser tab opened before an update kept the
  old stylesheet (htmx only swaps the middle of the page), so the new
  tiles looked broken in Chrome. Now the stylesheet and scripts carry a
  version in their address, and a tab from before an update reloads the
  page it's going to by itself.
- Owner's choice: the Series tiles are the front page (`/`); the file
  list moved to `/library`. The menu reads Series · Library.

#### 5c — Uploading artwork ✅ *done*
- Upload your own picture for a series (kept in the data folder, never
  in the library).
- **Try it:** give a series without a picture one of your own.
- Notes: "Change picture…" on a series page. JPEG, PNG or WebP, up to
  30 MB; the app keeps a 600-pixel copy in `data/uploaded-artwork/`
  (needs backing up, unlike `data/artwork/`). Phone photos are turned the
  right way up. An uploaded picture wins over the library's, for the
  series and for its episodes without a picture of their own. Renaming
  every episode of a series carries its picture to the new name. Database
  version 7 adds the `series_art` table. Pictures for single episodes
  aren't offered (could be added later).

#### 5d — Phone-friendly layout ✅ *done*
- Check every page on a phone, and fix what doesn't fit.
- **Try it:** browse your collection on your phone.
- Notes: checked every page at phone size (390 pixels wide). On touch
  screens everything tappable is at least 44 pixels; text boxes use
  16-pixel text so iPhones don't zoom in; the series list hides its
  Years column on phones so names don't break mid-word. The player bar
  shows the show's picture, and so does the phone's lock screen
  (`/files/{id}/art` finds an episode's picture).
- The browser's own audio controls were too small on the iPhone, so the
  player bar has the app's own: a big Play/Pause button, back 15 s and
  forward 30 s buttons (also on the lock screen), and a wide progress
  bar with a big handle.

### Phase 6 — Bulk editing, search, CSV import
Split into three smaller steps, done in order.

#### 6a — Bulk editing ✅ *done*
- Tick several episodes (or all of a series) and set details for all of
  them at once: series, station/network, host, cast, notes, and a start
  time with time zone.
- Rename a whole series in one step.
- **Try it:** fix a network on many episodes at once; rename a series.
- Notes: tick boxes on series pages and the library ("tick all" ticks
  the page); "Edit all N…" on a series page takes every episode of the
  series, across pages. Each detail: leave as it is / change to / empty
  it. A start time only goes on episodes with a full air date (the rest
  are counted and reported). Changed details count as manual. "Rename…"
  renames every episode of a series (typing another series' name joins
  them); an uploaded picture follows. Titles, episode numbers and dates
  are per-episode and not offered for bulk editing.

#### 6b — Search ✅ *done*
- Search box across titles, series, cast, host, notes (SQLite FTS5).
- **Try it:** search for an actor's name and find their episodes.
- Notes: a search box in the top bar (its own row on phones) and a
  `/search` page. It searches titles, series, cast and host, station,
  episode numbers, notes and file names. Every word must match, in any
  order; the end of a word can be left off; accents and capitals don't
  matter; "quoted words" must be together. Best matches first (a match in
  the title counts most), with highlighted snippets; matching series are
  shown as tiles above. The index (`episode_search`, database version 8)
  is updated automatically whenever episodes or files are saved.

#### 6c — CSV import ✅ *done*
- Import an episode log (a CSV spreadsheet) with a preview before
  anything is saved.
- **Try it:** import an episode log for a series.
- Notes: "Import spreadsheet…" on the Library page. Commas, semicolons
  or tabs; Excel's text encodings. Columns are recognised by common names
  (Series/Show, Date/Air Date, Ep/#, Title, Starring/Cast/Guests,
  Description/Notes, Network/Station, Host, Time, Zone, File) and can be
  changed on the preview. Rows match an episode by file name, else series
  + full air date, else series + episode number; a row fitting several
  episodes isn't matched. An optional "Series" box fills in rows without
  one. Dates: 1939-04-14, 4/14/1939 and 4/14/39 (American order, two-digit
  years as 19xx), April 14, 1939, 14 April 1939, April 1939, 1939. Empty
  cells change nothing; rows replacing the owner's own values start
  unticked; rows with unreadable values can't be saved. Imported values
  count as manual. The uploaded file waits in `data/imports/` and is
  deleted after saving (or after a day). Built for common formats without
  a real log to test with: adjust when the owner tries one.
- A "Download an example spreadsheet" button gives a CSV with every
  column the app understands and one example row (a made-up series, so
  importing it unchanged touches nothing).

### Phase 7 — Upload inbox
Split into two steps. Owner's choice (2026-11-05): the whole library is
mounted writable, and approved files go into `library/<Series>/` next to
existing ones (they keep backups and snapshots). The app still never
overwrites, renames or deletes library files.

#### 7a — Upload and review ✅ *done*
- Upload files through the web page into the inbox (`/srv/airwave/inbox`,
  `AIRWAVE_INBOX`); fix the details worked out for each; reject (delete
  from the inbox). The library isn't touched.
- **Try it:** upload a few files, fix their details, reject one.
- Notes: an "Inbox" menu item. Only audio files are accepted; names are
  made safe and never overwrite (a number is added). Uploads are written
  under a hidden name until complete. Files copied into the inbox by hand
  appear too. Details come from tags first, with the file name filling
  gaps and giving the date if fuller. Each waiting file can be played. The
  `inbox_items` table (database version 9) holds the details.

#### 7b — Add to the library ✅ *done*
- Approve files: moved into `library/<Series>/`, scanned, with the
  details confirmed in the inbox saved on their episodes.
- **Try it:** upload a few files from your laptop, fix their dates, add
  them to the library.
- Notes: an "Add to library" button on each waiting file, and "Add all" for
  every waiting file at once (both save what
  was typed first). The file goes into the folder holding most of its
  series' recordings; if the series is new, a folder named after it
  (made safe); with no series, `Unsorted`. Names are never reused. Between
  disks the file is copied under a hidden name, then renamed, then removed
  from the inbox. Only that one file is read (no full rescan). Its details
  count as manual edits. The library is mounted writable in
  `compose.yaml`.

### Phase 8 — Continue listening ✅ *done*
- Save playback position; "Continue listening" row on the home page.
- Mark as played / unplayed.
- **Try it:** stop halfway through an episode, come back later, resume.
- Notes: the player reports its place (part + seconds) every 10 seconds,
  on pause, when switching episodes and when the page is hidden. Play
  picks up 3 seconds before that place (a later part's own button only
  resumes within or after that part). Under 10 seconds in doesn't count as
  started. Hearing the last part to its end marks the episode played;
  playing it again un-marks it. "Mark played" / "Mark not played" (or
  "Start over") on the episode page and for ticked episodes. Lists show
  "✓ Played" or the time left. The `listening` table (database version
  10) is removed along with its episode.

### Phase 9 — The calendar
- Month view, then week view, then day view.
- 9a (month view) ✅ *done*: "Calendar" in the menu; a month grid (weeks
  start on Sunday) with the number of episodes each day (titles on wide
  screens), then the month's episodes day by day in time order (unknown
  times last), then "Sometime in <month>" for those with no day. Episodes
  with only a year are counted, never placed. ← / → for the next month;
  "Earlier/Later recordings" jump over empty months; a month + year box
  jumps anywhere. Opens on the most recent month with recordings. Code in
  `app/airdates.py`.
- Correct handling of partial dates, "time unknown", time zones, and
  overnight shows.
- **Try it:** open the week of a famous broadcast and see everything you own
  from that week.

### Phase 10 — Writing tags back (optional)
- Opt-in action to write the app's metadata into the files' tags, with a
  preview and a clear warning. Requires the music folder to be mounted
  writable.
- **Try it:** write tags to one file, open it in another player, see the
  new info.

### Phase 11 — Export and import
- Export selected episodes or series as a zip of tagged copies + manifest.
- Import an Airwave zip through the inbox, with duplicates detected.
- **Try it:** export a series, import it on a second test server (or a
  fresh install), and see it arrive fully labelled.

### Later — Desktop installers (Windows and Mac)
For people who don't want Docker: package the app as a normal desktop
program (a Windows installer, a Mac `.dmg`). It starts in the background,
opens the browser to the Library page, and asks for the recordings folder
on first run. Built automatically by GitHub for each release.
- Open question: code signing. Without it, Windows and macOS show
  "unknown publisher" warnings; signing costs roughly $100–400/yr
  (Windows) plus $99/yr (Apple). The website explains how to get past
  the warnings either way.

### Later — Going public
- Make the GitHub repository public. Turn off **Issues** and
  **Discussions** (Settings → General → Features), since there's no
  support channel.
- Publish finished versions on GitHub **Releases** (installers and
  Docker image).
- **airwavearchive.org**: one static page covering what it is,
  screenshots, download links (pointing to GitHub Releases), how to get
  past install warnings, Docker instructions, GPL v3, and "provided as-is,
  no support." No contact info.
- Hosted on a small, separate VM on the public-facing VLAN, using Caddy
  (automatic HTTPS), with only ports 80/443 open, SSH over Tailscale only,
  and automatic security updates.

### Later ideas (not committed)
- Multiple library folders, playlists / "collections", backups of the
  database, import from online episode logs, podcast RSS feed subscriptions.
