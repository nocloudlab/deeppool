"""API route tests via the Flask test client.

These cover the layer the parser tests don't: route wiring, JSON shaping,
cache reads, and the history bucketing/query path against a real (temp)
SQLite database. Collection itself is never invoked — the cache is
populated directly, the same way the background threads would.
"""
import json
import sqlite3
import time

import pytest

import app as appmod


@pytest.fixture
def client():
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the module at a throwaway DB and run migrations on it."""
    db = tmp_path / "history.db"
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    appmod.init_db()
    return str(db)


@pytest.fixture
def populated_cache(monkeypatch):
    cache = {
        "pools": [{
            "name": "tank", "size_bytes": 100, "alloc_bytes": 40,
            "free_bytes": 60, "fragmentation_pct": "2", "capacity_pct": 40,
            "dedup_ratio": "1.00", "health": "ONLINE", "topology": "ZFS RAID10",
        }],
        "statuses": {"tank": {
            "pool": "tank", "scan": "none requested", "vdevs": [],
            "topology": "ZFS RAID10", "raw": "RAW ZPOOL STATUS TEXT",
        }},
        "iostat": [{"pool": "tank", "read_iops": 1, "write_iops": 2,
                    "read_bw_bytes": 3, "write_bw_bytes": 4}],
        "arc": {"available": True, "hit_ratio_pct": 99.5},
        "smart": [{"device": "/dev/sda", "health": "PASSED"}],
        "pool_disks": {"tank": [{"device": "/dev/sda", "health": "PASSED"}]},
        "last_fast_update": 1000.0,
        "last_smart_update": 900.0,
    }
    monkeypatch.setattr(appmod, "_cache", cache)
    return cache


# ------------------------------------------------------------- basic routes

def test_api_pools_returns_cached_pools(client, populated_cache):
    resp = client.get("/api/pools")
    assert resp.status_code == 200
    assert resp.get_json()[0]["name"] == "tank"


def test_api_pool_status_returns_named_pool(client, populated_cache):
    resp = client.get("/api/pool/tank/status")
    assert resp.status_code == 200
    assert resp.get_json()["topology"] == "ZFS RAID10"


def test_api_pool_status_unknown_pool_is_empty_not_500(client, populated_cache):
    resp = client.get("/api/pool/nosuchpool/status")
    assert resp.status_code == 200
    assert resp.get_json() == {}


def test_api_iostat_arc_smart_pool_disks(client, populated_cache):
    assert client.get("/api/iostat").get_json()[0]["pool"] == "tank"
    assert client.get("/api/arc").get_json()["hit_ratio_pct"] == 99.5
    assert client.get("/api/smart").get_json()[0]["device"] == "/dev/sda"
    assert "tank" in client.get("/api/pool_disks").get_json()


# ----------------------------------------------------------------- /api/all

def test_api_all_strips_raw_status_text(client, populated_cache):
    """The raw zpool status blob is the biggest field in the payload and
    the dashboard never renders it — it must not ship on every 5s poll."""
    data = client.get("/api/all").get_json()
    assert "raw" not in data["statuses"]["tank"]
    # ...but the fields the frontend does use survive.
    assert data["statuses"]["tank"]["topology"] == "ZFS RAID10"
    assert data["statuses"]["tank"]["scan"] == "none requested"


def test_api_all_still_exposes_raw_on_per_pool_route(client, populated_cache):
    assert client.get("/api/pool/tank/status").get_json()["raw"] == "RAW ZPOOL STATUS TEXT"


def test_api_all_includes_server_time(client, populated_cache):
    """The frontend derives SMART data age from this rather than the
    browser clock, so its absence would silently reintroduce skew."""
    data = client.get("/api/all").get_json()
    assert isinstance(data["server_time"], (int, float))
    assert abs(data["server_time"] - time.time()) < 60


def test_api_all_does_not_mutate_the_cache(client, populated_cache):
    client.get("/api/all")
    # Stripping "raw" for the response must not strip it from the cache.
    assert populated_cache["statuses"]["tank"]["raw"] == "RAW ZPOOL STATUS TEXT"


# ------------------------------------------------------------- /api/history

def _insert_pool_sample(db, ts, pool, capacity):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO pool_samples (ts, pool, capacity_pct, alloc_bytes, "
            "read_bw_bytes, write_bw_bytes, read_iops, write_iops) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, pool, capacity, 0, 0, 0, 0, 0),
        )
        conn.commit()


def _insert_smart_sample(db, ts, device, temp):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO smart_samples (ts, device, temperature_c) VALUES (?, ?, ?)",
            (ts, device, temp),
        )
        conn.commit()


def test_history_empty_db_returns_empty_structures(client, temp_db):
    data = client.get("/api/history?range=24h").get_json()
    assert data["pools"] == {}
    assert data["arc"] == {"timestamps": [], "hit_ratio_pct": []}
    assert data["smart"] == {}
    assert data["bucket_seconds"] > 0


def test_history_returns_per_pool_series(client, temp_db):
    """Samples spaced wider than the bucket width stay distinct points.

    Spacing matters: /api/history aggregates into ~500 buckets across the
    requested range, so a 24h bucket is ~172s. Samples closer together
    than that are averaged into one point by design (see
    test_history_averages_samples_within_a_bucket), so a test wanting N
    points must space them beyond the bucket width rather than assume
    one row equals one point."""
    now = int(time.time())
    bucket = appmod._bucket_seconds(appmod.HISTORY_RANGES["24h"])
    for i in range(3):
        _insert_pool_sample(temp_db, now - i * bucket * 3, "tank", 40 + i)
    data = client.get("/api/history?range=24h").get_json()
    assert "tank" in data["pools"]
    assert len(data["pools"]["tank"]["timestamps"]) == 3


def test_history_averages_samples_within_a_bucket(client, temp_db):
    """The flip side, and the reason the bug above was possible: several
    samples inside one bucket collapse to a single averaged point. This
    is what keeps a 12-month view at ~500 points instead of ~500,000."""
    # Anchored to the start of a bucket, not "now" — three samples 20s
    # apart around an arbitrary instant could straddle a boundary and
    # produce two points, making the test pass or fail by wall clock.
    bucket = appmod._bucket_seconds(appmod.HISTORY_RANGES["24h"])
    base = (int(time.time()) // bucket) * bucket
    for i, cap in enumerate((30, 40, 50)):
        _insert_pool_sample(temp_db, base + i * 5, "tank", cap)
    series = client.get("/api/history?range=24h").get_json()["pools"]["tank"]
    assert len(series["timestamps"]) == 1
    assert series["capacity_pct"][0] == pytest.approx(40.0)


def test_history_pools_with_different_depths_keep_own_timestamps(client, temp_db):
    """Regression guard for the chart-alignment bug: a pool added later
    has fewer samples starting at a later time. The API must report each
    series with its own timestamps (the frontend aligns them onto a
    shared axis) rather than implying a shared timeline."""
    now = int(time.time())
    # A day apart: comfortably wider than the 30d bucket (~86 min), so
    # each sample is its own point wherever the boundaries fall.
    for i in range(5):
        _insert_pool_sample(temp_db, now - i * 86400, "old_pool", 50)
    # New pool only exists for the two most recent buckets.
    for i in range(2):
        _insert_pool_sample(temp_db, now - i * 86400, "new_pool", 10)

    data = client.get("/api/history?range=30d").get_json()
    old_ts = data["pools"]["old_pool"]["timestamps"]
    new_ts = data["pools"]["new_pool"]["timestamps"]

    assert len(old_ts) > len(new_ts)
    # The shorter series must start later, not be silently left-padded.
    assert min(new_ts) > min(old_ts)
    # Every series is internally consistent: one value per timestamp.
    for series in data["pools"].values():
        assert len(series["timestamps"]) == len(series["capacity_pct"])


def test_history_devices_with_different_depths(client, temp_db):
    """Same guard for disks — the likeliest real-world case, since a
    replaced drive has no samples before its swap-in date."""
    now = int(time.time())
    for i in range(4):
        _insert_smart_sample(temp_db, now - i * 86400, "/dev/sda", 35)
    _insert_smart_sample(temp_db, now, "/dev/sdb", 40)

    data = client.get("/api/history?range=30d").get_json()
    assert len(data["smart"]["/dev/sda"]["timestamps"]) > len(
        data["smart"]["/dev/sdb"]["timestamps"]
    )
    for series in data["smart"].values():
        assert len(series["timestamps"]) == len(series["temperature_c"])


def test_history_range_affects_bucket_size(client, temp_db):
    short = client.get("/api/history?range=1h").get_json()["bucket_seconds"]
    long = client.get("/api/history?range=30d").get_json()["bucket_seconds"]
    assert long > short


def test_history_unknown_range_does_not_error(client, temp_db):
    resp = client.get("/api/history?range=bogus")
    assert resp.status_code == 200
    assert "pools" in resp.get_json()


def test_history_excludes_samples_outside_range(client, temp_db):
    now = int(time.time())
    _insert_pool_sample(temp_db, now, "tank", 40)
    _insert_pool_sample(temp_db, now - 10 * 86400, "tank", 20)  # 10 days old
    data = client.get("/api/history?range=1h").get_json()
    assert len(data["pools"]["tank"]["timestamps"]) == 1


# ------------------------------------------------------------- migrations

def test_init_db_sets_schema_version(temp_db):
    with sqlite3.connect(temp_db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(appmod.MIGRATIONS)


def test_init_db_is_idempotent(temp_db):
    """Re-running migrations (every service start) must be a no-op, and
    must not wipe existing history."""
    now = int(time.time())
    _insert_pool_sample(temp_db, now, "tank", 40)
    appmod.init_db()
    appmod.init_db()
    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM pool_samples").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert rows == 1
    assert version == len(appmod.MIGRATIONS)


def test_init_db_upgrades_a_pre_versioning_database(tmp_path, monkeypatch):
    """Databases created before migrations existed have user_version 0 but
    already contain the tables. Migration 001 is written to be safe to
    replay against them."""
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        appmod._migration_001_initial(conn)
        conn.execute(
            "INSERT INTO pool_samples (ts, pool, capacity_pct) VALUES (1, 'tank', 40)"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    appmod.init_db()

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(appmod.MIGRATIONS)
        assert conn.execute("SELECT COUNT(*) FROM pool_samples").fetchone()[0] == 1


def test_init_db_leaves_newer_schema_alone(tmp_path, monkeypatch, caplog):
    """A DB written by a future version must not be downgraded or have
    old migrations replayed over it."""
    db = tmp_path / "future.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {len(appmod.MIGRATIONS) + 5}")
        conn.commit()

    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    appmod.init_db()

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(appmod.MIGRATIONS) + 5


# --------------------------------------------------- /api/smart/details

NVME_RAW = json.dumps({
    "model_name": "Samsung SSD 970 EVO Plus 1TB",
    "serial_number": "S4EWNJ0N222537Z",
    "firmware_version": "2B2QEXM7",
    "device": {"protocol": "NVMe"},
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "critical_warning": 0, "temperature": 37, "available_spare": 100,
        "available_spare_threshold": 10, "percentage_used": 2,
        "data_units_written": 96396648, "media_errors": 0,
        "power_on_hours": 11849, "temperature_sensors": [37, 43],
    },
})


@pytest.fixture
def smart_cache(monkeypatch):
    entry = appmod._parse_smart_json("/dev/nvme0n1", NVME_RAW)
    monkeypatch.setattr(appmod, "_cache", {
        "pools": [], "statuses": {}, "iostat": [], "arc": {},
        "smart": [entry], "pool_disks": {},
        "last_fast_update": 1.0, "last_smart_update": 2.0,
    })
    return entry


def test_smart_details_returns_identity_and_rows(client, smart_cache):
    resp = client.get("/api/smart/details?device=/dev/nvme0n1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["serial"] == "S4EWNJ0N222537Z"
    assert data["firmware"] == "2B2QEXM7"
    assert data["protocol"] == "NVMe"
    assert data["details"]["type"] == "nvme"
    ids = [r["id"] for r in data["details"]["rows"]]
    assert "0" in ids and "175:160" in ids and "201:200" in ids


def test_smart_details_unknown_device_is_404(client, smart_cache):
    assert client.get("/api/smart/details?device=/dev/nope").status_code == 404


def test_smart_details_missing_param_is_404(client, smart_cache):
    assert client.get("/api/smart/details").status_code == 404


def test_api_all_omits_detail_rows(client, smart_cache):
    """The full attribute table is only needed when the panel is opened —
    it must not ride along on every 5s refresh."""
    data = client.get("/api/all").get_json()
    assert "details" not in data["smart"][0]
    # Summary fields the dashboard table renders are still present.
    assert data["smart"][0]["nvme_wear_pct"] == 2


def test_api_all_does_not_strip_details_from_the_cache(client, smart_cache):
    client.get("/api/all")
    assert "details" in appmod._cache["smart"][0]


# --------------------------------------------------------- history cache

@pytest.fixture(autouse=True)
def _clear_history_cache():
    appmod._history_cache.clear()
    yield
    appmod._history_cache.clear()


def test_history_is_cached_between_requests(client, temp_db, monkeypatch):
    """Long ranges aggregate every sample in the window, so repeat calls
    must not re-run the query."""
    now = int(time.time())
    _insert_pool_sample(temp_db, now, "tank", 40)

    calls = []
    real = appmod.get_history

    def counting(range_key):
        calls.append(range_key)
        return real(range_key)

    monkeypatch.setattr(appmod, "get_history", counting)

    client.get("/api/history?range=365d")
    client.get("/api/history?range=365d")
    client.get("/api/history?range=365d")
    assert calls == ["365d"]


def test_history_cache_is_per_range(client, temp_db, monkeypatch):
    calls = []
    real = appmod.get_history
    monkeypatch.setattr(appmod, "get_history",
                        lambda r: (calls.append(r), real(r))[1])
    client.get("/api/history?range=24h")
    client.get("/api/history?range=365d")
    client.get("/api/history?range=24h")
    assert calls == ["24h", "365d"]


def test_history_cache_expires(client, temp_db, monkeypatch):
    calls = []
    real = appmod.get_history
    monkeypatch.setattr(appmod, "get_history",
                        lambda r: (calls.append(r), real(r))[1])
    client.get("/api/history?range=90d")
    appmod._history_cache["90d"]["expires"] = time.time() - 1
    client.get("/api/history?range=90d")
    assert calls == ["90d", "90d"]


def test_cached_payload_matches_fresh_computation(client, temp_db):
    now = int(time.time())
    for i in range(3):
        _insert_pool_sample(temp_db, now - i * 3600, "tank", 40 + i)
    cached = appmod.get_history_cached("30d")
    fresh = appmod.get_history("30d")
    assert cached["bucket_seconds"] == fresh["bucket_seconds"]
    assert cached["pools"]["tank"]["capacity_pct"] == fresh["pools"]["tank"]["capacity_pct"]


def test_history_cache_ttl_bounds():
    # Never shorter than the recorder interval...
    assert appmod._history_cache_ttl(1) >= appmod.RECORD_INTERVAL
    # ...and never so long that a chart goes stale for hours.
    assert appmod._history_cache_ttl(10 ** 6) == appmod.HISTORY_CACHE_MAX_TTL


def test_long_range_endpoints_respond(client, temp_db):
    for rng in ("90d", "180d", "365d"):
        resp = client.get(f"/api/history?range={rng}")
        assert resp.status_code == 200, rng
        assert "pools" in resp.get_json()


# ---------------------------------------------------------- /api/health

def test_health_ok_when_collector_fresh(client, monkeypatch):
    monkeypatch.setattr(appmod, "_cache", {
        **{k: [] if isinstance(v, list) else {} for k, v in appmod._cache.items()
           if k not in ("last_fast_update", "last_smart_update")},
        "last_fast_update": time.time() - 2,
        "last_smart_update": time.time() - 100,
    })
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["version"] == appmod.__version__
    assert data["fast_data_age_seconds"] < appmod.STALE_AFTER_SECONDS


def test_health_503_when_collector_stalled(client, monkeypatch):
    monkeypatch.setattr(appmod, "_cache", {
        **{k: [] if isinstance(v, list) else {} for k, v in appmod._cache.items()
           if k not in ("last_fast_update", "last_smart_update")},
        "last_fast_update": time.time() - appmod.STALE_AFTER_SECONDS - 60,
        "last_smart_update": None,
    })
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "stale"


def test_health_503_before_first_poll(client, monkeypatch):
    monkeypatch.setattr(appmod, "_cache", {
        **{k: [] if isinstance(v, list) else {} for k, v in appmod._cache.items()
           if k not in ("last_fast_update", "last_smart_update")},
        "last_fast_update": None,
        "last_smart_update": None,
    })
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.get_json()["fast_data_age_seconds"] is None
