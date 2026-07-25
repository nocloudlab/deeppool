# DeepPool — ZFS Monitor

**Version 1.0.0-rc.1**

A lightweight self-hosted dashboard for ZFS pool capacity, I/O performance,
ARC cache stats, scrub/resilver status, per-disk SMART health (including a
full per-drive attribute panel with CSV export), and historical trends
(capacity growth, throughput/IOPS, ARC hit ratio, disk temperature) over
ranges from 1 hour to 12 months. Light and dark themes. A `/api/health`
endpoint (200 fresh / 503 stalled, plus the running version) is provided
for uptime monitors.

Flask backend that shells out to `zpool`/`zfs`/`smartctl`, plain HTML/JS
frontend with Chart.js, and a small SQLite database (stdlib, no separate
service) for history. No auth — put it behind a reverse proxy or keep it
LAN-only.

**Runs directly on the host**, not in a container. This was a deliberate
choice after review: a container needs `/dev/zfs` explicitly bind-mounted
in (otherwise `zpool`/`zfs` fail entirely), plus the container's
userspace `zfsutils-linux` version has to match the exact OpenZFS version
the host's kernel module speaks — a stock LXC/Docker image pulling from
its own repos won't guarantee that. Running on the host sidesteps both
problems: it's the same kernel module and userspace already in use, no
passthrough of any kind needed. The app never writes to the pool (no
create/import/export/destroy calls anywhere), so the one risk category
that matters for a production pool doesn't apply.

Built and tested on Proxmox VE (which is Debian underneath), but there's
nothing Proxmox-specific in the code — it only shells out to
`zpool`/`zfs`/`smartctl` and reads the standard OpenZFS `/proc/spl/kstat`
counters. It should run unmodified on any Debian-based distro (Debian,
Ubuntu, etc.) with a ZFS pool already set up: if you're running this
tool at all, you already have `zfsutils-linux` installed, since you can't
have a pool without it.

## Install

### Requirements

- A Debian-based distro (Debian, Ubuntu, Proxmox VE, ...) with systemd
- A ZFS pool already imported and healthy (`zpool status` works) —
  which means `zfsutils-linux` is already present; the installer
  deliberately never touches it, since its version must stay matched to
  your kernel module
- Root (the installer sets up a systemd service and `smartctl` needs
  raw device access)
- Internet access **during install only** (apt packages, Flask, and the
  one-time Chart.js download — the running dashboard needs none)

> **Naming note:** the project is called DeepPool, but the service,
> install path and environment variables keep the original
> `zfs-monitor` name so upgrades from earlier versions are drop-in.

### From a git clone

```bash
git clone https://github.com/nocloudlab/deeppool.git
cd deeppool
./install.sh
```

### From a release tarball

```bash
tar xzf zfs-monitor.tar.gz
cd zfs-monitor
./install.sh
```

Either way, `install.sh` is self-contained and idempotent — it copies
the app files into `/opt/zfs-monitor`, installs `smartmontools` and
Python venv tooling via apt, creates a venv, installs Flask, downloads
and checksum-verifies Chart.js, and sets up + starts the `zfs-monitor`
systemd service. Developed and tested in production on Proxmox VE
(which is Debian underneath); nothing in it is Proxmox-specific, but
other Debian derivatives haven't had the same soak time — reports
welcome.

### Open it

```
http://<host-ip>:8087
```

Most Debian installs don't run a firewall by default, so the port is
usually reachable as soon as the service starts. If you do have one
(`ufw`, `nftables`, or the Proxmox VE firewall), allow TCP 8087 scoped
to your LAN.

### Upgrade

Pull (or extract) the new version and re-run `./install.sh` — it
restarts the service and preserves `history.db` and the Chart.js
vendor copy. Schema changes are applied automatically by the built-in
migrations. Hard-refresh the browser tab afterwards.

### Uninstall

```bash
systemctl disable --now zfs-monitor
rm /etc/systemd/system/zfs-monitor.service
systemctl daemon-reload
rm -rf /opt/zfs-monitor     # includes history.db — back it up first if you care
```

## Known trade-offs

