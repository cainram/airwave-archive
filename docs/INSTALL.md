# Installing Airwave Archive

This guide sets up Airwave Archive on an **Ubuntu Server** machine, such as a
virtual machine (VM) on Proxmox. Every command goes in the server's terminal,
either at the console or over SSH. Type or paste each command block, press
**Enter**, and wait for it to finish before going on.

> Commands starting with `sudo` run as the administrator. Ubuntu will ask for
> your password the first time. Nothing appears while you type it; that's
> normal.

Recommended machine: 2 CPU cores, 4 GB RAM, and a disk with room for the
operating system (about 32 GB) plus your recordings and room to grow. See
"Recommended server" in [`PLAN.md`](PLAN.md).

---

## Step 1 — Create the app's folders

The app keeps everything in `/srv/airwave`: your recordings (`library`),
uploads waiting for review (`inbox`), and its own information (`data`).

1. Create the folders:

   ```bash
   sudo mkdir -p /srv/airwave/library /srv/airwave/inbox /srv/airwave/data
   ```

2. Make them yours:

   ```bash
   sudo chown -R 1000:1000 /srv/airwave
   ```

   (`1000` is the user number the app runs as. On a fresh Ubuntu install, the
   first user you created is also `1000`, so your files and the app's files
   match.)

3. Check it worked:

   ```bash
   ls /srv/airwave
   ```

   You should see `data  inbox  library`.

## Step 2 — Install Docker

Docker runs the app in a sealed container. Install it from Docker's own
source, **not** the Ubuntu "snap" version.

1. Add Docker's package source:

   ```bash
   sudo apt-get update && sudo apt-get install -y ca-certificates curl git
   ```

   ```bash
   sudo install -m 0755 -d /etc/apt/keyrings
   ```

   ```bash
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   ```

   ```bash
   sudo chmod a+r /etc/apt/keyrings/docker.asc
   ```

   ```bash
   printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")" | sudo tee /etc/apt/sources.list.d/docker.sources
   ```

   It prints five lines. The `Suites:` line should show your Ubuntu
   version's codename (like `noble`).

2. Install Docker:

   ```bash
   sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```

3. Let your user run Docker without `sudo`:

   ```bash
   sudo usermod -aG docker $USER
   ```

   Then **log out and log back in** (or reboot) for this to take effect.

4. Check it worked:

   ```bash
   docker run --rm hello-world
   ```

   You should see "Hello from Docker!".

## Step 3 — Download and start Airwave Archive

1. Download the project into your home folder:

   ```bash
   cd ~ && git clone https://github.com/cainram/airwave-archive.git
   ```

   If it says `git: command not found`, install it with
   `sudo apt-get install -y git` and try again.

2. Build and start it:

   ```bash
   cd ~/airwave-archive && docker compose up -d --build
   ```

   The first time takes a minute or two while it downloads and builds.
   `-d` means "keep running in the background."

3. Open it in a browser: `http://<server-address>:8000`

   Replace `<server-address>` with the server's IP address, or its Tailscale
   name if you use Tailscale. To find the IP, run `hostname -I` on the server.

   You should see the **Series** page. It will say there are no episodes
   until you add some recordings.

## Step 4 — Add some recordings

Copy audio files into `/srv/airwave/library/`, in any folder structure you
like. For example, from another Linux or Mac computer:

```bash
scp -r "/path/to/Suspense" youruser@<server-address>:/srv/airwave/library/
```

Then click **Library** in the menu at the top and press **Rescan**. The first scan reads every
file; later scans only look at files that are new or have changed, so they
are much faster.

For a few files at a time, you can also upload them in the app: click
**Inbox** in the menu. Uploads wait in `/srv/airwave/inbox/` (separate from
the library) until you add or reject them. Files copied into that folder
by hand show up there too. **Add to library** moves a file next to the
other recordings of its series (or into a new folder named after the
series, or `Unsorted` if it has none). A file with the same name is never
replaced: a number is added instead.

The inbox also takes **zips**: an export from another Airwave Archive (its
episodes arrive with all their details) or any zip of recordings. An
uploaded zip is unpacked straight away; a zip copied into the inbox folder
by hand gets an **Unpack** button (faster for very big zips). Unpacking
needs free disk space for the recordings, as well as the zip itself.

## Back up the data folder

The app keeps its database in `/srv/airwave/data`. It's small, and it will
hold all the information you add, so include it in your backups. (The app
only changes the recordings in `/srv/airwave/library` when you ask it to,
after a page showing exactly what will happen: adding the files you
approve in the inbox, deleting, converting odd formats, writing details
into files, or renaming and organizing them. Back up your recordings too.)

The `artwork` folder inside it holds the app's small copies of pictures
from your library. It doesn't need backing up: a scan makes the copies
again. The `uploaded-artwork` folder holds pictures you uploaded in the
app; those can't be made again, so keep them in your backups.

To give a series a picture, either open the series and press **Change
picture…** to upload one, or put a picture file named `cover.jpg` (or
`folder.jpg`, or `.png`) in its folder in the library and press
**Rescan**. An uploaded picture wins over one from the library.

When a new version of the app needs to change how its database is laid
out, it first saves a copy named like `airwave.db.before-v2` in the same
folder. Once the new version is working, you can delete these copies.

---

## Everyday commands

Run these from inside the `~/airwave-archive` folder.

| To… | Run |
|-----|-----|
| Update to the newest version | `git pull && docker compose up -d --build` |
| Stop the app | `docker compose down` |
| Start it again | `docker compose up -d` |
| See its log (for troubleshooting) | `docker compose logs --tail 50` |

## Troubleshooting

- **The page won't load.** Check that it's running with `docker compose ps`.
  The status should say "running" or "healthy". If not, look at
  `docker compose logs --tail 50`.
- **Some files are missing or show "unknown" length.** The app can only see
  files your user can read. Fix the permissions with:

  ```bash
  sudo chmod -R a+rX /srv/airwave/library
  ```

  Then press **Rescan**.
