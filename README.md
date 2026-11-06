# Airwave Archive

A self-hosted library and calendar for radio program recordings: old-time
radio, talk radio, podcasts.

Think of it as a music library built around radio. It knows about original air
dates (even partial ones), stations and networks, series, hosts, and cast, and
it has a calendar showing everything in your collection that aired on a given
day, week, or month.

- Runs on your own server (homelab-first), for one household.
- Reads your recordings where they sit; never changes your library files
  (it deletes them only when you ask, after confirming).
- Hosts no content. You bring your own recordings.

This is a personal project, shared as-is. You're welcome to use or fork it
under the GPL, but it isn't run as a community project, so there's no
promise of support or of acting on feature requests.

**Status:** early development, Phase 1. It scans a folder and lists the
audio files on a web page. See the [project plan](docs/PLAN.md) for what's
coming.

## Install

See [docs/INSTALL.md](docs/INSTALL.md). In short, on an Ubuntu server with
Docker and your recordings in `/srv/airwave/library`:

```bash
git clone https://github.com/cainram/airwave-archive.git
cd airwave-archive
docker compose up -d --build
```

Then open `http://<server-address>:8000`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
AIRWAVE_LIBRARY=/path/to/recordings uvicorn app.main:app --reload
```

## License

[GNU GPL v3](LICENSE).