- **The whole app runs as root**, not just the `zpool`/`smartctl` calls
  that actually need it (those genuinely do — SMART needs raw device
  access, `zpool` commands need pool-admin privileges). The Flask
  process itself doesn't strictly have to run as root, but does here for
  simplicity. If that's a problem for your environment, the two options
  are: (a) run the app as an unprivileged user and grant just the
  specific commands root via a narrow `sudoers` rule, or (b) delegate
  ZFS permissions to a non-root user with `zfs allow` and grant
  `smartctl` access via a udev rule or capability — neither is set up
  here.
- **No authentication**, and it binds `0.0.0.0` by default. This is a
  read-only monitoring dashboard with no write path to the pool, but it
  does expose pool/disk topology and SMART data to anyone who can reach
  the port. Keep it LAN-only or put a reverse proxy with basic auth in
  front of it if that's not acceptable for your network.
- **Test coverage, precisely.** Three suites run in CI:
  `tests/test_parsing.py` (CLI output parsing — topology detection, pool
  list, leaf devices, scan regex, `smartctl -j` for both ATA and NVMe,
  using fixtures that reproduce the real format including the literal
  tab prefix `zpool status` puts on every config line);
  `tests/test_routes.py` (every API route via the Flask test client,
  plus history bucketing and schema migrations against a temp SQLite
  DB); and `tests/app.test.js` (frontend logic — escaping, count
  parsing, threshold classes, device labeling, history-series
  alignment). **Not covered:** the background poll loops themselves, and
  anything needing a real pool or real disks — the tests drive the
  parsing and serving layers with canned input, they never invoke
  `zpool`/`smartctl`. Rendering is also untested (no DOM tests); the JS
  suite covers the pure helpers those render functions call. If
  something parses wrong on your setup, the usual failure mode is an
  empty or wrong-looking value rather than a crash — check the systemd
  journal (`journalctl -u zfs-monitor`) for warnings logged by failed
  commands.
- **Collection concurrency is bounded, not unlimited.** `smartctl` and
  per-pool `zpool status` calls within one cycle run on a thread pool
  capped by `ZFS_MONITOR_COLLECT_WORKERS` (default 8). On a very wide
  array a cycle still takes as long as its slowest batch — mostly
  relevant if you shorten `ZFS_MONITOR_SMART_INTERVAL`, since a spun-down
  disk can take many seconds to answer.
- **Single process by design.** The collector threads, in-memory cache,
  history cache and web server all live in one process. That's what
  makes the app zero-dependency beyond Flask, but it means you must not
  run it under a multi-worker WSGI server: each worker would start its
  own collector (multiplying `zpool`/`smartctl` load on your disks) with
  no shared cache. `waitress` or `gunicorn` with **one** worker is fine
  if you outgrow the dev server. It also means no horizontal scaling and
  no HA — if the process dies, systemd restarts it (`Restart=on-failure`)
  and you lose nothing but a few minutes of history samples.
- **CLI scraping, not libzfs bindings.** All ZFS data comes from parsing
  `zpool` output. Bindings (`py-libzfs`) would be more robust against
  format changes, but they version-couple the app to the exact OpenZFS
  build on the host — the very problem running on the host was chosen to
  avoid. The parsers are pinned by tests against real output formats
  (including the literal tab prefix in `zpool status`), and the failure
  mode for a format change is a wrong/empty field plus a journal
  warning, not a crash. `smartctl -j` output, by contrast, is a stable
  JSON contract.
- **Polling, not events.** Everything is sampled on intervals; there's
  no `zed` (ZFS Event Daemon) integration, so a pool fault appears
  within `ZFS_MONITOR_FAST_INTERVAL` seconds rather than instantly, and
  a fault that both occurs and clears between samples is invisible.
  Related: **no alerting** — this is a dashboard you look at, not a
  system that pages you. If the collector itself stalls, the header
  shows a red "collector stalled" warning instead of silently
  presenting stale data as fresh, and `/api/health` returns 503 (probe
  it from Uptime Kuma or similar if you want to be notified; that's the
  supported alerting story for 1.0).
- **Pool/disk removal isn't fully live.** If a pool or disk disappears
  while the page is open, its live chart group stays on screen with
  frozen data until a reload — cards, tables and history do update. A
  removed disk's temperature history also remains in the DB (and the
  temperature chart's legend) until the samples age out of retention.
