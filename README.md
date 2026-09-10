# PTO Tracker

A tiny, self-hosted PTO/vacation tracker: single login, SQLite storage, and
German public holidays baked in for all 16 federal states. No accounts
system, no database server, no build step — just Python and SQLite.

![Dashboard screenshot](screenshot.png)

## Features

- **Single login.** First visit walks you through creating one admin account
  (hashed password, no config file editing required).
- **Light or dark, your choice.** Follows your browser/OS setting
  automatically by default; override it to always-light or always-dark in
  Settings if you'd rather it not follow the system.
- **An actual dashboard, separate from PTO management.** `/` is a quick
  overview — compact PTO/overtime/sick-leave tiles and upcoming holidays —
  while adding, editing, and browsing PTO entries lives on its own PTO tab,
  the same way Overtime and Sick Leave already have theirs. Both the PTO
  and Overtime tiles also show the next upcoming not-yet-taken entry, so
  you don't have to click into Details just to see what's coming up.
- **German public holidays, computed automatically.** Pick your federal state
  in Settings (defaults to Rheinland-Pfalz) — weekends and that state's
  holidays are excluded automatically when counting days used, for any year,
  no yearly maintenance.
- **Planned, approved, or taken.** Every entry has a status. All three count
  against your balance, and the PTO tab breaks out how many of the used
  days are taken-or-approved versus still just planned (approved counts the
  same as taken there — both are committed) — change the status inline as
  plans firm up. Entries can be edited or deleted after the fact, and
  overlapping date ranges are rejected.
- **Half-day entries.** Mark the first or last day of an entry as a half day
  (e.g. leaving early or coming in late) and it counts as 0.5 days/hours
  instead of a full one.
- **Entries spanning New Year's are split automatically**, one part per
  calendar year, so each year's balance only reflects its own days — the
  PTO tab shows a "continues into"/"continued from" note linking the parts,
  and editing, deleting, or changing status on either part acts on both.
- **Automatic carryover.** Whatever's left of one year's balance
  (allowance + its own carryover − used) rolls into the next year on its own.
  You can still override it per year (e.g. if your employer caps carryover).
- **Per-year allowance overrides**, for contract changes etc.
- **A yearly-overview stats page** — every year with any data (entries or an
  allowance/carryover override) in one table: allowance, carryover, PTO used
  (with a quick visual bar), taken/approved vs. still-planned, remaining,
  overtime hours, and sick days if that's turned on.
- **Overtime tracking**, on its own tab. Track two independent hour balances
  (e.g. a main account and a second bank like AMA) in H:MM format, set your
  weekly paid hours, and log time off taken against either account. Only
  entries not yet marked "taken" count against the balance shown, so you can
  freely re-sync the balance from your employer's system without
  double-counting anything already reflected in it.
- **Optional sick-leave tracking.** Turn it on in Settings to get a third,
  separate log for sick days (Krankheitstage), on its own tab. Same
  half-day and New-Year's-split handling as PTO, but no status and no
  balance math — it's a record, not something that counts against your
  allowance. A day can only belong to one of PTO/overtime/sick leave at a
  time; overlapping any of the three against another is rejected.
- **Optional calendar view.** Turn it on in Settings to get a calendar grid
  alongside the list view, with PTO, overtime, sick leave, and public
  holidays all marked on it. It automatically shows as many months as
  needed (up to 6) so a vacation spanning multiple months — or across New
  Year's — is always visible in full, no manual range-picking required; an
  arrow on the edge day marks the rare case where it still runs past that.
- **Optional calendar feed (.ics).** Turn it on in Settings to get a
  subscribable URL for your PTO, overtime, and sick-leave time off, so it
  shows up in your phone or desktop calendar app. Gated by a random token
  in the URL rather than a login (calendar apps can't do interactive
  auth), with a "Regenerate link" button if it ever needs to be
  invalidated. The app itself stays LAN-only as before — reaching this
  feed from outside your network is a networking choice you make
  separately (e.g. a tunnel scoped to just this one path), not something
  the app does for you.
