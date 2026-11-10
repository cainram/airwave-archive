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

**The one exception is deleting** (owner's choice, 2026-11-06; the owner keeps
backups and snapshots): you can delete an episode's recordings from the disk,
from its page ("Delete…") or by ticking episodes ("Delete ticked…"). It's
always an explicit action, after a page listing exactly which files will go,
and only files inside the library can be deleted. The episode and its details
are removed from the app too. Folders are left as they are.

**A second exception is converting** (owner's choice, 2026-11-08). The
inbox is the main way recordings arrive, and it converts formats that
can't be played well before anything reaches the library. Adding files
straight into the library folder and scanning is an **advanced
technique**: recordings found that way in those formats can be converted
where they sit, **and the originals deleted**, but only after a page
listing exactly which files will be converted and warning that the
originals go. A new file replaces the original only once it has been
checked (it opens, and its length matches). See Phase 13.

**A third exception is writing details into the files' tags** (Phase 10a,
owner's go-ahead 2026-11-09). Only when asked, after a preview page listing
every file and each tag that will change. Only the tags change, never the
sound; tags the app doesn't know about are kept. Each file is written as a
hidden copy beside it, checked, and only then put in the original's place,
so a file is never left half-written.

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
  so approved uploads can be added to it, and recordings can be deleted
  when the owner asks (see the ground rule). The app never changes or
  renames the recordings already there; the owner keeps backups and
  snapshots.
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

- **Home page** (`/`, owner's choice 2026-11-07) — the first item in the
  menu (Home · Series · Library · Calendar · Inbox) and where the app
  opens. Rows, top to bottom: **Continue listening** (the 4 most recently
  played unfinished episodes; starting another pushes the oldest off); **Recently added
  series** (series by their newest recording, so an old series with a new
  episode comes back to the front; no time limit); **Most played** (series
  by how many of their episodes have been listened to; the app doesn't
  count replays). The series rows are tiles like the Series page: on a
  computer, one line of as many as fit the window; on a phone, 10 in a
  line you swipe sideways. An empty row isn't shown.
- **Series grid** (`/series`) — a wall of series with their artwork, like
  album covers.
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
  playing while you browse to other pages. A × at its right end stops it and hides the
  bar, to free the screen (the place is kept, so Resume still works).
- **Calendar** — day / week / month (section 2).
- **Continue listening** cards have a small × to take one off the row (its
  place is kept; listening again brings it back).
- **Action menus** (owner's choice, 2026-11-07) — to save space, mostly
  on phones, a page's main action stays a button (Play, Rescan) and the
  rest go in a **☰ Actions** drop-down; ticked episodes have a **☰ With
  ticked** menu. Delete items are red.
- **Light and dark themes** — a switch in the top bar, left of the search
  box. Each browser remembers its choice; until one is made, it follows
  the device's own light/dark setting. All colours are listed once at the
  top of `style.css` (light, then dark).

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
6. **Later: rename files from their details.** File names and folders can
   follow the app's details, like classic MP3 software's "organise" feature.
   Always previewed and undoable (Phase 10b, after v1.0).

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
  1996`, `01-Mar-2010`, added in 4e; `380411` = 1938-04-11, six digits
  run together with a two-digit year read as 19xx, added 2026-11-08 for
  Lone Ranger downloads, which also taught it to drop labels such as
  "Old Time Radio" or "OTR" from the series), and a part marker at the end
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
  part is kept (and skipped when playing). Guessing groups from file
  names came later, as suggestions the owner approves (see "Suggested
  groups" below, v1.1 and v1.2).

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
  list moved to `/library`. The menu reads Series · Library. (Later
  the home page took `/`, and the series moved to `/series`; see
  section 4.)

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

### Phase 9 — The calendar ✅ *done*
- Month view, then week view, then day view.
- 9a (month view) ✅ *done*: "Calendar" in the menu; a month grid (weeks
  start on Sunday) with the number of episodes each day (titles on wide
  screens), then the month's episodes day by day in time order (unknown
  times last), then "Sometime in <month>" for those with no day. Episodes
  with only a year are counted, never placed. ← / → for the next month;
  "Earlier/Later recordings" jump over empty months; a month + year box
  jumps anywhere. Opens on the most recent month with recordings. Code in
  `app/airdates.py`.
- 9b (week view) ✅ *done*: `/calendar/week?date=…` shows Sunday to
  Saturday around any date (typed any common way, e.g. 10/30/1938). Each
  day's episodes in time order with their span ("10:00 PM – 1:00 AM (next
  day) US Pacific": start plus the recordings' length). Days stack on
  phones, seven columns on wide screens. Previous/next week, jumps over
  empty weeks, "Month view"; the month view has "Week view" and a "Week"
  button on each day. Episodes with no known day are counted with a link
  to their month.
- 9c (day view) ✅ *done*: `/calendar/day?date=…` lists the day hour by
  hour ("8 PM"), by each show's own local time (a note says so when zones
  are mixed), then "Time unknown". Shows from the night before still on
  after midnight appear first as "Continued from the night before".
  Previous/next day, jumps over empty days, Week and Month view buttons;
  day headings in the week and month views link to their day.
- Correct handling of partial dates, "time unknown", time zones, and
  overnight shows.
- **Try it:** open the week of a famous broadcast and see everything you own
  from that week.

### Phase 10 — Writing tags back and renaming files (after v1.0)

**Not part of v1.0** (owner's decision, 2026-11-07). The owner still wants
it, in a later version. Both parts change files that are already in the
library, so when they're built, the ground rule in section 3 gets a second
exception alongside deleting. Until then the rule stands unchanged.

Rules for both parts:
- Always an **explicit action**: one episode, ticked episodes, a series, or
  the whole library. Never automatic, never during a scan.
- A **preview page first**, listing every file and exactly what will change.
  Nothing happens until it's confirmed.
- Only files inside the library are touched. The sound itself is never
  re-encoded.
- Large batches run with progress shown and can be stopped partway. A
  stopped batch leaves every file either fully done or untouched.

Whole-library runs of both parts live on the hidden **Advanced page**
(`/advanced`, see "Later" below). Everyday pages offer only one episode,
ticked episodes, or a series.

#### 10a — Write details into the files' tags ✅ *done*
- Save the app's details into each file's own tags: the standard tags
  (title, artist/series, year...) plus the Airwave tags (section 5), so
  other players and other Airwave servers can see them.
- Reuse `app/tagging.py`, which already does this for export copies
  (Phase 11). The difference is that here it writes to the originals.
- After writing, the next scan sees the files as changed. It must not undo
  the owner's edits (manual details still win, section 5).
- **Try it:** write tags to one episode, open the file in another player
  (VLC, iTunes/Music), see the new details.
- **Step 1 ✅ *done*: one episode.** On an episode's page, **☰ Actions →
  Write details into the file…** (or "files…" for an episode in parts)
  opens `/tags/write`, a preview: each file, and a table of each usual tag
  (title, artist, album, date, comment, track) as it is now and as it
  will be, with the Airwave tags in a fold-out. Files already up to date,
  missing, or that can't hold tags are said so and left alone; an episode
  with no details has nothing to write (a lone ID would be no use).
  **Write the details into N files** does it. Notes (`app/tagwriting.py`):
  the episode gets its permanent ID first (as exports do). Each file is
  copied to a hidden `.tagging-…` file beside it, the tags are written
  into the copy, the copy is checked (it opens, and is as long as the
  original), and it replaces the original only if the original hasn't
  changed meanwhile; all with the scan lock held. The app's record of the
  file (size, time, tags) is updated in the same step, so the next scan
  doesn't see a changed file and nothing the owner typed is undone.
  Hidden copies left by a restart are cleared at start-up. Checked on
  copies of real files: the sound is identical afterwards, and cover art
  and other tags (genre, the original comment) are kept.
- **Step 2 ✅ *done*: many at once.** **☰ With ticked → Write details
  into files…** (series and Library pages) and a series page's **☰
  Actions → Write details into files…** open the same preview. With more
  than one file it starts with a summary (how many will be written, which
  tags change and in how many files, how many are already up to date,
  have no details, or can't be written), then shows the changes for the
  first 50 files and counts the rest. Up to 10 files are written straight
  away; more run in the background on a progress page (`/tags/writing`,
  updating every 2 seconds, which can be left) with **Stop writing**.
  Afterwards it says how many were written and lists any files left
  alone, with why. Notes: one batch at a time (the preview says so if one
  is running). The batch is kept in memory, not the database: after a
  restart it's simply gone, every file it reached fully done and the rest
  untouched; running it again skips files already up to date. Whole-library
  runs are left for the Advanced page.

#### 10b — Rename and organise files from the database
Like the "organise my music folder" feature in classic MP3 software
(Winamp, iTunes, MusicBrainz Picard): file names and folders follow the
app's details.
- A **naming pattern** the owner picks once, e.g.
  `{series}/{series} {air_date} - {title}` →
  `Suspense/Suspense 1943-05-13 - Donovan's Brain.mp3`. Multi-part
  episodes add `- Part 1`, `- Part 2`...
- **Never invent dates** (ground rule): a partial date is written as known,
  e.g. `1943` or `1943-05`. Empty fields are left out cleanly, with no
  stray dashes or brackets.
- Names are made **safe on Windows and Mac too**: characters like `: ? " / \`
  are replaced, and names are kept to a sensible length.
- **Clashes** (two episodes that would get the same name) are shown on the
  preview and get a number added, or are skipped and reported. An existing
  file is never overwritten.
- **The app updates its own records in the same step**, so edits, listening
  history, artwork and podcast feeds keep working. Feed entries use file
  IDs, not names, so podcast apps won't see "new" episodes. (Renaming files
  *outside* the app would look like delete-and-add to the scanner, and the
  details would be lost. That's why the app has to do it.)
- **An undo record**: every rename (old name → new name) is kept, so a batch
  can be put back.
- Folders left empty by a move are removed. Pictures in a folder
  (`cover.jpg`...) move along with the recordings.
- Caveat: other programs that point at the old file names (another
  player's playlists, for example) will lose track of them.
- **Try it:** preview renaming one series, confirm, check the files on
  disk, then undo it.

### Phase 11 — Export and import
- 11a (export) ✅ *done*: "Export…" on a series page and "Export ticked…"
  in the tick-box toolbar open a summary (episodes, recordings, size);
  "Download zip" streams the zip while it's made: each recording is copied
  to `export-temp/` in the data folder, tagged (`app/tagging.py`), stored
  (no compression), and deleted. Zip: `<Series>/<file>`, `manifest.json`
  (format "airwave-archive-export", version 1), `pictures/`, `README.txt`.
  Episodes get a permanent ID (`episodes.uid`, database version 11) the
  first time they're exported. Phase 10 is on hold (owner's choice,
  2026-11-06).
- 11b (import) ✅ *done*: the inbox's upload takes zips (`app/zipimport.py`);
  a zip copied into the inbox folder gets an "Unpack" button. Recordings
  are unpacked into the inbox (folder names in the zip ignored, names made
  safe, free space checked first) and the zip is deleted. From an Airwave
  zip, the manifest (or, for a lone tagged copy, its Airwave tags) fills
  in the inbox boxes; the other details (time, station, host, cast, notes,
  ID, part, series picture) wait in `inbox_items.extra_json` (database
  version 12) and are saved as manual edits when the file is added. An
  air time is kept only if the date wasn't changed in the inbox. A file
  whose episode ID is already in the library is marked "Already in your
  library" and can only be rejected ("Add all" skips it; one button
  deletes them all); a part an episode is still missing joins it. A series
  picture is used only if the series has none.
- Export selected episodes or series as a zip of tagged copies + manifest.
- Import an Airwave zip through the inbox, with duplicates detected.
- **Try it:** export a series, import it on a second test server (or a
  fresh install), and see it arrive fully labelled.

### Phase 12 — Podcast feeds (RSS)
Owner's request (2026-11-07): a **Feeds** menu entry; the app makes RSS
feeds of episodes for podcast apps, from whole series or ticked
episodes, with basic details to edit, publish, manage and delete them.
Apple Podcasts on the owner's iPhone comes first: it reads LAN-only
plain-http feeds and their artwork fine (the owner has done so for years
with WordPress's Seriously Simple Podcasting), so feeds are standard RSS
2.0 with Apple's `itunes:` tags. Cloud-based podcast apps (Pocket Casts,
Overcast...) can't reach a home server.

Decisions (owner, 2026-11-07):
- **Dates (option A):** an item's date is when it was added to the feed,
  never an invented air date. Episodes already in the series when the
  feed is made count as added then, one second apart in air-date order
  (so apps keep that order); later ones, when they arrived in the
  library. The real air date (and time, station, cast, notes) is only in
  the episode's description.
- **Names:** the podcast's name starts as the series name (changeable);
  each item's name is the episode's title in Airwave Archive. An episode
  in several parts is one item per part ("Title (part 1 of 2)").
- **Drafts first:** a feed's address works only once it's published.
- **Oldest first (option A):** feeds are "serial" shows, so Podcasts
  lists them in air-date order with each episode's parts 1, 2, 3, and
  plays on to the next; episodes added later come at the end.
- **QR code** on the feed page (`podcast://` address, opens Podcasts).
- Each feed has a private address with a long random token (podcast apps
  can't log in); `itunes:block` keeps it out of Apple's directory. The
  addresses use the server name the page (or podcast app) used.

- 12a (series feeds) ✅ *done*: **Feeds** in the menu (list of feeds,
  draft or published); "☰ Actions → Make a feed…" on a series (name and
  description); a feed's page (Publish, ☰ Actions: edit name and
  description, unpublish, delete), with "Add it on your iPhone": QR code,
  "Open in Podcasts", the address with a Copy button, and steps for
  "Follow a Show by URL". The feed is `/feeds/<token>/feed.xml`, its
  recordings `/feeds/<token>/audio/<file id>.<ext>` (only while
  published, only that feed's recordings). Files Apple's app may not play
  (not MP3/M4A/AAC) are pointed out. A feed follows its series when it's
  renamed. Deleting a feed never touches recordings. Database version 14
  (`feeds` table). `app/feeds.py`; QR codes by segno.
- 12b (hand-picked feeds) ✅ *done*: "☰ With ticked → Make a feed…"
  (name suggested when they're all one series) and "Add to a feed…"
  (choose a hand-picked feed; ones already in it are skipped; new ones go
  at the end). A hand-picked feed's page lists all its episodes with
  ticks to take some out (never out of the library). Episodes deleted
  from the library drop out (`feed_items` rows go with their episode;
  database version 15). Each batch is a second apart per part in
  air-date order. The picture is the first episode's series picture.
- 12c (editing and extras) ✅ *done*: "Edit details…" adds an author
  (empty: "Airwave Archive") and the order: oldest first (a "serial"
  show, the default) or newest first (an "episodic" one); parts read 1,
  2, 3 either way (in newest first, part 1 gets the latest second).
  "Change picture…" uploads a picture for just that feed (kept with the
  uploaded series pictures), or goes back to the series' picture. "New
  private link…" (after a confirmation page) gives the feed a new token;
  the old address stops working. Database version 16 (`feeds.author`,
  `art_hash`, `newest_first`).
- Not done (offered, not asked for): a setting to fix the feed address
  (e.g. always the Tailscale name) instead of using the address the page
  was opened with.
- **Try it:** make a feed of a series, publish it, add it on the iPhone
  (QR code), and play an episode in Podcasts.

### Suggested groups (v1.1 and v1.2)
Owner's request, 2026-11-08: find files that are probably parts of one
broadcast and suggest grouping them. **Never group without approval.**

#### Step 1 (v1.1) — Find possible groups ✅ *done*
- **Library → ☰ Actions → Find possible groups** checks the whole
  library and opens the **Suggested groups** page (`/groups/suggested`).
- A set is suggested when its files are in the same folder (uploads are
  filed into their series' folder, so an upload's parts end up together),
  with names identical (ignoring capitals) except for one part marker:
  `Hour 1`, `Hr. 2`, `Part 3`, `Pt 4`, `Part 1 of 3`, `(1 of 2)`,
  `Side A` / `Side B` (or `Side 1`). The numbers must run 1, 2, 3… with
  none missing or doubled, match an "of N" total if there is one, and be
  24 or fewer. Files' date tags, where present, must agree. Files already
  in a group, or missing, are left out.
- Each suggestion has a tick box (ticked to start) and a **Dismiss**
  button; **Approve ticked** groups the ticked ones, the same as
  grouping by hand (the first part's episode and details are kept; a
  warning shows if a later part has details the owner typed). Dismissed
  sets are remembered (`group_dismissals` table, database version 17) by
  a fingerprint of folder and file names, so a set that later gains a
  new part is suggested again. The code is in `app/grouping.py`.
- **Try it:** split a grouped show (its page → Group with other files…,
  untick all but one), then Find possible groups, and approve it.

#### Step 2 (v1.2) — Suggestions after scans and in the inbox ✅ *done*
- After a scan, a notice on the Library page when there are new
  suggestions (leading to the Suggested groups page).
- In the inbox, files that look like parts of one broadcast are shown
  together in the review list, so an approved group goes into the
  library as one episode.
- Notes: "new" means not yet shown on the Suggested groups page (opening
  it counts as seeing them; `group_seen` table, database version 18). In
  the inbox the same rules apply, with the inbox counting as one folder
  and the dates worked out for each file (from its name or tags) having
  to agree; files from an Airwave export are left out (they know their
  own parts). Each suggested group sits in a box with a tick box
  (ticked to start) and **Dismiss**. While ticked, **Add all**, or
  **Add to library** on any of its files, adds the whole group: all go
  into the first part's folder, then they're grouped with part 1's
  details (a trailing "Hour 1" is dropped from its title). Unticked or
  dismissed, they go in one by one. A set dismissed in the inbox isn't
  suggested again once it's in the library.
- **Try it:** upload "Hour 1", "Hour 2", "Hour 3" of one show, leave the
  group ticked, press Add all: one 3-part episode.

### Phase 13 — Converting other formats with ffmpeg
Prompted by Lone Ranger episodes in `.mkv` (2026-11-07). The Mac's upload
window greys them out, because the inbox only accepts audio file types.

**Decisions (owner, 2026-11-08):**
- Converted: formats that can't be used or play badly: MKV/MKA, WebM,
  MP4 video, RealAudio (`.ra`, `.rm`), WMA, Ogg (`.ogg`, `.oga`), Opus.
  ~~Lossless FLAC, WAV and AIFF are left alone (they play nearly everywhere;
  converting would lose quality).~~ *Changed 2026-11-09 (13e):* FLAC, WAV
  and AIFF are converted too (podcast apps can't play them, and they're
  very big).
- Sound that's already MP3 or AAC is copied unchanged into `.mp3` /
  `.m4a`; everything else becomes MP3 (mono if the original is mono, at
  a bitrate with a little headroom over the original's, at most 128 kbps).
- The inbox deletes the uploaded original once its conversion succeeds.
- Scanning files added straight to the library is an advanced technique:
  odd formats found that way are converted in place and the originals
  deleted, after warnings and a confirmation page (see section 3).
- The upload window's file filter must let every one of these formats be
  picked (extensions plus the type names systems use; and a "Let me
  choose any file" box that turns the filter off as a fallback).
- Converting runs in the background, and the owner is told the files
  won't be available until it's done.

#### 13a — ffmpeg in the app, and what's inside each file ✅ *done*
- ffmpeg is in the Docker image (Debian's package: the image grew from
  about 277 MB to 905 MB). Where it is comes from `AIRWAVE_FFMPEG` (the
  program's full path) or the usual places; without it the app still
  runs and says converting isn't available.
- The inbox accepts the formats above (uploads, zips, and files copied
  into the inbox folder). For each, ffprobe looks inside and the inbox
  says what it is and what converting will do ("WebM file (a still
  picture with sound). Its sound is Opus, 40 kbps, mono. It will be
  converted to MP3 (mono, 56 kbps)."), or that it can't be read. Their
  tags and length are read with ffprobe when the usual reader can't.
- Until 13b they wait in the inbox: no Play or Add button, and Add all
  leaves them. The plan is kept on the inbox item (`convert_json`,
  database version 19). Code: `app/convert.py`.
- **Try it:** upload one of the Lone Ranger `.webm` files and read what
  the inbox says about it.

#### 13b — Converting in the inbox ✅ *done*
- A work queue (kept in the database, so it survives a restart) that
  converts one or two files at a time in the background; a notice on
  upload that the files need converting and will appear when ready
  (the page can be left); progress for each file; Cancel; plain error
  messages. The original is deleted once the new file has been checked.
  Converted files then join the usual inbox list (and group suggestions).
- Notes: one file at a time (measured on the dev VM: about 32 seconds to
  re-encode 40 minutes of Opus to MP3, under a second to copy MP3 sound
  out of a container). Files join the queue as soon as they're in the
  inbox (uploads, unpacked zips, and files copied into the folder: the
  helper looks there every 30 seconds). The queue is
  `inbox_items.convert_status` (waiting / converting / failed /
  cancelled), with progress and the error (database version 20); a
  restart puts a half-done file back to waiting and clears half-made
  files. The new file is made under a hidden name, checked (it opens,
  holds sound, and its length is within 2 seconds or 1% of the
  original's), then takes the original's name with the new extension (a
  number is added if taken); the original is deleted and the waiting
  file's details, including anything typed, move to the new file. The
  inbox shows a box that updates every 3 seconds; when it's done,
  "Show the converted files" saves what was typed and reloads. Each file
  has Cancel converting (or Try again after a failure or cancel); a
  failure keeps the original and shows why. Code: `app/converting.py`
  (the queue and helper) and `app/convert.py` (the ffmpeg command and
  the check).

#### 13c — Pictures from "videos" that are just a still image ✅ *done*
- That picture becomes the episode's artwork. Better still-picture
  detection (some files hold a picture as an unchanging video track).
- Notes: the picture is stored inside the new MP3 or M4A as its cover
  art (front cover), so the usual artwork reading shows it once the file
  is added; nothing else changes. A "still picture" is cover art, a
  one-frame track, or (checked just before converting) a video whose
  tiny grey snapshots at 10%, 50% and 90% of the way through differ by
  less than 1.5 out of 255 on average (about a second to check).
  Blank pictures (a black screen) are skipped; pictures wider than 1000
  dots are made smaller. Moving video is left out, as before. Lengths
  now come from the sound itself, not the whole file (a video track can
  run on longer than the sound, which made the length check fail).

#### 13d — Converting odd formats found by a scan (advanced) ✅ *done*
- A scan notices library files in these formats and offers to convert
  them; a confirmation page lists each file, what it becomes, and warns
  the original will be deleted. The same queue converts them beside the
  original under a hidden name; once checked, the new file takes its
  place and the original is deleted. Episode details, listening place,
  groups and feeds move to the new file. Failures leave the original.
- Notes: scans now include MKV/MKA, WebM, MP4 video, RealAudio, WMA, Ogg
  and Opus files (ffprobe reads their length and tags if the usual reader
  can't, and works out the plan: `audio_files.convert_json`). The Library
  page says how many there are, with **Review and convert…** (also
  **☰ Actions → Convert odd formats…**), leading to `/library/convert`:
  a red warning, then each file, what it becomes and what happens, with
  tick boxes (ticked to start) and **Convert the ticked files and delete
  the originals**. The queue is the `library_conversions` table
  (database version 21); the same helper works through it after the
  inbox's files, one at a time. The new file is made beside the original
  under a hidden name and checked; then, with the scan lock held (so a
  scan never sees it halfway), it takes the original's name with its new
  extension (a number is added if taken), the original is deleted, and
  the app's record of the file is updated to describe the new one. The
  record (and its number) stays the same, so the episode, its details,
  the listening place, the file's part in a group, and feeds (whose
  items are known by the file's number) all carry on. Nothing is
  replaced if the original changed meanwhile. The Library page shows
  progress (updating every 3 seconds) with **Stop converting**; failures
  are counted there and explained on the convert page, where they can be
  ticked to try again.

#### 13e — Lossless FLAC, WAV and AIFF are converted too ✅ *done*
- **Decisions (owner, 2026-11-09):** FLAC, WAV and AIFF join the
  converted formats: Apple Podcasts and many podcast apps can't play
  them, and they're several times bigger than they need to be. Not just
  old-time radio: modern broadcasts and music too, so lossless sound
  becomes MP3 at **96 kbps mono** or **192 kbps stereo** (a steady
  bitrate, so skipping and resuming land in the right place). MP3 and
  AAC are still never converted, and other lossy formats keep the rule
  above (a little over the original's, at most 128 kbps: more can't
  bring back what was lost).
- Notes: lossless means FLAC, Apple Lossless, WavPack, Monkey's Audio,
  TTA and uncompressed ("PCM") sound, in any container. WAV uploads are
  now converted in the inbox like the rest. Library files are offered
  by their file name, so ones scanned earlier (85 Amos and Andy FLACs on
  the dev VM, 2.4 GB) show up on the Library page without a rescan; they
  are only converted after the confirmation page. In the tests, small
  WAVs stand in for files that play fine, so WAV isn't converted there
  unless a test is marked `wav_converts` (`tests/conftest.py`).

**Background notes (2026-11-07):**

Why just allowing `.mkv` isn't enough: MKV is a *container* (video, audio,
or both). Safari, iPhones, and podcast apps can't play it, and mutagen
can't read its length or tags. The audio has to come out of the container.

The plan: **ffmpeg** (the standard free converter) inside the inbox.
- Uploads also accept `.mkv`, `.mka`, `.webm`, `.mp4` (video), and `.ra`
  (RealAudio, common on older OTR sites).
- The inbox pulls out the audio track. If it's already MP3 or AAC, it's
  **copied unchanged** (no quality loss, fast) into `.mp3` or `.m4a`.
  Otherwise it's converted to a format every browser, phone, and podcast app
  plays. The owner's suggested default is MP3, at a quality matching the
  original.
- If the "video" is just a still picture (typical of YouTube downloads),
  that picture can become the episode's artwork.
- What becomes of the original: *decided 2026-11-08, see above* (deleted
  from the inbox once conversion succeeds).
- ~~The ground rules stay intact: conversion happens only in the inbox.~~
  *Changed 2026-11-08:* files found by a scan can be converted in place
  (13d), as a second exception in section 3.
- ffmpeg goes into the Docker image (a few hundred MB). It also exists for
  Windows and Mac, so desktop packaging still works, and its licence is
  compatible with GPL v3.
- Meanwhile, a batch can be converted by hand with ffmpeg on the dev VM and
  then uploaded as normal.

### Later — The Advanced page (`/advanced`, after v1.0)
A page of **power-user tools** that work on the whole library at once
(owner's request, 2026-11-07). Examples:
- converting the entire library's odd formats (see ffmpeg above)
- writing tags into every file (Phase 10a)
- renaming and organising every file (Phase 10b)
- and similar bulk jobs added later

How it works:
- **Reached only by typing the address**: `http://<server>:8000/advanced`.
  No link to it anywhere in the app, not in menus, the footer, or help text.
- **A big red bar across the top** of the page saying, in effect: *"These
  tools change your library files. You're on your own: keep backups and
  take a snapshot first."*
- Each tool still follows the usual safety steps: a **preview** of exactly
  what will change, an explicit confirmation, progress shown, the ability to
  stop partway (files left either fully done or untouched), and an undo
  record where possible (e.g. renames).
- The everyday pages keep the small-scale versions (one episode, ticked
  episodes, a series). Whole-library versions live only here.
- **Ground rules:** these tools change existing library files, so each one
  is an owner-approved exception to "never change files in the library."
  The exceptions get written into section 3 when each tool is built.
- Note: an unlinked address is a **"keep out of the way" measure, not a
  lock**. Anyone on the network who knows the address can open it. That's
  fine for a single household, but it isn't security.

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

### Later — Look up episode details online (after v1.0)
The dream: fill in air dates, titles, and cast from public OTR sources
instead of typing them. Research notes (2026-11-07): no ready-made "TV
guide for old radio" service exists, but these sources are worth knowing:

| Source | What it has | Can a program ask it? |
|--------|-------------|-----------------------|
| **Internet Archive** (archive.org) | Huge OTR holdings, including many OTRR-checked sets; series, titles, often dates (usually in file names) | **Yes**: free, documented metadata and search API. The best first source. |
| **Wikipedia / Wikidata** | Episode tables for major series (e.g. "List of The Shadow episodes"); Wikidata mostly whole series | Yes, but tables must be picked apart. Free with credit (CC BY-SA). |
| **OTRR episode database** (OTTER) | Aims to log every Golden Age episode | No API found. If they offer an export, it can come in through the CSV import (Phase 6c). |
| **RadioGOLDINdex** (UMKC Libraries) | The most detailed: cast, dates, notes | No API or download. Don't copy pages automatically; ask David Goldin / UMKC about an export. |

How it would work:
- A **"Look it up online"** action on a series or episode, used only when
  pressed. **Off by default**: this is an exception to the "no cloud
  services" ground rule, so it needs the owner's OK when it's built.
- It sends only a series name or episode title, never anything about the
  owner or their files.
- Results are shown as **suggestions with their source named**, side by side
  with what's already there. The owner accepts or rejects each one. Nothing
  is overwritten, and manual edits always win.
- Sources disagree (one *Suspense* episode has two different air dates in
  two logs), so the source shown on each suggestion matters.
- Partial dates stay partial. A lookup never "completes" a date unless a
  source actually gives the full date.

### Later ideas (not committed)
- Multiple library folders, playlists / "collections", backups of the
  database, podcast RSS feed subscriptions. (Online episode logs: see
  "Look up episode details online" above.)