- **The Chart.js pin is trust-on-first-use.** `chartjs.sha256` was
  pinned from a copy fetched over TLS from cdnjs and cross-checked from
  a second network path — not from an upstream-published digest (Chart.js
  doesn't publish release checksums). What it guarantees is that every
  future install gets byte-identical vendor code and that tampering
  after install is caught on the next upgrade.
- **History timestamps are host-local Unix time.** Samples are stamped
  with `time.time()` on the host; the browser renders them in its own
  locale/timezone. If the host clock steps backwards (NTP correction),
  a few samples may land out of order — harmless for charts (they're
  bucketed by averaging) but visible as a brief kink.
- **Browser support is modern-evergreen.** The frontend uses template
  literals, `Set`/`Map`, `async/await` and CSS custom properties, so
  anything from roughly 2018 onward works; IE11 does not, deliberately —
  supporting it would mean a build step, and shipping unbundled,
  readable source is part of the design.

## Development

Run the tests without touching a real pool — `ZFS_MONITOR_NO_POLL=1`
(set automatically by `tests/conftest.py`) makes importing `app.py`
side-effect free (no DB creation, no background threads, no `zpool`
calls):

```bash
python3 -m venv .venv && . .venv/bin/activate   # Debian/Ubuntu system
pip install -r requirements.txt pytest          # Python is PEP 668-managed;
pytest                        # parsers + API routes   pip refuses outside a venv
node --test tests/app.test.js  # frontend logic
```

`static/app.js` guards its bootstrap block behind a `typeof document`
check and exports its pure helpers under `module.exports`, so the Node
test runner can require it without a DOM, timers, or network. The
browser is unaffected — neither `module` nor a bundler is involved.

CI (GitHub Actions) runs all three suites plus `node --check` and
`bash -n install.sh` on every push and pull request.

**Adding a history DB column?** Append a function to `MIGRATIONS` in
`app.py` rather than editing the existing `CREATE TABLE` statements —
`CREATE TABLE IF NOT EXISTS` does nothing to an already-created
database, so editing it in place would leave every deployed
`history.db` on the old schema and fail on the next insert. Migrations
are applied in order and tracked via SQLite's `PRAGMA user_version`.


## Notes / things you might want to change

- **No auth** — add a reverse proxy (Caddy/nginx) with basic auth if
  this needs to be reachable beyond your LAN. Running on the host
  doesn't change this; the dashboard itself still has zero
  authentication.
- **Keep this host-only, low-footprint.** It's one lightweight Python
  process; don't let it become a pattern for installing arbitrary other
  services directly on your storage host (especially if that host is a
  hypervisor). If your needs grow past "simple ZFS dashboard," that's a
  signal to move the extra services to a VM or container instead.
- **Polling is decoupled from the frontend refresh.** A background
  thread collects data on its own schedule; every API call just reads
  the last cached result, so requests are always instant regardless of
  how long collection takes. Two separate intervals, both overridable
  via the systemd unit's `Environment=` lines:
  - `ZFS_MONITOR_FAST_INTERVAL` (default 5s) — pool capacity/health,
    scrub status, iostat, ARC. None of this touches physical disks
    directly, so a short interval is safe.
  - `ZFS_MONITOR_SMART_INTERVAL` (default 3600s / 1 hour) — SMART
    queries the disks directly and can wake one from spin-down, so it
    gets its own long interval. Raise or lower this to match how
    aggressive your drives' spin-down policy is; if you don't use APM
    at all, there's no harm in shortening it.
  - `static/app.js`'s `REFRESH_MS` (default 5s, frontend polling) can
    stay short regardless — it's just re-reading the cache, not
    triggering new collection.
  - `ZFS_MONITOR_HOST` / `ZFS_MONITOR_PORT` (default `0.0.0.0` / `8087`)
    if you need the dashboard on a different interface or port.
  - `ZFS_MONITOR_COLLECT_WORKERS` (default 8) — how many `zpool
    status` / `smartctl` subprocesses may run concurrently within a
    single collection pass. These calls are independent and mostly I/O
    wait, so overlapping them stops a cycle scaling linearly with
    disk/pool count; the cap keeps a wide array from forking one
    subprocess per drive at once. Lower it if you'd rather stagger the
    load across your disks.
- **Historical data lives in `/opt/zfs-monitor/history.db`** (SQLite,
  stdlib — no extra service to install or manage). A separate recorder
  thread samples the live cache on its own cadence and writes one row
  per pool/ARC-reading/disk, independent of both the fast and SMART
  poll loops:
  - `ZFS_MONITOR_RECORD_INTERVAL` (default 60s) — how often a sample is
    written. Doesn't trigger new `zpool`/`smartctl` calls itself, it
    just reads whatever the live cache already has.
  - `ZFS_MONITOR_HISTORY_RETENTION_DAYS` (default 365) — rows older
    than this get pruned on every recording cycle. **Budget roughly
    1.3 MB per recorded series per month** at the default 1-minute
    resolution: measured on a 2-pool / 6-disk host, 180 days of history
    came to ~119 MB, so a full year lands near 240 MB. That's fine on a
    Proxmox root pool, but it's not the "few MB" you might assume —
    shorten retention or lengthen `ZFS_MONITOR_RECORD_INTERVAL` if the
    space matters. Disk count drives this more than pool count, since
    one row per disk per interval is written for temperature.
  - **Retention is a rolling window, not a periodic reset.** The prune
    runs in the same transaction as each sample write, deleting anything
    older than the cutoff — so the database always holds the trailing N
    days and nothing is ever wiped and restarted. Practically, the file
    grows for the first N days and then plateaus: rows expire at the same
    rate they're written.
  - **Shrinking retention later needs a `VACUUM`.** SQLite doesn't return
    freed pages to the filesystem (`auto_vacuum` is off, deliberately —
    in steady state the freelist is reused in place, which avoids
    constant allocate/release churn). So if you *lower*
    `ZFS_MONITOR_HISTORY_RETENTION_DAYS`, the extra rows are deleted
    promptly but the file stays its old size until you reclaim it:

    ```bash
    systemctl stop zfs-monitor
    sqlite3 /opt/zfs-monitor/history.db "VACUUM;"
    systemctl start zfs-monitor
    ```

    Stop the service first — `VACUUM` rewrites the whole file and wants
    exclusive access. It needs free space roughly equal to the current
    database size while it runs, and it isn't needed for normal
    operation or for *raising* retention. (`sqlite3` isn't a dependency
    of the app; `apt install sqlite3` if you don't have the CLI.)
  - The `1h`/`24h`/`7d`/`30d`/`3m`/`6m`/`12m` range picker on the
    dashboard queries `/api/history?range=...` (`90d`/`180d`/`365d` for
    the last three), which averages samples into roughly 500 buckets
    server-side regardless of range — a 12-month view isn't pulling
    half a million raw rows per pool into the browser.
  - **Long ranges are cached server-side.** The aggregate for a 12-month
    view scans every sample in the window and takes a few seconds on a
    full year of history, so results are cached per range, with a TTL of
    the range's own bucket width (capped at 30 min). A result can't
    meaningfully change faster than one bucket, so this costs nothing in
    accuracy — the first 12m load after a restart is slow, the rest are
    instant. The dashboard's own auto-refresh also backs off on long
    ranges for the same reason.
  - To reset history (e.g. after moving disks around), stop the
    service and delete `/opt/zfs-monitor/history.db` — it's recreated
    empty on next start.
- **Light and dark themes.** The toggle sits top-right in the header and
  is labelled with the theme it switches *to*. Dark is the default look,
  but with no explicit choice saved the dashboard follows the OS setting
  (`prefers-color-scheme`) and tracks changes to it live; once you press
  the button, that choice is stored in `localStorage` and wins from then
  on. Everything themeable is a CSS variable in `static/style.css` — two
  blocks, `:root` and `:root[data-theme="light"]` — so a custom palette
  means editing colour values in one place, not hunting through rules.
  Two things worth knowing if you change them: the light palette's
  status colours are deliberately darker than the dark palette's (the
  dark greens and ambers fail WCAG AA as text on white — every pair in
  both themes currently clears 4.5:1), and Chart.js can't read CSS
  variables, so the chart axis/grid/label colours are mirrored into
  `--chart-*` variables that `app.js` reads at render time and re-reads
  on every theme switch. The logo mark is a deliberate exception: its
  `--brand-*` colours are defined once and never overridden, so the
  identity is identical in both themes. That works because the mark
  carries its own dark plate — the brand blue and green sit on `#12151a`
  whatever the page behind it is doing (5.7:1 and 9.2:1). The wordmark
  beside it does follow the theme, because it's text directly on the
  page background, where the brand green would manage only 1.8:1.
- **Per-drive S.M.A.R.T. detail panel** — every row in the SMART table
  has a `S.M.A.R.T.` button that opens the drive's complete attribute
  set, keyed the way the underlying standard keys it:
  - **NVMe** drives show the SMART/Health Information Log (log page
    02h) by byte offset — `0` Critical Warning, `5` Percentage Used,
    `175:160` Media and Data Integrity Errors, and so on, including
    each populated Temperature Sensor. The offsets are fixed by the
    NVMe spec, which is why they're the row identifier.
  - **ATA/SATA** drives show attribute IDs with their normalised
    value/worst/threshold triplets alongside the raw counter.

  Each row carries a Critical / Fair / Good dot so a problem is visible
  without reading every number, and **Export CSV** downloads the table
  as shown. The full attribute set is fetched on demand from
  `/api/smart/details` when the panel opens, so it doesn't ride along
  on the 5-second refresh.
- **SMART on NVMe** — attribute ID 5 (reallocated sectors) is
  ATA-specific, so NVMe drives show "—" in that column, which is
  expected. The SMART table's three right-most columns cover NVMe
  endurance instead, pulled from `nvme_smart_health_information_log`
  (and blank on ATA/SATA drives, the mirror-image case):
  - **NVMe Wear** — the drive's own `percentage_used` estimate,
    normalized against its rated write endurance. Can exceed 100 for a
    drive that's outlived its rated life but still works; colored
    amber ≥70%, red ≥90%.
  - **Media Errors** — unrecovered data-integrity errors. Any nonzero
    count is a real signal (unlike wear, which is a gradual, expected
    climb), so this one's binary: red if nonzero, green at 0.
  - **Total Written** — lifetime bytes written (`data_units_written *
    512,000`, per the NVMe spec's unit size), for tracking usage
    against your drive's rated TBW/DWPD figure.
- **Multiple pools** — everything is designed to loop over however many
  pools `zpool list` reports, no hardcoding.
- **Scale beyond a couple of viewers** — swap the Flask dev server for
  `waitress` or `gunicorn` in front of `app.py`, but keep it to a single
  worker process. The background poller and cache live in-process;
  multiple workers would each run an independent poller with no shared
  cache, multiplying `zpool`/`smartctl` calls for no benefit.
- **Disk naming edge case** — if your pool was created using plain
  `/dev/sdX` (rather than `/dev/disk/by-id/...`), `zpool status -P`
  reports whole-disk members as `/dev/sdX1` (a real partition, not the
  `-partN` suffix ZFS uses on by-id names). The code's suffix-stripping
  only matches the by-id pattern, so `smartctl` gets pointed at the
  partition node in that case — it generally still returns correct
  SMART data for the underlying physical disk, just worth knowing if a
  device shows up with a `1` on the end in the SMART table.
- **Chart.js is downloaded once during install**, not loaded from a CDN
  at runtime — `install.sh` fetches it into `static/vendor/` the first
  time it runs. Once installed, the dashboard works with zero internet
  access, which matters since a storage monitoring tool ideally still
  works during a network outage. If `install.sh` runs without internet
  access, it warns and skips this step; re-run it once you're back
  online, or place a copy at `/opt/zfs-monitor/static/vendor/chart.umd.min.js`
  yourself.
- **The Chart.js download is checksum-pinned.** `install.sh` verifies it
  against the SHA-256 in `chartjs.sha256` and refuses to install a
  mismatch; it also re-verifies an already-installed copy on every run,
  so tampering with the vendored file surfaces at the next upgrade
  rather than persisting silently. TLS only guarantees the bytes arrived
  intact — it says nothing about whether the CDN served what you expect,
  which is what the pin covers. If the pin file is empty or missing, the
  install still proceeds but prints a warning that the file is
  unverified. To re-pin when bumping the Chart.js version:

  ```bash
  sha256sum chart.umd.min.js | awk '{print $1}' > chartjs.sha256
  ```

## License

MIT — see [LICENSE](LICENSE).
