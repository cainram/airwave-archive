# Airwave Archive

A self-hosted library and calendar for radio program recordings: old-time
radio, talk radio and other broadcasts.

Think of it as a music library built around radio. It knows about original
air dates (even partial ones, such as just a year), series, stations and
networks, hosts and cast, and it has a calendar showing everything in your
collection that aired on a given day or month.

- Runs on your own server, for one household. No accounts, no cloud
  services, no tracking.
- Hosts no content. You bring your own recordings.

This is a personal project, shared as-is. You're welcome to use or fork it
under the GPL, but it isn't run as a community project, so there's no
promise of support or of acting on feature requests.

## What it does

- **Library and series pages.** Recordings are grouped by series, in
  air-date order, with artwork taken from the files and folders (or
  uploaded).
- **A player** that keeps playing while you browse, and remembers where
  you stopped in each episode ("Continue listening"), on a computer or a
  phone.
- **The calendar.** Everything that aired on a given day, month by month.
- **Episode details.** Title, air date, station, hosts, cast and notes,
  filled in from the files' tags and names, with a review page that
  guesses series, dates and titles from file names. Bulk editing, search,
  and importing episode logs from spreadsheets (CSV).
- **Broadcasts in parts.** "Hour 1", "Hour 2"... play as one episode, and
  overnight shows keep their real air date. The app suggests groups of
  files that look like parts of one broadcast.
- **An upload inbox.** New recordings are uploaded from the browser (or
  copied into the inbox folder), checked and given details, then added to
  the library.
- **Converting formats that don't play everywhere.** Video files (MKV,
  WebM, MP4), RealAudio, WMA, Ogg, Opus, and large lossless files (FLAC,
  WAV, AIFF) are converted to MP3 (or the sound is copied out unchanged
  when it already plays everywhere). A "video" that's one still picture
  gives the episode its artwork.
- **Export and import.** Zips of episodes with their details written into
  the tags of the copies, which another Airwave Archive can import.
- **Feeds for your phone.** Private feeds of series or chosen episodes, so
  a podcast app on your home network can play them.

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

The folders come from settings (environment variables), set in
`compose.yaml`: `AIRWAVE_LIBRARY` (your recordings), `AIRWAVE_INBOX`
(uploads waiting to be added) and `AIRWAVE_DATA` (the app's database, the
folder to back up). Converting uses ffmpeg, which the Docker image
includes; elsewhere, `AIRWAVE_FFMPEG` can point to it.

## Development

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
