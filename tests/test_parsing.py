"""Parser tests against realistic `zpool status` / `zpool list` output.

The fixtures reproduce the real format quirks that produced bugs in the
past — most importantly the literal tab character that prefixes every
config line (see classify_topology), with space-based tree nesting
inside that.
"""
import app


def status(pool, config_lines, scan="none requested"):
    body = "\n".join("\t" + l for l in config_lines)
    return (
        f"  pool: {pool}\n"
        f" state: ONLINE\n"
        f"  scan: {scan}\n"
        f"config:\n"
        f"\n"
        f"\tNAME        STATE     READ WRITE CKSUM\n"
        f"{body}\n"
        f"\n"
        f"errors: No known data errors\n"
    )


# ---------------------------------------------------------------- topology

def test_raid1_mirror():
    out = status("rpool", [
        "rpool                        ONLINE  0 0 0",
        "  mirror-0                   ONLINE  0 0 0",
        "    /dev/disk/by-id/nvme-A   ONLINE  0 0 0",
        "    /dev/disk/by-id/nvme-B   ONLINE  0 0 0",
    ])
    assert app.classify_topology("rpool", out) == "ZFS RAID1"


def test_three_way_mirror():
    out = status("p", [
        "p             ONLINE  0 0 0",
        "  mirror-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "    /dev/sdc1  ONLINE  0 0 0",
    ])
    assert app.classify_topology("p", out) == "ZFS RAID1 (3-way mirror)"


def test_raid10_two_striped_mirrors():
    out = status("tank", [
        "tank          ONLINE  0 0 0",
        "  mirror-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "  mirror-1    ONLINE  0 0 0",
        "    /dev/sdc1  ONLINE  0 0 0",
        "    /dev/sdd1  ONLINE  0 0 0",
    ])
    assert app.classify_topology("tank", out) == "ZFS RAID10"


def test_raidz2():
    out = status("z2", [
        "z2            ONLINE  0 0 0",
        "  raidz2-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "    /dev/sdc1  ONLINE  0 0 0",
        "    /dev/sdd1  ONLINE  0 0 0",
    ])
    assert app.classify_topology("z2", out) == "ZFS RAIDZ2"


def test_striped_raidz1():
    out = status("z", [
        "z             ONLINE  0 0 0",
        "  raidz1-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "    /dev/sdc1  ONLINE  0 0 0",
        "  raidz1-1    ONLINE  0 0 0",
        "    /dev/sdd1  ONLINE  0 0 0",
        "    /dev/sde1  ONLINE  0 0 0",
        "    /dev/sdf1  ONLINE  0 0 0",
    ])
    assert app.classify_topology("z", out) == "ZFS RAIDZ1 (striped, 2x)"


def test_single_disk():
    out = status("s", [
        "s             ONLINE  0 0 0",
        "  /dev/sda1   ONLINE  0 0 0",
    ])
    assert app.classify_topology("s", out) == "ZFS Single Disk"


def test_raid0_stripe():
    out = status("s", [
        "s             ONLINE  0 0 0",
        "  /dev/sda1   ONLINE  0 0 0",
        "  /dev/sdb1   ONLINE  0 0 0",
    ])
    assert app.classify_topology("s", out) == "ZFS RAID0 (striped, no redundancy)"


def test_mixed_topology():
    out = status("m", [
        "m             ONLINE  0 0 0",
        "  mirror-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "  /dev/sdc1   ONLINE  0 0 0",
    ])
    assert app.classify_topology("m", out) == "ZFS Mixed Topology"


def test_logs_and_cache_sections_ignored():
    # logs/cache are printed at the same indent level as the pool name
    # itself — regression test for the flush-left special-vdev bug.
    out = status("rpool", [
        "rpool         ONLINE  0 0 0",
        "  mirror-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "logs          ",
        "  /dev/sde1   ONLINE  0 0 0",
        "cache         ",
        "  /dev/sdf1   ONLINE  0 0 0",
    ])
    assert app.classify_topology("rpool", out) == "ZFS RAID1"


def test_empty_topology_unknown():
    assert app._describe_topology([]) == "Unknown"


# ------------------------------------------------------------- zpool list

