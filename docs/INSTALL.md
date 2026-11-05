# Installing Airwave Archive

This guide sets up Airwave Archive on an **Ubuntu Server** machine, such as a
virtual machine (VM) on Proxmox. Every command goes in the server's terminal,
either at the console or over SSH. Type or paste each command block, press
**Enter**, and wait for it to finish before going on.

> Commands starting with `sudo` run as the administrator. Ubuntu will ask for
> your password the first time. Nothing appears while you type it; that's
> normal.

Recommended machine: 2 CPU cores, 4 GB RAM, a 32 GB boot disk, and a
**second disk** for recordings. See "Recommended server" in
[`PLAN.md`](PLAN.md).

---

## Step 1 — Prepare the recordings disk

This formats the second disk and makes it appear at `/srv/airwave`.

> ⚠️ Formatting erases a disk. Make sure you pick the **empty** second disk,
> not the one Ubuntu is installed on.

1. List the disks:

   ```bash
   lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS
   ```

   You'll see something like this:

   ```
   NAME    SIZE TYPE MOUNTPOINTS
   sda      32G disk
   ├─sda1    1G part /boot/efi
   └─sda2   31G part /
   sdb     500G disk
   ```

   The recordings disk is the one with **no partitions and no mount
   points**: `sdb` in this example. Below, replace `sdb` with your disk's
   name if it's different.

2. Format it (creates an empty ext4 filesystem):

   ```bash
   sudo mkfs.ext4 -L airwave /dev/sdb
   ```

3. Create the folder it will appear at, and set it to mount automatically at
   every boot:

   ```bash
   sudo mkdir -p /srv/airwave
   ```

   ```bash
   echo "LABEL=airwave /srv/airwave ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
   ```

   ```bash
   sudo systemctl daemon-reload && sudo mount -a
   ```

4. Check it worked:

   ```bash
   df -h /srv/airwave
   ```

   The size shown should match your recordings disk.

5. Create the app's folders and make them yours:

   ```bash
   sudo mkdir -p /srv/airwave/library /srv/airwave/inbox /srv/airwave/data
   ```

   ```bash
   sudo chown -R 1000:1000 /srv/airwave
   ```

   (`1000` is the user number the app runs as. On a fresh Ubuntu install, the
   first user you created is also `1000`, so your files and the app's files
   match.)

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

The project's GitHub repository is private, so the server must sign in to
GitHub before it can download it. (If the repository is public, skip to
item 4 and use `git clone https://github.com/cainram/airwave-archive.git`.)

1. Install the GitHub command-line tool:

   ```bash
   sudo apt-get install -y gh
   ```

2. Sign in to GitHub:

   ```bash
   gh auth login
   ```

   It asks a few questions. Use the arrow keys and **Enter** to choose:
   - Where do you use GitHub? → **GitHub.com**
   - Preferred protocol → **HTTPS**
   - Authenticate Git with your GitHub credentials? → **Yes**
   - How would you like to authenticate? → **Login with a web browser**

   It then shows a one-time code (like `ABCD-1234`). On any computer or phone,
   open https://github.com/login/device, sign in to GitHub, and type the code.
   Back in the terminal you should see "Logged in as …".

3. Check it worked:

   ```bash
   gh auth status
   ```

4. Download the project into your home folder:

   ```bash
   cd ~ && gh repo clone cainram/airwave-archive
   ```

5. Build and start it:

   ```bash
   cd ~/airwave-archive && docker compose up -d --build
   ```

   The first time takes a minute or two while it downloads and builds.
   `-d` means "keep running in the background."

6. Open it in a browser: `http://<server-address>:8000`

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

## Back up the data folder

The app keeps its database in `/srv/airwave/data`. It's small, and it will
hold all the information you add, so include it in your backups. (The app
never changes, renames or deletes the recordings in `/srv/airwave/library`;
it only adds the files you approve in the inbox. Back up your recordings
too.)

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
