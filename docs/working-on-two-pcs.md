# Working on two PCs

How to keep entering charts on more than one computer without losing work.

## The one fact everything depends on

All the data is in **one file, `billboard.db`**: every chart entry, recording,
artist, Discogs/Spotify link and playlist. Git stores that file whole, as a binary
blob. It **cannot merge** two versions of it the way it merges two versions of a
`.py` file.

So if PC 1 enters week A and PC 2 enters week B, each on its own copy, git can only
keep **one of the two files**. The other PC's week is lost unless you re-type it.
Every option below is a different way around this:

| Option | Both PCs at the same moment? | Works offline? | Setup | Recommended for |
|---|---|---|---|---|
| **A. Take turns through GitHub** | No, one at a time | Yes | Once per PC | **Default.** Home PC and laptop, used on different days |
| **B. One PC serves, the other connects** | Yes | Only on the same network (or via Tailscale) | Small settings change | Two people entering at once, or the laptop in the next room |
| **C. Separate copies, merged later** | Yes | Yes | **Not built yet** | Only if A and B really don't fit |
| D. A synced folder (OneDrive / Dropbox / Google Drive) | — | — | — | **Don't.** See below |

---

## Option A: take turns through GitHub (recommended)

This is how backups already work (README → "Backups"). It is extended to a second
clone of the same repo. The rule is simple:

> **Pull before you start. Commit and push when you stop.**
> Only one PC "holds" the database at a time.

### One-time setup on the second PC

1. **Install Python 3.12 or newer** (Django 6.1 requires it) from python.org. Tick
   "Add python.exe to PATH". The main PC runs 3.14.
2. **Install Git for Windows** from git-scm.com. It includes Git Credential
   Manager, so the first push opens a browser window to log in to GitHub (the repo
   is private).
3. **Tell git who you are** (shown on commits):
   ```bash
   git config --global user.name "nlopezk"
   git config --global user.email "<your GitHub email>"
   ```
4. **Clone the repo** wherever you like, for example:
   ```bash
   git clone https://github.com/nlopezk/Billie.git D:\jackie\Billie
   cd D:\jackie\Billie
   py -m pip install -r requirements.txt
   ```
5. **Copy the secrets by hand.** Git deliberately does not carry them. Use a USB
   stick, a password manager, or retype them:
   - `.env` holds `DISCOGS_TOKEN`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and
     `SPOTIFY_MARKET`. Without it the chart pages work fine; only `/discogs/` and
     `/spotify/` need it.
   - `.spotify_token.json` is your Spotify login, used for pushing playlists. You can
     copy it, or press **Connect Spotify** on `/spotify/` on the new PC. The same
     redirect URI (`http://127.0.0.1:8000/spotify/callback/`) works on every PC,
     because 127.0.0.1 always means "this computer".
6. **Start it** by double-clicking `Billboard Charts.bat`. The admin login is the
   same as on the main PC, because users are stored inside `billboard.db`.

`Bb Charts fixed.xlsx` is not needed on the second PC. It was only used for the
one-time import.

### Every session, on whichever PC you sit at

**Before you start:**

```bash
cd D:\jackie\Billie
git pull
python manage.py migrate     # normally "No migrations to apply" -- harmless, and
                             # catches the rare case where new code came down
```

Then start the server (`Billboard Charts.bat`).

`git pull` should answer `Already up to date.` or `Fast-forward`. If it shows
anything else, stop and read "If it goes wrong" below before entering anything.

**When you stop:**

1. **Close the server window first.** That makes sure nothing is half-written to
   the file while git copies it.
2. Then:
   ```bash
   git add billboard.db
   git commit -m "Chart entry: Country Jockeys 1951-04 to 1951-06"
   git push
   ```
   If code changed too, for example after a Claude session on that PC, commit it
   separately first (`git add -A`, commit, then the data commit). Same habit as
   now: code commits and "Data: …" commits are kept apart.

**The habit that makes this safe:** never walk away from a PC with an unpushed
database. If you forget, the other PC will start from an old copy, and you're in the
"If it goes wrong" case.

### Checking where you stand

```bash
git status          # "modified: billboard.db" = you have unsaved (uncommitted) work
git fetch
git status          # "Your branch is behind" = the other PC pushed; pull before working
                    # "Your branch is ahead"  = you committed but didn't push yet
```

### If it goes wrong (both PCs changed the database)

This shows up as one of these messages:

- `git pull` → *"Your local changes to the following files would be overwritten: billboard.db"*.
  You have uncommitted work here, and the other PC pushed too.
- `git push` → *"rejected … fetch first"*, then `git pull` → *"CONFLICT (content): Merge conflict in billboard.db"*.
  You committed here, and the other PC pushed too.

Nothing is lost yet. You have two complete databases, and you need to choose one
and re-type the other's few weeks into it. Do this:

1. **Save your copy outside the repo**, so nothing below can touch it:
   ```bash
   copy billboard.db D:\jackie\billboard-this-pc-2026-11-23.db
   ```
2. **Take the other PC's version** (the pushed one becomes the master):
   - If the message was "would be overwritten":
     ```bash
     git restore billboard.db
     git pull
     ```
   - If the message was "Merge conflict":
     ```bash
     git checkout --theirs billboard.db
     git add billboard.db
     git commit -m "Data: keep the other PC's database (merge)"
     ```
     During a pull, "theirs" means the version that came from GitHub.