- **Optional automatic backups.** Turn it on in Settings to keep a rolling
  set of snapshots on disk — pick how often (every N days) and how many to
  keep. Runs in the background regardless of whether you have the app open,
  with a "Back up now" button for an on-demand one and a list of stored
  backups to download or delete. On top of (not instead of) the manual
  "Download backup" button, which still works the same as always.
- **CSV export and import** for PTO, overtime, and sick-leave entries, each
  on its own tab — import uses the exact same columns the export produces,
  so round-tripping or backfilling from a spreadsheet just works. It's
  best-effort: a bad row (unparseable dates, an overlap with something
  already there or with an earlier row in the same file) is skipped and
  reported rather than failing the whole import.
- Mobile-friendly — usable on a phone down to a 320px-wide screen.
- No JavaScript framework, no external CDN dependency — works entirely on
  your own network.

## Quick install (Debian/Ubuntu host or LXC container)

```bash
git clone https://github.com/Boisti13/pto-tracker.git
cd pto-tracker
sudo ./install.sh
```

The installer is interactive — it asks for an install directory, port, and
service user (sensible defaults for all three), then sets up a Python venv,
installs dependencies, and registers a systemd service that starts on boot.
Re-running it later updates the app in place and leaves your data untouched.

Once it's running, open `http://<host>:5000/` and follow the first-time setup
to create your login.

## Manual install

If you'd rather do it by hand, or aren't on Debian/Ubuntu:

```bash
sudo apt install -y python3 python3-venv python3-pip
sudo useradd -r -m -d /opt/pto-tracker -s /usr/sbin/nologin pto

git clone https://github.com/Boisti13/pto-tracker.git /opt/pto-tracker
cd /opt/pto-tracker
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
sudo chown -R pto:pto /opt/pto-tracker

sudo cp pto-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pto-tracker
```

## Deploying in a Proxmox LXC

The installer works well inside a fresh, unprivileged Debian 12 LXC container:

```bash
# on the Proxmox host
pveam download local debian-12-standard_12.7-1_amd64.tar.zst
pct create 111 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname pto-tracker --cores 1 --memory 512 --swap 512 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --rootfs local-lvm:4 --unprivileged 1 --onboot 1 --start 1

pct exec 111 -- bash -c "apt update && apt install -y git"
pct exec 111 -- git clone https://github.com/Boisti13/pto-tracker.git /root/pto-tracker
pct exec 111 -- /root/pto-tracker/install.sh
```

## Configuration

Everything lives in one SQLite file (`data/pto.db` under the install
directory, overridable with the `PTO_DB_PATH` environment variable). There's
no other config file — admin credentials, holiday state, allowance, and
carryover overrides are all managed from the Settings page in the web UI.

## Notes

- **LAN-only by design.** There's no HTTPS, and CSRF protection is a minimal
  session-token check rather than anything hardened — fine for a personal
  tool reachable only on your own network. If you want to reach it from
  outside your LAN, put it behind a reverse proxy (e.g. Caddy, nginx, or
  Nginx Proxy Manager) with TLS rather than exposing the port directly.
- **Backups.** Download a full copy of the SQLite file any time from the
  "Backup" section of Settings (safe to do while the app is running), turn
  on automatic backups there too (stored in `data/backups/`), or back up
  `data/pto.db` yourself however you like (snapshot, cron `cp`, etc.).
- **Changing the password.** Settings has an in-app "Change password" form
  if you know your current one. If you've forgotten it: stop the service,
  delete the `admin_username` / `admin_password_hash` rows from the
  `settings` table in `pto.db` (or delete the DB file entirely to start
  over — this also wipes your PTO/overtime data, so prefer the two-row
  delete), then restart so `/setup` runs again.

## License

[MIT](LICENSE)