def test_get_pools_parses_hp_output(monkeypatch):
    canned = (
        "rpool\t998579896320\t8850831360\t989729064960\t3\t14\t1.00\tONLINE\n"
        "tank\t15992117387264\t6047313952768\t9944803434496\t1\t37\t1.00\tONLINE\n"
    )
    monkeypatch.setattr(app, "run", lambda cmd: canned)
    pools = app.get_pools()
    assert [p["name"] for p in pools] == ["rpool", "tank"]
    assert pools[0]["capacity_pct"] == 14
    assert pools[0]["health"] == "ONLINE"
    assert pools[1]["size_bytes"] == 15992117387264
    assert pools[1]["fragmentation_pct"] == "1"


def test_get_pools_skips_malformed_lines(monkeypatch):
    monkeypatch.setattr(app, "run", lambda cmd: "garbage line\n")
    assert app.get_pools() == []


# ---------------------------------------------------------- leaf devices

def test_get_leaf_devices(monkeypatch):
    out = status("tank", [
        "tank          ONLINE  0 0 0",
        "  mirror-0    ONLINE  0 0 0",
        "    /dev/sda1  ONLINE  0 0 0",
        "    /dev/sdb1  ONLINE  0 0 0",
        "logs          ",
        "  /dev/sde1   ONLINE  0 0 0",
    ])
    monkeypatch.setattr(app, "run", lambda cmd: out)
    devs = app.get_leaf_devices("tank")
    assert "/dev/sda1" in devs and "/dev/sdb1" in devs
    assert not any(d.startswith("mirror") for d in devs)


# ------------------------------------------------------------- scan regex

def test_scan_regex_extracts_scrub_line():
    raw = status(
        "tank",
        ["tank ONLINE 0 0 0"],
        scan="scrub repaired 0B in 08:15:23 with 0 errors on Sun Jul 13 08:39:24 2025",
    )
    m = app.SCAN_RE.search(raw)
    assert m
    assert m.group(1).strip().startswith("scrub repaired 0B")


# --------------------------------------------------------------- smartctl

def test_parse_smart_json_ata_drive():
    import json as _json
    raw = _json.dumps({
        "model_name": "ST8000VN004-3CP101",
        "user_capacity": {"bytes": 8001563222016},
        "smart_status": {"passed": True},
        "temperature": {"current": 34},
        "power_on_time": {"hours": 12000},
        "ata_smart_attributes": {"table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 9, "raw": {"value": 12000}},
        ]},
    })
    entry = app._parse_smart_json("/dev/sda", raw)
    assert entry["health"] == "PASSED"
    assert entry["reallocated_sectors"] == 0
    assert entry["temperature_c"] == 34
    # ATA drives have no NVMe endurance log — fields stay None.
    assert entry["nvme_wear_pct"] is None
    assert entry["nvme_media_errors"] is None
    assert entry["nvme_data_written_bytes"] is None


def test_parse_smart_json_nvme_drive_endurance_fields():
    import json as _json
    raw = _json.dumps({
        "model_name": "CT1000T500SSD8",
        "user_capacity": {"bytes": 1000204886016},
        "smart_status": {"passed": True},
        "temperature": {"current": 37},
        "power_on_time": {"hours": 5000},
        "nvme_smart_health_information_log": {
            "percentage_used": 3,
            "media_errors": 0,
            "data_units_written": 20000000,
        },
    })
    entry = app._parse_smart_json("/dev/nvme0n1", raw)
    assert entry["health"] == "PASSED"
    assert entry["nvme_wear_pct"] == 3
    assert entry["nvme_media_errors"] == 0
    # 20,000,000 data units * 512,000 bytes/unit
    assert entry["nvme_data_written_bytes"] == 20_000_000 * 512_000
    # NVMe drives report no ATA reallocated-sector attribute.
    assert entry["reallocated_sectors"] is None


def test_parse_smart_json_nvme_high_wear_and_media_errors():
    import json as _json
    raw = _json.dumps({
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "percentage_used": 97,
            "media_errors": 4,
            "data_units_written": 500000000,
        },
    })
    entry = app._parse_smart_json("/dev/nvme1n1", raw)
    assert entry["nvme_wear_pct"] == 97
    assert entry["nvme_media_errors"] == 4


