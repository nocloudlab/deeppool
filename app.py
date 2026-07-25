#!/usr/bin/env python3
"""
ZFS Monitor - lightweight dashboard for ZFS pool capacity, performance,
ARC stats, scrub status and SMART health. Runs directly on the host
(not in a container) — avoids needing /dev/zfs passthrough and avoids
any risk of the ZFS userspace tools mismatching the host kernel
module's version. Built and tested on Proxmox VE, but nothing here is
Proxmox-specific — it runs on any Debian-based distro with a ZFS pool
already set up. Read-only: never calls zpool create/import/export/
destroy, so it can't touch pool state.

Collection is decoupled from serving: a background thread polls at two
speeds and caches results, so API requests are always instant reads
from memory rather than triggering fresh subprocess calls. This matters
for two reasons:
  1. zpool iostat blocks ~1s per pool and smartctl can block much longer
     on a disk waking from spin-down; doing that synchronously inside a
     request handler causes requests to queue up under Flask's default
     single-threaded dev server.
  2. Polling SMART on the same fast cycle as capacity/iostat would keep
     spun-down disks awake permanently. SMART gets its own slow interval.
"""
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template, request

__version__ = "1.0.0-rc.1"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# The dashboard polls /api/all every 5s and /api/history every minute,
# per open tab — at INFO, werkzeug writes one journal line per request,
# which is thousands of identical entries a day drowning out the log
# lines that matter (collection failures). Errors still come through.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Capacity/health/iostat/ARC never touch physical disks directly (they
# read pool metadata and kernel counters), so a short interval is safe.
FAST_INTERVAL = int(os.environ.get("ZFS_MONITOR_FAST_INTERVAL", "5"))

# SMART queries the physical disks directly and can wake a spun-down
# drive. Default to 1 hour; override via systemd unit Environment= line.
SMART_INTERVAL = int(os.environ.get("ZFS_MONITOR_SMART_INTERVAL", "3600"))

# Historical recording: a separate, slower cadence purely for the
# history charts — recording every 5s reading forever would bloat
# storage 5x for no real benefit. At 1-minute samples, budget roughly
# 1.3 MB per recorded series per month (a 2-pool/6-disk host measured
# ~240 MB/year); the file plateaus once retention kicks in.
RECORD_INTERVAL = int(os.environ.get("ZFS_MONITOR_RECORD_INTERVAL", "60"))
HISTORY_RETENTION_DAYS = int(os.environ.get("ZFS_MONITOR_HISTORY_RETENTION_DAYS", "365"))
DB_PATH = os.environ.get("ZFS_MONITOR_DB_PATH", "history.db")

