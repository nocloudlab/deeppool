# DeepPool 1.0.0-rc.1

First public release candidate. A self-hosted ZFS dashboard that runs
directly on your storage host — one Python process, one SQLite file, no
Prometheus, no Grafana, no agents.

Running in production on a Proxmox VE host with two pools (a mirrored
NVMe `rpool` and a 4-disk RAID10 `tank`) since before this tag. It's an
RC rather than 1.0 because it has only ever run on that one machine.

## What it does

- **Pools** — capacity, fragmentation, dedup ratio, health, and an
  auto-detected topology label (RAID1 / RAID10 / RAIDZ1-3 / dRAID /
  single disk / stripe), derived from the vdev tree rather than
  configured per pool. Expandable per-pool disk list.
- **Live I/O** — per-pool throughput and IOPS, sampled every 5 seconds.
- **ARC** — hit ratio, size against target/min/max, MFU/MRU split.
- **Scrub / resilver** — per-pool scan summary plus the vdev tree with
  read/write/checksum error counts.
- **SMART health** — per-disk health, temperature, power-on hours,
  reallocated sectors, and for NVMe: wear percentage, media errors and
  lifetime bytes written. Polled hourly on its own cadence so it doesn't
  keep spun-down disks awake.
- **Per-drive S.M.A.R.T. panel** — the drive's complete attribute set,
  keyed the way the underlying standard keys it: NVMe by SMART/Health
  Information Log byte offset (`0` Critical Warning, `5` Percentage
  Used, `175:160` Media Errors …), ATA by attribute ID with
  value/worst/threshold. Critical / Fair / Good status per row, and
  CSV export.
- **History** — 1h / 24h / 7d / 30d / 3m / 6m / 12m for capacity,
  throughput, IOPS, ARC hit ratio and per-disk temperature, backed by
  SQLite with a rolling retention window.
- **Light and dark themes**, following your OS setting until you choose
  otherwise.
- **`/api/health`** — 200 while the collector is fresh, 503 once its
  data goes stale, plus the running version. Point an uptime monitor
  at it.

## Design decisions worth knowing

- **Runs on the host, not in a container.** A container would need
  `/dev/zfs` passed through *and* its userspace `zfsutils-linux` version
  matched to the host's kernel module. Running on the host avoids both.
  The app is strictly read-only — no `zpool create/import/export/destroy`
  anywhere.
- **Collection is decoupled from serving.** Background threads poll on
  three separate cadences (5s live, 1h SMART, 1min history recorder);
  every API route reads a cache, so a request never blocks on a slow
  `zpool` or a disk waking from standby.
- **Aggregation happens server-side.** Any history range is bucketed to
  ~500 points, so a 12-month view isn't shipping half a million rows to
  the browser. Long ranges are cached, since a result can't change
  faster than its own bucket width.
- **Vendored Chart.js, checksum-pinned.** Downloaded once at install and
  verified against `chartjs.sha256`; the running dashboard has no CDN
  dependency, which matters for a storage monitor you might open during
  a network outage.

## Known limitations

Documented in full in the README's *Known trade-offs* section. The short
version: runs as root, has no authentication, is a single process by
design (never run it multi-worker), polls rather than subscribing to ZFS
events, and has no built-in alerting — probe `/api/health` instead.

## Requirements

A Debian-based distro with systemd and an existing ZFS pool. Developed
and tested on Proxmox VE; nothing in it is Proxmox-specific, but other
derivatives haven't had the same soak time.

## Install

Run on the host that has the ZFS pool, as root.

From a clone:

```bash
git clone https://github.com/nocloudlab/deeppool.git
cd deeppool
./install.sh
```

Or from the source tarball attached below — it extracts to a directory
named after the tag:

```bash
tar xzf deeppool-1.0.0-rc.1.tar.gz
cd deeppool-1.0.0-rc.1
./install.sh
```

If you take the **.zip** instead, that format doesn't carry Unix
permissions, so run `bash install.sh` (or `chmod +x install.sh` first).

Then open `http://<host-ip>:8087`. Re-running `install.sh` upgrades in
place and preserves your history database. Full instructions, upgrade
and uninstall steps are in the README.

## Feedback wanted

Most useful right now: whether topology detection labels your pool
correctly (especially RAIDZ, dRAID, or mixed/special vdev layouts),
whether the SMART panel renders sensibly for drives other than Crucial
NVMe and Seagate IronWolf, and whether it installs cleanly on Debian or
Ubuntu as opposed to Proxmox VE.

## Verification

121 tests run in CI on every push — 72 Python, 49 JavaScript.

The Python suites cover CLI output parsing (topology detection, pool
list, leaf devices, scan status, `smartctl -j` for both ATA and NVMe),
every API route via the Flask test client, and history bucketing plus
schema migrations against a temporary database. The JavaScript suite
covers frontend logic: HTML escaping, ZFS count parsing, threshold
classes, device labelling, CSV escaping and history-series alignment.

Not covered: the background poll loops, and anything requiring a real
pool or real disks — the tests drive the parsing and serving layers
with canned input and never invoke `zpool` or `smartctl`.