def test_parse_smart_json_handles_garbage_output():
    entry = app._parse_smart_json("/dev/sdz", "not json at all")
    assert entry["device"] == "/dev/sdz"
    assert entry["health"] == "unknown"
    assert entry["nvme_wear_pct"] is None


def test_parse_smart_json_missing_smart_status():
    import json as _json
    entry = app._parse_smart_json("/dev/sdz", _json.dumps({}))
    assert entry["health"] == "unknown"


# ------------------------------------------------- SMART detail (panel)

def _nvme_payload(**overrides):
    log = {
        "critical_warning": 0,
        "temperature": 37,
        "available_spare": 100,
        "available_spare_threshold": 10,
        "percentage_used": 2,
        "data_units_read": 157619882,
        "data_units_written": 96396648,
        "host_reads": 1503449280,
        "host_writes": 1091706487,
        "controller_busy_time": 3752,
        "power_cycles": 2261,
        "power_on_hours": 11849,
        "unsafe_shutdowns": 124,
        "media_errors": 0,
        "num_err_log_entries": 12725,
        "warning_temp_time": 0,
        "critical_comp_time": 0,
        "temperature_sensors": [37, 43],
    }
    log.update(overrides)
    return {"nvme_smart_health_information_log": log}


def _rows_by_label(rows):
    return {r["label"]: r for r in rows}


def test_nvme_details_use_spec_byte_offsets():
    details = app._build_smart_details(_nvme_payload())
    assert details["type"] == "nvme"
    by_id = {r["id"]: r["label"] for r in details["rows"]}
    # Offsets straight from the NVMe SMART/Health Information Log layout.
    assert by_id["0"] == "Critical Warning"
    assert by_id["5"] == "Percentage Used (%)"
    assert by_id["63:48"] == "Data Units Written"
    assert by_id["143:128"] == "Power On Hours"
    assert by_id["175:160"] == "Media and Data Integrity Errors"


def test_nvme_temperature_sensors_get_consecutive_offsets():
    details = app._build_smart_details(_nvme_payload())
    sensors = [r for r in details["rows"] if r["label"].startswith("Temperature Sensor")]
    assert len(sensors) == 2
    assert sensors[0]["id"] == "201:200"
    assert sensors[1]["id"] == "203:202"


def test_nvme_healthy_drive_is_all_good():
    details = app._build_smart_details(_nvme_payload())
    assert all(r["status"] == "good" for r in details["rows"]
               if not r["label"].startswith("Unsafe"))


def test_nvme_critical_warning_flags_critical():
    details = app._build_smart_details(_nvme_payload(critical_warning=1))
    assert _rows_by_label(details["rows"])["Critical Warning"]["status"] == "critical"


def test_nvme_wear_thresholds():
    fair = app._build_smart_details(_nvme_payload(percentage_used=75))
    crit = app._build_smart_details(_nvme_payload(percentage_used=95))
    assert _rows_by_label(fair["rows"])["Percentage Used (%)"]["status"] == "fair"
    assert _rows_by_label(crit["rows"])["Percentage Used (%)"]["status"] == "critical"


def test_nvme_media_errors_are_binary():
    details = app._build_smart_details(_nvme_payload(media_errors=1))
    assert _rows_by_label(details["rows"])["Media and Data Integrity Errors"]["status"] == "critical"


def test_nvme_available_spare_below_threshold_is_critical():
    details = app._build_smart_details(_nvme_payload(available_spare=5, available_spare_threshold=10))
    assert _rows_by_label(details["rows"])["Available Spare (%)"]["status"] == "critical"


def test_nvme_omits_fields_the_drive_does_not_report():
    payload = {"nvme_smart_health_information_log": {"percentage_used": 1}}
    rows = app._build_smart_details(payload)["rows"]
    assert [r["label"] for r in rows] == ["Percentage Used (%)"]