HOST = os.environ.get("ZFS_MONITOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("ZFS_MONITOR_PORT", "8087"))

# Upper bound on concurrent zpool/smartctl subprocesses within a single
# collection pass. These calls are independent and almost entirely I/O
# wait (a spun-down disk can take many seconds to answer smartctl), so
# overlapping them keeps a cycle from scaling linearly with disk/pool
# count — but an unbounded pool would fork one subprocess per drive at
# once on a wide array, so it's capped.
COLLECT_WORKERS = int(os.environ.get("ZFS_MONITOR_COLLECT_WORKERS", "8"))

_cache_lock = threading.Lock()
_cache = {
    "pools": [],
    "statuses": {},
    "iostat": [],
    "arc": {},
    "smart": [],
    "pool_disks": {},
    "last_fast_update": None,
    "last_smart_update": None,
}


def run(cmd):
    """Run a shell command, return stdout text or '' on failure. Failures
    are logged (to the systemd journal in production) rather than
    swallowed silently — an empty '' return still means "nothing to show"
    to callers, but a missing binary or permission error is no longer
    invisible."""
    try:
        result = subprocess.run(
            cmd, shell=False, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 and not result.stdout.strip():
            # Nonzero exit + no stdout is a real signal something's wrong.
            # (smartctl in particular uses a bitmask exit code where
            # nonzero often just reflects historical attribute dips on an
            # otherwise-fine drive, not a failure — so exit code alone
            # isn't a reliable trigger; empty output is.)
            logging.warning(f"command {cmd[0]} exited {result.returncode} with no output: {result.stderr.strip()}")
        return result.stdout
    except FileNotFoundError:
        logging.error(f"command not found: {cmd[0]} (is it installed / on PATH?)")
        return ""
    except subprocess.TimeoutExpired:
        logging.warning(f"command timed out: {' '.join(cmd)}")
        return ""
    except Exception as e:
        logging.error(f"command failed: {' '.join(cmd)}: {e}")
        return ""


# ---------------------------------------------------------------------
# Pools: capacity + health
# ---------------------------------------------------------------------
def _to_int(value, default=0):
    """Parse a numeric field from `zpool list` output. ZFS prints "-" for
    fields that don't apply or aren't computable yet — a pool mid-import,
    or certain feature-flag/vdev-class combinations, will legitimately
    show "-" for capacity or fragmentation. int("-") raises, and because
    get_pools() feeds the whole fast-poll cycle, one such field used to
    abort the entire pools/status/iostat/ARC refresh (caught by the outer
    handler, so it failed safe with a stale cache — but it stalled every
    other metric until the condition cleared). Degrade that one field to
    a default instead."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_pools():
    """zpool list -Hp -> name,size,alloc,free,ckpoint,expandsz,frag,cap,dedup,health,altroot"""
    out = run([
        "zpool", "list", "-Hp",
        "-o", "name,size,alloc,free,fragmentation,capacity,dedupratio,health"
    ])
    pools = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        name, size, alloc, free, frag, cap, dedup, health = parts[:8]
        # fragmentation_pct stays a string (the frontend appends "%" and
        # its threshold helper tolerates a non-numeric value), but "-"
        # is normalised away so it doesn't render as "-%".
        frag = frag.rstrip("%")
        pools.append({
            "name": name,
            "size_bytes": _to_int(size),
            "alloc_bytes": _to_int(alloc),
            "free_bytes": _to_int(free),
            "fragmentation_pct": frag if frag not in ("-", "") else "0",
            "capacity_pct": _to_int(cap),
            "dedup_ratio": dedup,
            "health": health,
        })
    return pools


# ---------------------------------------------------------------------
# Pool status: vdev tree, per-disk health, scrub/resilver info
# ---------------------------------------------------------------------
SCAN_RE = re.compile(
    r"scan:\s*(.+?)(?:\n\s*config:|\n\n|\Z)", re.DOTALL
)


VDEV_GROUP_RE = re.compile(r"^(mirror|raidz1|raidz2|raidz3|draid1|draid2|draid3)-\d+$")
SPECIAL_VDEV_CLASSES = ("logs", "cache", "spares", "special", "dedup")


def classify_topology(pool_name, raw_status):
    """Derive the pool's RAID topology from the indentation-based vdev
    tree in `zpool status` output — no hardcoded per-pool config. Returns
    a short human label like "ZFS RAID1" or "ZFS RAID10".

    Tracks indentation relative to whatever entry is "current" rather
    than assuming a fixed +2/+4 offset, since special vdev classes
    (logs/cache/spares) are printed flush-left at the same level as the
    pool name itself, not nested under it like data vdevs are."""
    lines = raw_status.splitlines()
    in_config = False
    pool_indent = None
    top_level = []  # [group_kind, disk_count] per top-level data vdev
    current_group_indent = None
    skip_until_indent = None  # set while inside a logs/cache/spares/etc. section

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("config:"):
            in_config = True
            continue
        if stripped.startswith("errors:"):
            break
        if not in_config or not stripped or stripped.startswith("NAME"):
            continue

        # zpool status prefixes every config line with a leading tab,
        # then uses spaces for the actual tree nesting within that — the
        # tab is a constant offset present on every line, so stripping
        # both uniformly preserves the relative depth comparisons below.
        indent = len(line) - len(line.lstrip(" \t"))
        name = stripped.split()[0]

        if name == pool_name and pool_indent is None:
            pool_indent = indent
            continue
        if pool_indent is None:
            continue

        if skip_until_indent is not None:
            if indent > skip_until_indent:
                continue  # still inside the skipped special-class section
            skip_until_indent = None  # back out to this level, fall through

        if name in SPECIAL_VDEV_CLASSES:
            skip_until_indent = indent
            current_group_indent = None
            continue

        if current_group_indent is not None and indent > current_group_indent:
            top_level[-1][1] += 1  # child disk of the current top-level group
            continue

        m = VDEV_GROUP_RE.match(name)
        if m:
            top_level.append([m.group(1), 0])
        else:
            top_level.append(["disk", 1])  # bare disk = unredundant stripe member
        current_group_indent = indent

    return _describe_topology(top_level)


def _describe_topology(top_level):
    if not top_level:
        return "Unknown"

    kinds = set(t[0] for t in top_level)
    n_groups = len(top_level)
    width = top_level[0][1] if top_level else 0

    if kinds == {"disk"}:
        return "ZFS Single Disk" if n_groups == 1 else "ZFS RAID0 (striped, no redundancy)"

    if kinds == {"mirror"}:
        if n_groups == 1:
            return "ZFS RAID1" if width == 2 else f"ZFS RAID1 ({width}-way mirror)"
        return "ZFS RAID10" if width == 2 else f"ZFS RAID10-like ({n_groups}x {width}-way mirrors)"

    raidz_labels = {"raidz1": "RAIDZ1", "raidz2": "RAIDZ2", "raidz3": "RAIDZ3"}
    if len(kinds) == 1 and next(iter(kinds)) in raidz_labels:
        label = raidz_labels[next(iter(kinds))]
        return f"ZFS {label}" if n_groups == 1 else f"ZFS {label} (striped, {n_groups}x)"

    if len(kinds) == 1 and next(iter(kinds)).startswith("draid"):
        kind = next(iter(kinds))
        return f"ZFS dRAID{kind[-1]}"

    return "ZFS Mixed Topology"


def get_pool_status(pool_name):
    out = run(["zpool", "status", "-P", pool_name])
    scrub_match = SCAN_RE.search(out)
    scan_info = scrub_match.group(1).strip() if scrub_match else "none requested"

    # Parse the config block: lines after "config:" until "errors:"
    vdevs = []
    in_config = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("config:"):
            in_config = True
            continue
        if stripped.startswith("errors:"):
            in_config = False
            continue
        if in_config and stripped and not stripped.startswith("NAME"):
            cols = stripped.split()
            if len(cols) >= 5:
                vdevs.append({
                    "name": cols[0],
                    "state": cols[1],
                    "read_errors": cols[2],
                    "write_errors": cols[3],
                    "cksum_errors": cols[4],
                })

    return {
        "pool": pool_name,
        "scan": scan_info,
        "vdevs": vdevs,
        "topology": classify_topology(pool_name, out),
        "raw": out,
    }


def _collect_pool_statuses(pool_names):
    """get_pool_status() for every pool, overlapped. Returns the same
    {pool_name: status} mapping the serial dict-comprehension produced."""
    if not pool_names:
        return {}
    with ThreadPoolExecutor(max_workers=min(COLLECT_WORKERS, len(pool_names))) as pool:
        return dict(zip(pool_names, pool.map(get_pool_status, pool_names)))


def get_leaf_devices(pool_name):
    """Return real block device paths (e.g. /dev/sdb) participating in a pool,
    skipping mirror/raidz/pool-name label rows."""
    out = run(["zpool", "status", "-P", pool_name])
    devices = []
    in_config = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("config:"):
            in_config = True
            continue
        if stripped.startswith("errors:"):
            in_config = False
            continue
        if not in_config or not stripped:
            continue
        cols = stripped.split()
        if not cols or cols[0] in ("NAME",):
            continue
        first = cols[0]
        if first == pool_name:
            continue
        if first.startswith("mirror") or first.startswith("raidz") or first.startswith("spare") or first == "logs" or first == "cache":
            continue
        if first.startswith("/dev/") or first.startswith("/"):
            devices.append(first)
    return devices


# ---------------------------------------------------------------------
# I/O performance: one interval snapshot (non-cumulative)
# ---------------------------------------------------------------------
def get_iostat(pool_names=None):
    """One combined zpool iostat call samples every pool in a single
    1-second interval, instead of blocking ~1s per pool serially. With -H
    (no headers/separators) and N pools, output is just 2*N lines: the
    first N are the since-import cumulative sample, the last N are the
    live 1-second sample we actually want — in the same pool order both
    times, so we can slice by the pool count we already know.

    Callers that already have the pool list pass it in, so one poll
    cycle doesn't run `zpool list` multiple times."""
    if pool_names is None:
        pool_names = [p["name"] for p in get_pools()]
    n = len(pool_names)
    if n == 0:
        return []
    out = run(["zpool", "iostat", "-Hp", "1", "2"])
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < n * 2:
        return []
    live_lines = lines[n:n * 2]
    results = []
    for line in live_lines:
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        pool, alloc, free, ops_read, ops_write, bw_read, bw_write = cols[:7]
        results.append({
            "pool": pool,
            "read_iops": int(ops_read),
            "write_iops": int(ops_write),
            "read_bw_bytes": int(bw_read),
            "write_bw_bytes": int(bw_write),
        })
    return results


# ---------------------------------------------------------------------
# ARC stats
# ---------------------------------------------------------------------
def get_arc_stats():
    stats = {}
    try:
        with open("/proc/spl/kstat/zfs/arcstats") as f:
            lines = f.readlines()[2:]  # skip 2 header lines
        for line in lines:
            parts = line.split()
            if len(parts) == 3:
                name, _type, value = parts
                stats[name] = int(value)
    except FileNotFoundError:
        return {"available": False}

    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    total = hits + misses
    hit_ratio = round((hits / total) * 100, 2) if total else 0.0

    return {
        "available": True,
        "size_bytes": stats.get("size", 0),
        "target_size_bytes": stats.get("c", 0),
        "min_size_bytes": stats.get("c_min", 0),
        "max_size_bytes": stats.get("c_max", 0),
        "hits": hits,
        "misses": misses,
        "hit_ratio_pct": hit_ratio,
        "mfu_size_bytes": stats.get("mfu_size", 0),
        "mru_size_bytes": stats.get("mru_size", 0),
    }


# ---------------------------------------------------------------------
# SMART health per physical disk
# ---------------------------------------------------------------------
SMART_ENTRY_DEFAULTS = {
    "health": "unknown", "temperature_c": None, "power_on_hours": None,
    "reallocated_sectors": None, "model": None, "capacity_bytes": None,
    # NVMe-only endurance fields, from the "nvme_smart_health_information_log"
    # object smartctl -j emits for NVMe drives — absent (stays None) for
    # ATA/SATA disks, same as reallocated_sectors is for NVMe.
    "nvme_wear_pct": None, "nvme_media_errors": None,
    "nvme_data_written_bytes": None,
    "serial": None, "firmware": None, "protocol": None,
}


# ---------------------------------------------------------------------
# Full SMART attribute detail (the per-drive "S.M.A.R.T." panel)
# ---------------------------------------------------------------------
# The dashboard's SMART table shows the handful of fields worth watching
# at a glance. This section builds the complete attribute list behind
# that, in the shape the NVMe spec and ATA both natively use:
#
#   NVMe — the SMART/Health Information Log (log page 02h) is a fixed
#     512-byte structure, so every field has a defined byte offset. Those
#     offsets are the canonical way to refer to these fields, so they're
#     surfaced as the row identifier.
#   ATA — attributes are identified by a vendor-assigned ID (1-254) and
#     carry normalised value/worst/threshold triplets alongside the raw
#     counter, so the row identifier is the attribute ID.
#
# Each row is tagged good/fair/critical so the UI can dot-colour it
# without duplicating threshold logic in JavaScript.

# (byte range, smartctl JSON key, human label) for the NVMe health log.
NVME_LOG_FIELDS = [
    ("0", "critical_warning", "Critical Warning"),
    ("2:1", "temperature", "Composite Temperature (°C)"),
    ("3", "available_spare", "Available Spare (%)"),
    ("4", "available_spare_threshold", "Available Spare Threshold (%)"),
    ("5", "percentage_used", "Percentage Used (%)"),
    ("47:32", "data_units_read", "Data Units Read"),
    ("63:48", "data_units_written", "Data Units Written"),
    ("79:64", "host_reads", "Host Read Commands"),
    ("95:80", "host_writes", "Host Write Commands"),
    ("111:96", "controller_busy_time", "Controller Busy Time (min)"),
    ("127:112", "power_cycles", "Power Cycles"),
    ("143:128", "power_on_hours", "Power On Hours"),
    ("159:144", "unsafe_shutdowns", "Unsafe Shutdowns"),
    ("175:160", "media_errors", "Media and Data Integrity Errors"),
    ("191:176", "num_err_log_entries", "Number of Error Information Log Entries"),
    ("195:192", "warning_temp_time", "Warning Composite Temperature Time (min)"),
    ("199:196", "critical_comp_time", "Critical Composite Temperature Time (min)"),
]

# Temperature Sensor 1-8 occupy consecutive 2-byte slots from 201:200.
NVME_TEMP_SENSOR_BASE = 200

# ATA attribute IDs where a nonzero raw counter is a genuine warning
# sign rather than routine wear or an informational counter.
ATA_CRITICAL_RAW_ATTRS = {
    5: "Reallocated Sectors Count",
    196: "Reallocation Event Count",
    197: "Current Pending Sector Count",
    198: "Uncorrectable Sector Count",
}


def _status_for_nvme(key, value, log):
    """good / fair / critical for one NVMe health-log field."""
    if value is None:
        return "good"
    if key == "critical_warning":
        # Bitmask: any bit set means the controller is reporting a
        # problem (spare below threshold, read-only mode, degraded, ...).
        return "critical" if value else "good"
    if key == "percentage_used":
        if value >= 90:
            return "critical"
        return "fair" if value >= 70 else "good"
    if key == "available_spare":
        threshold = log.get("available_spare_threshold")
        if threshold is not None and value <= threshold:
            return "critical"
        return "fair" if value < 20 else "good"
    if key == "media_errors":
        # Unrecovered data-integrity errors — never routine.
        return "critical" if value else "good"
    if key == "temperature":
        if value >= 70:
            return "critical"
        return "fair" if value >= 60 else "good"
    if key in ("warning_temp_time", "critical_comp_time"):
        # Minutes spent over the drive's own temperature thresholds.
        if key == "critical_comp_time" and value:
            return "critical"
        return "fair" if value else "good"
    if key == "unsafe_shutdowns":
        # Common and mostly harmless on a home server, but worth noting.
        return "fair" if value else "good"
    return "good"


def _nvme_detail_rows(log):
    rows = []
    for byte_range, key, label in NVME_LOG_FIELDS:
        if key not in log:
            continue
        value = log.get(key)
        rows.append({
            "id": byte_range,
            "label": label,
            "value": value,
            "status": _status_for_nvme(key, value, log),
        })

    # temperature_sensors is a list; the spec gives each sensor its own
    # 2-byte slot, and smartctl omits sensors the drive doesn't populate.
    for i, temp in enumerate(log.get("temperature_sensors") or []):
        offset = NVME_TEMP_SENSOR_BASE + i * 2
        rows.append({
            "id": f"{offset + 1}:{offset}",
            "label": f"Temperature Sensor {i + 1} (°C)",
            "value": temp,
            "status": _status_for_nvme("temperature", temp, log),
        })
    return rows


def _status_for_ata(attr):
    """good / fair / critical for one ATA SMART attribute."""
    # smartctl surfaces the drive's own pass/fail verdict when an
    # attribute has ever dropped below its threshold.
    when_failed = attr.get("when_failed")
    if when_failed == "now":
        return "critical"
    if when_failed == "past":
        return "fair"

    attr_id = attr.get("id")
    raw_value = (attr.get("raw") or {}).get("value")
    if attr_id in ATA_CRITICAL_RAW_ATTRS and raw_value:
        # Pending/uncorrectable/reallocated sectors: a handful is a
        # warning, more than a handful means the disk is going.
        return "critical" if raw_value > 10 else "fair"

    # Fall back to how close the normalised value sits to its threshold.
    value = attr.get("value")
    thresh = attr.get("thresh")
    if isinstance(value, int) and isinstance(thresh, int) and thresh > 0:
        if value <= thresh:
            return "critical"
        if value <= thresh + 10:
            return "fair"
    return "good"


def _ata_detail_rows(data):
    rows = []
    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        raw = attr.get("raw") or {}
        # Prefer smartctl's formatted raw string ("1234 (12 45 0)") since
        # some attributes pack several counters into one raw field.
        raw_display = raw.get("string")
        if raw_display is None:
            raw_display = raw.get("value")
        rows.append({
            "id": attr.get("id"),
            "label": (attr.get("name") or "").replace("_", " ") or "Unknown",
            "value": raw_display,
            "normalized": attr.get("value"),
            "worst": attr.get("worst"),
            "threshold": attr.get("thresh"),
            "status": _status_for_ata(attr),
        })
    return rows


def _build_smart_details(data):
    """Return {"type": "nvme"|"ata"|"none", "rows": [...]} for one drive."""
    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log:
        return {"type": "nvme", "rows": _nvme_detail_rows(nvme_log)}
    if data.get("ata_smart_attributes", {}).get("table"):
        return {"type": "ata", "rows": _ata_detail_rows(data)}
    return {"type": "none", "rows": []}


def _parse_smart_json(device, raw_out):
    """Parse one `smartctl -i -H -A -j <device>` JSON payload into the flat
    dict the dashboard renders. Pulled out of get_smart_summary() as a pure
    function (string in, dict out) so it's testable without shelling out."""
    entry = {"device": device, **SMART_ENTRY_DEFAULTS, "details": {"type": "none", "rows": []}}
    try:
        data = json.loads(raw_out)
        passed = data.get("smart_status", {}).get("passed")
        entry["health"] = "PASSED" if passed else ("FAILED" if passed is False else "unknown")
        entry["temperature_c"] = data.get("temperature", {}).get("current")
        entry["power_on_hours"] = data.get("power_on_time", {}).get("hours")
        entry["model"] = data.get("model_name")
        entry["capacity_bytes"] = data.get("user_capacity", {}).get("bytes")
        entry["serial"] = data.get("serial_number")
        entry["firmware"] = data.get("firmware_version")
        # smartctl reports the transport as e.g. "NVMe" / "ATA" / "SAT".
        entry["protocol"] = data.get("device", {}).get("protocol")
        entry["details"] = _build_smart_details(data)
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("id") == 5:  # Reallocated_Sector_Ct
                entry["reallocated_sectors"] = attr.get("raw", {}).get("value")

        nvme_log = data.get("nvme_smart_health_information_log")
        if nvme_log:
            # percentage_used is the drive's own estimate of endurance
            # consumed (vendor-normalized against its rated write
            # endurance) — the NVMe equivalent of an SSD wear-leveling
            # count. Spec allows it to exceed 100 for a drive that has
            # outlived its rated endurance but is still functioning.
            entry["nvme_wear_pct"] = nvme_log.get("percentage_used")
            # media_errors counts unrecovered data-integrity errors —
            # unlike wear_pct, any nonzero value here is a real signal,
            # not just gradual endurance consumption.
            entry["nvme_media_errors"] = nvme_log.get("media_errors")
            data_units_written = nvme_log.get("data_units_written")
            if data_units_written is not None:
                # NVMe spec: 1 "data unit" = 512,000 bytes (not 512 KiB).
                entry["nvme_data_written_bytes"] = data_units_written * 512_000
    except (json.JSONDecodeError, AttributeError):
        pass
    return entry


def get_smart_summary(pool_names=None):
    if pool_names is None:
        pool_names = [p["name"] for p in get_pools()]
    all_devices = set()
    for pool in pool_names:
        for dev in get_leaf_devices(pool):
            # strip partition suffix like -part1 for smartctl on whole disk
            base = re.sub(r"-part\d+$", "", dev)
            all_devices.add(base)

    # smartctl is run per-device and each call can block for a long time
    # — a spinning disk waking from standby can take many seconds, and
    # run()'s timeout is 15s. Serially that put a floor of
    # (devices x wake time) on a single SMART cycle, which becomes minutes
    # on a wide pool of spun-down drives. These calls are independent and
    # almost entirely I/O wait, so a bounded pool overlaps them without
    # hammering the disks: COLLECT_WORKERS caps concurrency so a very
    # wide pool doesn't spawn a subprocess per drive all at once.
    devices = sorted(all_devices)
    if not devices:
        return []
    with ThreadPoolExecutor(max_workers=min(COLLECT_WORKERS, len(devices))) as pool:
        outputs = pool.map(
            lambda dev: (dev, run(["smartctl", "-i", "-H", "-A", "-j", dev])),
            devices,
        )
        return [_parse_smart_json(dev, out) for dev, out in outputs]


def get_pool_disks(smart_summary, pool_names=None):
    """Map each pool to the physical disks that make it up, for the
    dashboard's per-pool "more info" panel. Reuses the SMART data already
    fetched this cycle rather than running smartctl again."""
    smart_by_device = {d["device"]: d for d in smart_summary}
    if pool_names is None:
        pool_names = [p["name"] for p in get_pools()]
    result = {}
    for pool in pool_names:
        disks = []
        for dev in get_leaf_devices(pool):
            base = re.sub(r"-part\d+$", "", dev)
            info = smart_by_device.get(base, {})
            disks.append({
                "device": base,
                "model": info.get("model"),
                "capacity_bytes": info.get("capacity_bytes"),
                "health": info.get("health", "unknown"),
                "temperature_c": info.get("temperature_c"),
            })
        result[pool] = disks
    return result


# ---------------------------------------------------------------------
# Historical recording (SQLite) — separate cadence from the live cache.
# One connection per operation; at a once-a-minute write frequency plus
# occasional reads when someone opens the history charts, connection
# pooling isn't worth the complexity.
# ---------------------------------------------------------------------
def _migration_001_initial(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_samples (
            ts INTEGER NOT NULL,
            pool TEXT NOT NULL,
            capacity_pct REAL,
            alloc_bytes INTEGER,
            read_bw_bytes REAL,
            write_bw_bytes REAL,
            read_iops REAL,
            write_iops REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_samples_ts ON pool_samples(ts)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS arc_samples (
            ts INTEGER NOT NULL,
            hit_ratio_pct REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arc_samples_ts ON arc_samples(ts)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_samples (
            ts INTEGER NOT NULL,
            device TEXT NOT NULL,
            temperature_c REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_smart_samples_ts ON smart_samples(ts)")


# Ordered schema migrations. Each entry is applied exactly once, in order,
# and the DB's PRAGMA user_version is advanced to its index as it goes.
#
# CREATE TABLE IF NOT EXISTS alone is not a schema management strategy: it
# creates missing tables on a fresh install but silently does nothing to
# an existing database, so adding a column to the definition above would
# leave every already-deployed history.db on the old shape — surfacing
# later as "table has no column named ..." on the next INSERT, after the
# upgrade appeared to succeed. Appending a function here instead makes
# the change apply to existing installs too.
#
# To add a migration: append a function that performs the change
# idempotently (ALTER TABLE ADD COLUMN, backfill, etc.) and leave the
# existing entries untouched.
MIGRATIONS = [
    _migration_001_initial,
]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # WAL lets readers (the /api/history route) and the writer (the
        # recorder thread) work concurrently instead of taking turns on a
        # whole-file lock — avoids "database is locked" if a query lands
        # at the same moment as a write.
        conn.execute("PRAGMA journal_mode=WAL")

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            # DB written by a newer version of the app than this one.
            logging.warning(
                f"history DB schema version {version} is newer than this build "
                f"supports ({len(MIGRATIONS)}) — leaving it alone."
            )
            return

        for i, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            logging.info(f"applying history DB migration {i}: {migration.__name__}")
            migration(conn)
            # user_version doesn't accept a bound parameter, but i is a
            # loop index over a module-level list, never user input.
            conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()


def _record_sample():
    """Read the current live cache (already populated by the fast/smart
    loops) and append one row per pool/ARC/device. Reuses cached data
    rather than re-running zpool/smartctl itself."""
    with _cache_lock:
        pools = list(_cache["pools"])
        iostat = {e["pool"]: e for e in _cache["iostat"]}
        arc = dict(_cache["arc"])
        smart = list(_cache["smart"])

    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        for p in pools:
            io = iostat.get(p["name"], {})
            conn.execute(
                "INSERT INTO pool_samples (ts, pool, capacity_pct, alloc_bytes, "
                "read_bw_bytes, write_bw_bytes, read_iops, write_iops) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, p["name"], p["capacity_pct"], p["alloc_bytes"],
                 io.get("read_bw_bytes"), io.get("write_bw_bytes"),
                 io.get("read_iops"), io.get("write_iops")),
            )

        if arc.get("available"):
            conn.execute(
                "INSERT INTO arc_samples (ts, hit_ratio_pct) VALUES (?, ?)",
                (now, arc.get("hit_ratio_pct")),
            )

        for d in smart:
            if d.get("temperature_c") is not None:
                conn.execute(
                    "INSERT INTO smart_samples (ts, device, temperature_c) VALUES (?, ?, ?)",
                    (now, d["device"], d["temperature_c"]),
                )

        cutoff = now - HISTORY_RETENTION_DAYS * 86400
        conn.execute("DELETE FROM pool_samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM arc_samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM smart_samples WHERE ts < ?", (cutoff,))
        conn.commit()


def _record_loop():
    while True:
        try:
            _record_sample()
        except Exception as e:
            app.logger.error(f"history recording failed: {e}")
        time.sleep(RECORD_INTERVAL)


# Range strings the frontend can request, in seconds. "all" is handled
# separately (no time filter).
HISTORY_RANGES = {
    "1h": 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    # Longer views for capacity planning — the point of keeping a year of
    # history. Calendar months vary in length, so these are the plain
    # day-count approximations the UI labels 3m/6m/12m.
    "90d": 90 * 86400,
    "180d": 180 * 86400,
    "365d": 365 * 86400,
}
TARGET_POINTS = 500  # aim for roughly this many points regardless of range


def _bucket_seconds(range_seconds):
    if range_seconds is None:
        range_seconds = HISTORY_RETENTION_DAYS * 86400
    return max(RECORD_INTERVAL, range_seconds // TARGET_POINTS)


# Cached /api/history responses, keyed by range.
#
# Unlike the live metrics, history is computed on demand, and the long
# ranges are genuinely expensive: the query aggregates every sample in
# the window, so a 12-month view over a full year of 1-minute samples
# scans hundreds of thousands of rows per table and takes seconds. That
# would otherwise happen on the request thread, on every poll, for every
# open browser tab.
#
# Caching is safe because a result can't meaningfully change faster than
# its own bucket width — a 12m chart buckets into ~17-hour points, so
# recomputing it every minute would produce a pixel-identical chart.
# The TTL is the bucket width, floored at the recorder's interval (no
# point caching past the next write for fine ranges) and capped so a very
# long range still refreshes periodically.
_history_cache = {}
_history_cache_lock = threading.Lock()
HISTORY_CACHE_MAX_TTL = 1800


def _history_cache_ttl(bucket):
    return max(RECORD_INTERVAL, min(bucket, HISTORY_CACHE_MAX_TTL))


def get_history_cached(range_key):
    now = time.time()
    with _history_cache_lock:
        hit = _history_cache.get(range_key)
        if hit and hit["expires"] > now:
            return hit["payload"]

    # Computed outside the lock: a slow long-range query shouldn't block
    # cheap ones. A concurrent duplicate request may compute the same
    # thing twice, which is harmless and cheaper than holding the lock
    # across a multi-second query.
    payload = get_history(range_key)

    with _history_cache_lock:
        _history_cache[range_key] = {
            "expires": now + _history_cache_ttl(payload["bucket_seconds"]),
            "payload": payload,
        }
    return payload


def get_history(range_key):
    range_seconds = HISTORY_RANGES.get(range_key)  # None for "all"/unrecognized -> no cutoff
    bucket = _bucket_seconds(range_seconds)
    now = int(time.time())
    cutoff = (now - range_seconds) if range_seconds else 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        pool_rows = conn.execute(
            "SELECT (ts / ?) * ? AS bucket_ts, pool, "
            "AVG(capacity_pct) AS capacity_pct, AVG(read_bw_bytes) AS read_bw_bytes, "
            "AVG(write_bw_bytes) AS write_bw_bytes, AVG(read_iops) AS read_iops, "
            "AVG(write_iops) AS write_iops "
            "FROM pool_samples WHERE ts >= ? GROUP BY bucket_ts, pool ORDER BY bucket_ts",
            (bucket, bucket, cutoff),
        ).fetchall()

        arc_rows = conn.execute(
            "SELECT (ts / ?) * ? AS bucket_ts, AVG(hit_ratio_pct) AS hit_ratio_pct "
            "FROM arc_samples WHERE ts >= ? GROUP BY bucket_ts ORDER BY bucket_ts",
            (bucket, bucket, cutoff),
        ).fetchall()

        smart_rows = conn.execute(
            "SELECT (ts / ?) * ? AS bucket_ts, device, AVG(temperature_c) AS temperature_c "
            "FROM smart_samples WHERE ts >= ? GROUP BY bucket_ts, device ORDER BY bucket_ts",
            (bucket, bucket, cutoff),
        ).fetchall()

    pools = {}
    for r in pool_rows:
        p = pools.setdefault(r["pool"], {
            "timestamps": [], "capacity_pct": [], "read_bw_bytes": [],
            "write_bw_bytes": [], "read_iops": [], "write_iops": [],
        })
        p["timestamps"].append(r["bucket_ts"])
        p["capacity_pct"].append(r["capacity_pct"])
        p["read_bw_bytes"].append(r["read_bw_bytes"])
        p["write_bw_bytes"].append(r["write_bw_bytes"])
        p["read_iops"].append(r["read_iops"])
        p["write_iops"].append(r["write_iops"])

    arc = {"timestamps": [], "hit_ratio_pct": []}
    for r in arc_rows:
        arc["timestamps"].append(r["bucket_ts"])
        arc["hit_ratio_pct"].append(r["hit_ratio_pct"])

    smart = {}
    for r in smart_rows:
        d = smart.setdefault(r["device"], {"timestamps": [], "temperature_c": []})
        d["timestamps"].append(r["bucket_ts"])
        d["temperature_c"].append(r["temperature_c"])

    return {"pools": pools, "arc": arc, "smart": smart, "bucket_seconds": bucket}


# ---------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------
def _fast_poll_loop():
    """Capacity, health, scrub status, iostat, ARC — none of this touches
    physical disks directly, safe to sample every FAST_INTERVAL seconds."""
    while True:
        try:
            pools = get_pools()
            # One `zpool status` subprocess per pool, overlapped for the
            # same reason get_iostat() batches its call into one: these
            # are independent, I/O-bound, and serializing them makes the
            # cycle scale linearly with pool count.
            statuses = _collect_pool_statuses([p["name"] for p in pools])
            for p in pools:
                p["topology"] = statuses[p["name"]].get("topology", "Unknown")
            iostat = get_iostat([p["name"] for p in pools])
            arc = get_arc_stats()
            with _cache_lock:
                _cache["pools"] = pools
                _cache["statuses"] = statuses
                _cache["iostat"] = iostat
                _cache["arc"] = arc
                _cache["last_fast_update"] = time.time()
        except Exception as e:
            app.logger.error(f"fast poll failed: {e}")
        time.sleep(FAST_INTERVAL)


def _smart_poll_loop():
    """SMART touches physical disks and can wake a spun-down drive —
    kept on its own long interval, independent of the fast loop."""
    while True:
        try:
            pool_names = [p["name"] for p in get_pools()]
            smart = get_smart_summary(pool_names)
            pool_disks = get_pool_disks(smart, pool_names)
            with _cache_lock:
                _cache["smart"] = smart
                _cache["pool_disks"] = pool_disks
                _cache["last_smart_update"] = time.time()
        except Exception as e:
            app.logger.error(f"smart poll failed: {e}")
        time.sleep(SMART_INTERVAL)


def start_background_polling():
    threading.Thread(target=_fast_poll_loop, daemon=True).start()
    threading.Thread(target=_smart_poll_loop, daemon=True).start()
    threading.Thread(target=_record_loop, daemon=True).start()


# ---------------------------------------------------------------------
# Routes — all read from the cache, never block on subprocess calls
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon_ico():
    # Some browsers request this path directly regardless of the <link>
    # tags in the page head — serve it explicitly rather than 404ing.
    return app.send_static_file("favicon.ico")


@app.route("/api/pools")
def api_pools():
    with _cache_lock:
        return jsonify(_cache["pools"])


@app.route("/api/pool/<name>/status")
def api_pool_status(name):
    with _cache_lock:
        return jsonify(_cache["statuses"].get(name, {}))


@app.route("/api/iostat")
def api_iostat():
    with _cache_lock:
        return jsonify(_cache["iostat"])


@app.route("/api/arc")
def api_arc():
    with _cache_lock:
        return jsonify(_cache["arc"])


@app.route("/api/smart")
def api_smart():
    with _cache_lock:
        return jsonify(_cache["smart"])


@app.route("/api/smart/details")
def api_smart_details():
    """Full attribute table for one drive, for the S.M.A.R.T. panel.

    Device paths contain slashes (/dev/disk/by-id/...), so the device is
    a query parameter rather than a path segment. It's matched against
    the cached device list rather than passed to a subprocess — this
    route never shells out, it only reads what the SMART poll already
    collected, so an unknown device is a 404 rather than an opportunity
    to run smartctl against arbitrary input."""
    device = request.args.get("device", "")
    with _cache_lock:
        entry = next((d for d in _cache["smart"] if d["device"] == device), None)
    if entry is None:
        return jsonify({"error": "unknown device"}), 404
    return jsonify({
        "device": entry["device"],
        "model": entry.get("model"),
        "serial": entry.get("serial"),
        "firmware": entry.get("firmware"),
        "protocol": entry.get("protocol"),
        "health": entry.get("health"),
        "capacity_bytes": entry.get("capacity_bytes"),
        "details": entry.get("details", {"type": "none", "rows": []}),
        "last_smart_update": _cache.get("last_smart_update"),
    })


@app.route("/api/pool_disks")
def api_pool_disks():
    with _cache_lock:
        return jsonify(_cache["pool_disks"])


@app.route("/api/all")
def api_all():
    """Single call for the dashboard's periodic refresh — instant, since
    it's just a cache read regardless of how slow collection is.

    The raw `zpool status` text is stripped here: it's the biggest field
    in the payload and the dashboard never renders it. It stays available
    on the per-pool /api/pool/<name>/status endpoint for debugging.
    server_time lets the frontend compute data age without trusting the
    browser clock."""
    with _cache_lock:
        data = dict(_cache)
    data["statuses"] = {
        name: {k: v for k, v in s.items() if k != "raw"}
        for name, s in data["statuses"].items()
    }
    # Same reasoning as "raw" above: the full per-drive attribute tables
    # are only needed when someone opens the S.M.A.R.T. panel, so they're
    # served on demand from /api/smart/details rather than shipped on
    # every 5-second refresh.
    data["smart"] = [
        {k: v for k, v in d.items() if k != "details"} for d in data["smart"]
    ]
    data["server_time"] = time.time()
    return jsonify(data)


@app.route("/api/history")
def api_history():
    range_key = request.args.get("range", "24h")
    return jsonify(get_history_cached(range_key))


# A collection cycle takes a couple of seconds; if the newest fast-poll
# data is older than this many seconds, the collector is considered
# stalled and /api/health reports it (and the dashboard shows a warning
# instead of pretending cached data is current).
STALE_AFTER_SECONDS = max(FAST_INTERVAL * 6, 30)


@app.route("/api/health")
def api_health():
    """Machine-readable liveness for uptime monitors (Uptime Kuma etc.).

    Returns 200 while the collector is fresh and 503 once the newest
    fast-poll data exceeds STALE_AFTER_SECONDS — a monitoring dashboard
    whose own collector has died should fail its health check, not keep
    answering 200 from a stale cache. The route reads only the in-memory
    cache, so it can't itself block on zpool/smartctl."""
    now = time.time()
    with _cache_lock:
        last_fast = _cache["last_fast_update"]
        last_smart = _cache["last_smart_update"]
    fast_age = (now - last_fast) if last_fast else None
    stale = fast_age is None or fast_age > STALE_AFTER_SECONDS
    body = {
        "status": "stale" if stale else "ok",
        "version": __version__,
        "fast_data_age_seconds": round(fast_age, 1) if fast_age is not None else None,
        "smart_data_age_seconds": round(now - last_smart, 1) if last_smart else None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }
    return jsonify(body), (503 if stale else 200)


# Import-safe startup: tests (and tooling that just wants to import the
# parsing functions) set ZFS_MONITOR_NO_POLL=1 to skip DB creation and
# the background threads. Normal runs — python app.py, or a WSGI server
# importing app:app — start everything as before.
if os.environ.get("ZFS_MONITOR_NO_POLL") != "1":
    init_db()
    start_background_polling()

if __name__ == "__main__":
    # threaded=True so a slow-loading page (e.g. first request before the
    # initial poll completes) can't block other clients; the app itself
    # is otherwise safe for the modest request volume a monitoring
    # dashboard sees. For anything beyond a handful of viewers, run this
    # behind waitress or gunicorn instead of the Flask dev server —
    # but keep it to a single worker process. The background poller and
    # cache live in-process; multiple workers would each run their own
    # independent poller (multiplying subprocess/SMART calls) with no
    # shared cache between them.
    app.run(host=HOST, port=PORT, threaded=True)