3. **Look at your saved copy side by side.** Copy the whole project folder somewhere
   else, for example `D:\jackie\Billie-old`. Put your saved `.db` in it, named
   `billboard.db`. Start that copy on a different port:
   ```bash
   cd D:\jackie\Billie-old
   python manage.py runserver 127.0.0.1:8001
   ```
   Now `http://127.0.0.1:8000/` is the master and `http://127.0.0.1:8001/` is your
   old copy. Open the weeks you entered on the old copy and re-enter them on the
   master. That is ordinary data entry, which this project wants to be manual anyway.
4. Commit and push the master. Delete `Billie-old` and the saved `.db` once you're
   sure.

Which one to keep: usually the one with **more** new work, since you re-type the
smaller side. If the other PC's work is the smaller side, swap the roles in steps
2–3: keep yours (`git checkout --ours`, or after "would be overwritten": commit
yours first, then pull and resolve with `--ours`), and re-type theirs.

### Costs to know about

- Each data commit stores a full copy of the file, about 14 MB now, in the repo's
  history forever. Commit once per session, not once per week entered. That's
  roughly 1 GB per 70 sessions. GitHub warns at around 5 GB per repo, so this is
  fine for years but not free.
- A fresh clone downloads all of that history (54 MB today).

---

## Option B: one PC serves, the other connects with a browser

The database stays on **one** PC (the "host"). The second PC runs no code at all. It
opens the host's pages in its browser, exactly like opening `127.0.0.1:8000`, but
using the host's network address. There's one file and one writer, so there's
nothing to merge, and two people can enter at the same time.

**Limits:**
- The host must be switched on, with the server running.
- The second PC must be on the same network: the same home Wi-Fi, or anywhere
  through Tailscale (below).
- Backups (Option A's commit and push) happen **on the host only**.

### Setup (not done yet; a small change)

1. **Let Django answer to more than `127.0.0.1`.** In `config/settings.py`,
   `ALLOWED_HOSTS = []` currently means "localhost only". It would become, for
   example:
   ```python
   ALLOWED_HOSTS = ["127.0.0.1", "localhost", "192.168.1.50"]   # the host's LAN address
   ```
   You can find the host's address with `ipconfig` ("IPv4 Address").
2. **Start the server listening on the network**, not just on itself:
   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```
   The `.bat` launcher would get a second "network" version for this.
3. **Allow it through Windows Firewall.** Windows asks the first time. Tick
   **Private networks** only, never Public.
4. On the second PC, open `http://192.168.1.50:8000/`.

**From outside the house:** install **Tailscale** (free for personal use) on both PCs.
Each PC gets a fixed private address like `100.x.y.z` that works from anywhere, and
the address is not exposed to the internet. Use that address instead of `192.168…`,
and add it to `ALLOWED_HOSTS`. The data still never leaves the host PC; Tailscale only
connects the two machines.

**Things to know:**
- **Anyone on that network can edit**, because the entry form has no login. That's
  fine on your own Wi-Fi, but never do this on a café or hotel network.
- This is Django's development server with `DEBUG = True`. It's fine for two people
  at home, but it is not a real web server. Don't expose it to the internet: no port
  forwarding on the router, use Tailscale instead.
- **Spotify Connect only works when done at the host's own keyboard.** Spotify
  only accepts the `127.0.0.1` callback. Once connected, pushing a playlist works
  from either PC.
- Two people saving at the very same second can rarely get "database is locked".
  Pressing Save again fixes it.
- Two people **editing the same chart week**: the last save wins, because an edit
  replaces that week. Entering the same **new** week twice is already refused.

---

## Option C: separate copies, merged later (not built)

This is the only way to work **on both PCs at the same time, both offline**. It needs a
real tool that doesn't exist yet:

- An **export** of the weeks entered on PC 2 since the last sync, as a small text
  file, identifying everything by its natural key rather than its database number:
  chart + week date for an entry, label + catalog # + side for a recording, name for
  an artist.
- An **import** on PC 1 that adds those weeks. It creates the recordings and artists
  it doesn't already have, and stops and asks on anything ambiguous (the same rule as
  the entry form: never silently guess or merge).

The hard part is that both PCs number new recordings and artists independently.
Recording #1300 on PC 1 and #1300 on PC 2 can be two different records, which is why
copying rows or using SQLite's `sqldiff` would corrupt data. It's doable and would
follow the project's rules, but it's a real feature. Worth building only if you
genuinely enter on two machines at the same time with no network between them.

---

## D: why not a synced folder (OneDrive, Dropbox, Google Drive)

It looks easiest: put the project in OneDrive and let it sync. Don't, for three
reasons:

- **SQLite plus file sync corrupts databases.** The sync client can upload the file
  halfway through a write, or while the other PC has it open.
- When both PCs changed it, you get `billboard-PC2-conflict.db` copies. That's the
  same problem as Option A, but silent, and with no history to recover from.
- It moves the database into a cloud service, against the project's "the database
  stays local" rule. GitHub is the agreed exception, as the backup.

---

## Summary

- **Different days on different PCs:** Option A. `git pull` → work → close the
  server → commit `billboard.db` → `git push`.
- **Both at once, same house:** Option B. One PC hosts and the other uses a browser.
  It needs the small `ALLOWED_HOSTS` + network-launcher change.
- **Both at once, both offline:** Option C. It needs building first.