def test_ata_details_use_attribute_ids():
    payload = {"ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100,
         "thresh": 10, "raw": {"value": 0, "string": "0"}},
        {"id": 194, "name": "Temperature_Celsius", "value": 65, "worst": 50,
         "thresh": 0, "raw": {"value": 35, "string": "35 (Min/Max 20/45)"}},
    ]}}
    details = app._build_smart_details(payload)
    assert details["type"] == "ata"
    ids = [r["id"] for r in details["rows"]]
    assert ids == [5, 194]
    # Underscores in smartctl's attribute names are humanised.
    assert details["rows"][0]["label"] == "Reallocated Sector Ct"
    # The formatted raw string is preferred over the bare integer, since
    # some attributes pack several counters into one raw field.
    assert details["rows"][1]["value"] == "35 (Min/Max 20/45)"


def test_ata_pending_sectors_escalate_with_count():
    def payload(raw):
        return {"ata_smart_attributes": {"table": [
            {"id": 197, "name": "Current_Pending_Sector", "value": 100,
             "worst": 100, "thresh": 0, "raw": {"value": raw}},
        ]}}
    assert app._build_smart_details(payload(0))["rows"][0]["status"] == "good"
    assert app._build_smart_details(payload(3))["rows"][0]["status"] == "fair"
    assert app._build_smart_details(payload(50))["rows"][0]["status"] == "critical"


def test_ata_when_failed_now_is_critical():
    payload = {"ata_smart_attributes": {"table": [
        {"id": 1, "name": "Raw_Read_Error_Rate", "value": 5, "worst": 5,
         "thresh": 50, "when_failed": "now", "raw": {"value": 1}},
    ]}}
    assert app._build_smart_details(payload)["rows"][0]["status"] == "critical"


def test_ata_value_near_threshold_is_fair():
    payload = {"ata_smart_attributes": {"table": [
        {"id": 1, "name": "Raw_Read_Error_Rate", "value": 55, "worst": 55,
         "thresh": 50, "raw": {"value": 1}},
    ]}}
    assert app._build_smart_details(payload)["rows"][0]["status"] == "fair"


def test_details_none_when_drive_reports_neither_format():
    assert app._build_smart_details({})["type"] == "none"
    assert app._build_smart_details({})["rows"] == []


def test_parse_smart_json_attaches_details_and_identity():
    import json as _json
    raw = _json.dumps({
        "model_name": "Samsung SSD 970 EVO Plus 1TB",
        "serial_number": "S4EWNJ0N222537Z",
        "firmware_version": "2B2QEXM7",
        "device": {"protocol": "NVMe"},
        "smart_status": {"passed": True},
        **_nvme_payload(),
    })
    entry = app._parse_smart_json("/dev/nvme0n1", raw)
    assert entry["serial"] == "S4EWNJ0N222537Z"
    assert entry["firmware"] == "2B2QEXM7"
    assert entry["protocol"] == "NVMe"
    assert entry["details"]["type"] == "nvme"
    assert len(entry["details"]["rows"]) > 10


def test_parse_smart_json_details_default_on_garbage():
    entry = app._parse_smart_json("/dev/sdz", "not json")
    assert entry["details"] == {"type": "none", "rows": []}


# ------------------------------------------------------- history ranges

def test_history_ranges_cover_the_picker_buttons():
    for key in ("1h", "24h", "7d", "30d", "90d", "180d", "365d"):
        assert key in app.HISTORY_RANGES, key


def test_history_ranges_are_ordered_and_correct():
    r = app.HISTORY_RANGES
    assert r["90d"] == 90 * 86400
    assert r["180d"] == 180 * 86400
    assert r["365d"] == 365 * 86400
    values = [r[k] for k in ("1h", "24h", "7d", "30d", "90d", "180d", "365d")]
    assert values == sorted(values)


def test_long_ranges_still_bucket_to_about_target_points():
    """Whatever the range, the server aggregates to ~500 points so the
    browser never receives a year of raw samples."""
    for key in ("30d", "90d", "180d", "365d"):
        bucket = app._bucket_seconds(app.HISTORY_RANGES[key])
        points = app.HISTORY_RANGES[key] / bucket
        assert 400 <= points <= 600, (key, points)


def test_bucket_never_finer_than_the_record_interval():
    # A 1h range over 500 points would imply 7s buckets, but samples are
    # only written once a minute — finer buckets would be empty noise.
    assert app._bucket_seconds(3600) >= app.RECORD_INTERVAL
