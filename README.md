# Airwave Archive

A self-hosted library, player and calendar for recorded radio: old-time
radio, talk radio and other broadcasts.

Think of it as a music library built around radio. It knows about
original air dates (even partial ones, such as just a year), series,
stations and networks, hosts and cast. It has a calendar of everything in
your collection that aired on a given day, and it turns your recordings
into private podcast feeds, so you can listen in a podcast app.

- Runs on your own server, for one person. No accounts, no cloud services,
  no tracking.
- Hosts no content. You bring your own recordings.
- Calm by design: few buttons, few settings, and maintenance tools kept
  out of the way.

Website: [airwavearchive.org](https://airwavearchive.org) · Downloads:
[Releases](https://github.com/cainram/airwave-archive/releases)

This is a personal project, shared as-is. You're welcome to use or fork it
under the GPL, but it isn't run as a community project, so there's no
promise of support or of acting on feature requests.

## What it does

### Browse and listen

- **A home page** with Continue listening, recently added series, most
  played, and rows for the genres with the newest additions.
- **Series pages** with artwork taken from the files and folders (or
  uploaded), episodes in original air-date order, genres you choose, and
  a few lines "About this series".
- **A player** that keeps playing while you browse, remembers where you
  stopped in each episode, and marks what you've played.
- **The calendar.** Everything that aired on a given day, like a TV guide
  for the past.
- **Search** across titles, series, genres, cast, hosts and notes.
- **A mobile version** for listening on a phone: the same library, with
  the curating tools left out.
- **Light and dark themes.**

### Podcast feeds

Private feeds that a podcast app (such as Apple Podcasts) plays straight
from your server. Each feed has a long secret address instead of a
password.

- **Series feeds** follow a series, and new episodes show up in them.
  **Hand-picked feeds** hold the episodes you tick.
- **Rolling feeds** release episodes on a schedule, so your phone gets
  a new one overnight instead of a whole series at once. You choose every
  day, weekdays, weekly, weekends or every few days, at a time in your
  time zone.
- **"On this day"** brings each day's anniversaries from the series you
  choose: broadcasts that first aired on that date, in any year.
- Add a feed to your phone with a QR code or a link.

### Getting details in

- **Episode details** (title, air date, station, hosts, cast and notes)
  filled in from the files' tags and names, with a review page that
  guesses series, dates and titles from file names.
- **Air dates are never invented.** If only the year is known, only the
  year is kept.
- **Bulk editing**, and importing episode logs from spreadsheets (CSV).
- **Broadcasts in parts.** "Hour 1", "Hour 2"... play as one episode, and
  overnight shows keep their real air date. The app suggests groups of
  files that look like parts of one broadcast, and can join an episode's
  parts into one file with a chapter at each part.
- **An upload inbox.** New recordings are uploaded from the browser (or
  copied into the inbox folder), checked and given details, then added to
  the library.
- **Converting formats that don't play everywhere.** Video files (MKV,
  WebM, MP4), RealAudio, WMA, Ogg, Opus, and large lossless files (FLAC,
  WAV, AIFF) are converted to MP3 (or the sound is copied out unchanged
  when it already plays everywhere). A "video" that's one still picture
  gives the episode its artwork.
- **Export and import.** Zips of chosen series or episodes, with their
  details written into the tags of the copies, which another Airwave
  Archive can import.

### Settings and the Advanced page

A **Settings** page (the cog in the top bar) holds your time zone. Tools
you rarely need live on a separate **Advanced** page: writing details
into every file, renaming and organizing the whole library, converting
odd formats, joining multi-part episodes, and exporting several series.

## Your files

The app reads your recordings where they sit, and only changes the
library when you ask:

- New files are added through the inbox.
- Recordings are deleted only when you choose to, after a confirmation
  page listing exactly which files will go.
- Converting deletes the original once the new file has been checked.
  Uploads are converted automatically in the inbox. Files copied straight
  into the library are converted only after a confirmation page that
  lists them and warns about it.
- Episode details are written into a file's own tags only when you ask,
  after a preview of every tag that will change. The sound isn't touched.
- Files are renamed and sorted into a folder per series (like
  `Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3`) only when you ask,
  after a preview of every old and new name. Renames can be put back.
- Joining an episode's parts deletes the parts once the joined file has
  been checked, after a preview of every episode.

Keep backups of your recordings and of the app's data folder.

## Install

See [docs/INSTALL.md](docs/INSTALL.md). In short, on an Ubuntu server with
Docker and your recordings in `/srv/airwave/library`:

```bash
git clone https://github.com/cainram/airwave-archive.git
cd airwave-archive
docker compose up -d --build
```

Then open `http://<server-address>:8000`.

To update later, run `git pull && docker compose up -d --build` in the
same folder. The database is updated automatically, and a backup copy of
the old one is kept.

The folders come from settings (environment variables), set in
`compose.yaml`: `AIRWAVE_LIBRARY` (your recordings), `AIRWAVE_INBOX`
(uploads waiting to be added) and `AIRWAVE_DATA` (the app's database, the
folder to back up). Converting and joining use ffmpeg, which the Docker
image includes; elsewhere, `AIRWAVE_FFMPEG` can point to it.

## Development

Python with FastAPI, server-rendered pages with htmx, and SQLite.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
AIRWAVE_LIBRARY=/path/to/recordings uvicorn app.main:app --reload
```

The tests that use the real ffmpeg are skipped if it isn't installed.
See the [project plan](docs/PLAN.md) for how the app is built and what's
planned.

## License

[GNU GPL v3](LICENSE).
