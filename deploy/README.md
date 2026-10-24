# Deploying the Headway collector

This guide sets up the collector on an always-on Linux machine, for example a
Raspberry Pi 4/5 with an SSD, so it can run unattended for weeks. It assumes no prior
knowledge of the repository. Commands are for Raspberry Pi OS / Debian; run them as a user
with `sudo`.

What you get:

- `headway-collect.service`: polls Golemio every 20 s and restarts automatically.
- `headway-backup.timer`: a nightly rsync copy of the archive and a disk-usage check.
- Optional phone notifications through [ntfy](https://ntfy.sh) for outages, stale data,
  backup failures and a full disk.

## 1. Requirements

- A 64-bit Linux machine that stays on, with a stable network connection.
- Storage: about **370 MB per day** (~11 GB per month, ~135 GB per year), measured from a
  midday probe. A 256 GB SSD covers well over a year. Avoid running on an SD card alone:
  constant writes wear it out.
- A Golemio API key from <https://api.golemio.cz/api-keys> (free registration).
- Correct system time. File names and the manifest depend on it. Check it with:

  ```sh
  timedatectl            # "System clock synchronized: yes" is required
  ```

## 2. Prepare the data disk

Mount the SSD at `/srv/headway` (adjust the device name; `lsblk` lists disks):

```sh
sudo mkfs.ext4 -L headway /dev/sda1           # only for a new, empty disk: erases it
sudo mkdir -p /srv/headway
echo 'LABEL=headway /srv/headway ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /srv/headway
```

## 3. Create a service user and install the code

```sh
sudo apt-get update && sudo apt-get install -y git rsync curl
sudo useradd --system --create-home --shell /usr/sbin/nologin headway
sudo mkdir -p /opt/headway /srv/headway/raw /etc/headway
sudo chown headway:headway /opt/headway /srv/headway /srv/headway/raw

# Install uv for the headway user (https://docs.astral.sh/uv/).
sudo -u headway -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# Get the code and its locked dependencies. Python is installed under /opt so the
# hardened service (which hides home directories) can use it.
sudo -u headway git clone https://github.com/SkrabMartin/headway.git /opt/headway
cd /opt/headway
sudo -u headway -H env UV_PYTHON_INSTALL_DIR=/opt/headway/.python \
    /home/headway/.local/bin/uv sync --frozen --no-dev
```

## 4. Configure

```sh
sudo cp /opt/headway/deploy/collector.env.example /etc/headway/collector.env
sudo chmod 600 /etc/headway/collector.env
sudoedit /etc/headway/collector.env           # set GOLEMIO_TOKEN, optionally the ntfy URL
```

All other settings (interval, retries, thresholds) are in `/opt/headway/config/default.toml`
and can be overridden in the env file with `HEADWAY_<SECTION>__<KEY>=value`, for example
`HEADWAY_COLLECT__INTERVAL_S=30`.

## 5. Test one poll

This runs one poll as the `headway` user with the same env file the service will use
(the file is readable only by root, so it is loaded by systemd, as in production):

```sh
sudo systemd-run --wait --pipe --uid=headway --gid=headway \
    -p EnvironmentFile=/etc/headway/collector.env -p WorkingDirectory=/opt/headway \
    /opt/headway/.venv/bin/headway collect --once
echo "exit code: $?"                          # 0 means HTTP 200 and the body was stored
find /srv/headway/raw -name '*.json.gz' | head
```

The output is one JSON log line per request. A non-zero exit code with `status: 401`
means the API key is wrong.

## 6. Start the service

```sh
sudo cp /opt/headway/deploy/headway-collect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now headway-collect.service
systemctl status headway-collect.service
journalctl -u headway-collect.service -f     # one "poll" line every 20 s
```

## 7. Set up backups and disk monitoring

```sh
sudo cp /opt/headway/deploy/backup.env.example /etc/headway/backup.env
sudo chmod 600 /etc/headway/backup.env
sudoedit /etc/headway/backup.env              # set BACKUP_DEST, optionally NTFY_URL
sudo cp /opt/headway/deploy/headway-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now headway-backup.timer
sudo systemctl start headway-backup.service   # run once now
journalctl -u headway-backup.service -n 20
```

For an SSH destination, give the `headway` user a key (`sudo -u headway ssh-keygen`), add
it to the target host and connect once (`sudo -u headway ssh <host>`) to accept the host
key. The backup never deletes files at the destination.

The backup job also checks disk usage every night and alerts above `DISK_ALERT_PCT`
(default 85 %). To check it by hand:

```sh
df -h /srv/headway
du -sh /srv/headway/raw
du -sh /srv/headway/raw/$(date -u +%Y)/$(date -u +%m)/*   # per day
```

## 8. Check that it keeps working

- `journalctl -u headway-collect.service --since today | grep -c '"msg": "poll"'` should
  grow by about 180 per hour.
- `health` log lines (and ntfy alerts) report `error_streak`, `stale_feed` (the API keeps
  returning identical data) and `gap` (no successful poll for 2× the interval), each with a
  matching recovery message.
- A `heartbeat` line every hour gives the totals of successful and failed polls.
- The daily manifest `/srv/headway/raw/manifest/YYYY-MM-DD.parquet` (UTC days) lists every
  request, including failed ones, and is written at least every 5 minutes.

## What the collector writes

```
/srv/headway/raw/
  2026/10/24/104820_3f2a9c1b7d4e.json.gz   # response body, gzip; HHMMSS (UTC)_<sha256[:12]>
  manifest/2026-10-24.parquet              # one row per HTTP attempt
```

Bodies are stored exactly as received (after HTTP decompression); nothing is parsed or
changed. Files are written atomically, so a crash or power cut never leaves a half-written
file under its final name. The manifest columns are `fetched_at` (UTC), `attempt`,
`http_status`, `sha256`, `bytes`, `stored_bytes`, `latency_ms`, `path` (relative to
`raw/`) and `error`.

## Updating

```sh
cd /opt/headway
sudo -u headway git pull
sudo -u headway -H env UV_PYTHON_INSTALL_DIR=/opt/headway/.python \
    /home/headway/.local/bin/uv sync --frozen --no-dev
sudo systemctl restart headway-collect.service
```

A restart costs at most one poll. The collector finishes the current write and flushes
the manifest on `SIGTERM`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Service restarts in a loop | `journalctl -u headway-collect.service -n 50`; a missing `GOLEMIO_TOKEN` exits at once |
| `status: 401` or `403` | API key in `/etc/headway/collector.env` |
| `status: 429` | Rate limited; the collector waits for `Retry-After` automatically |
| `stale_feed` alerts | The upstream feed stopped updating; nothing to fix locally, the gap is recorded |
| `Permission denied` under `/srv/headway` | `sudo chown -R headway:headway /srv/headway` |
| Wrong times in file names | `timedatectl`; enable NTP with `sudo timedatectl set-ntp true` |
