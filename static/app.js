const REFRESH_MS = 5000;
const HISTORY_LEN = 30;

// All strings that originate from CLI output (pool names, device paths,
// disk models) get escaped before being interpolated into HTML. On a
// trusted LAN this is belt-and-braces, but pool names are user-created
// strings and there's no reason to trust them as markup.
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// zpool status abbreviates large error counts (e.g. "1.2K", "3M") —
// naive parseInt("1.2K") returns 1 and understates the problem.
function parseZfsCount(s) {
  const m = String(s).trim().match(/^([\d.]+)([KMGTP]?)$/i);
  if (!m) return 0;
  const mult = { "": 1, K: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15 }[m[2].toUpperCase()];
  return Math.round(parseFloat(m[1]) * mult);
}

function bytesToHuman(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, val = bytes;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
  return `${val.toFixed(val >= 100 ? 0 : 1)} ${units[i]}`;
}

function countToHuman(n) {
  if (n === null || n === undefined) return "—";
  const units = ["", "K", "M", "B", "T"];
  let i = 0, val = n;
  while (val >= 1000 && i < units.length - 1) { val /= 1000; i++; }
  return `${val.toFixed(val >= 100 || i === 0 ? 0 : 1)}${units[i]}`;
}

// --- Data freshness ------------------------------------------------------
// The API serves from an in-memory cache, so a fetch can succeed while
// the underlying collector thread is stalled (hung subprocess, ZFS
// deadlock). Stamping "Updated <now>" on every successful fetch would
// present hours-old data as current — the worst failure mode a
// monitoring tool can have. Both helpers are pure for testability;
// the 30s threshold matches STALE_AFTER_SECONDS on the server.
function dataAgeSeconds(serverTime, lastFastUpdate) {
  if (!serverTime || !lastFastUpdate) return null;
  return Math.max(0, Math.round(serverTime - lastFastUpdate));
}

function updatedLabel(timeString, ageSeconds, staleAfter = 30) {
  if (ageSeconds === null) return `Updated ${timeString}`;
  if (ageSeconds <= staleAfter) return `Updated ${timeString}`;
  const mins = Math.round(ageSeconds / 60);
  const age = ageSeconds < 120 ? `${ageSeconds}s` : `${mins} min`;
  return `Collector stalled — data is ${age} old`;
}

// --- Threshold helpers -------------------------------------------------
// Each returns "good" / "warn" / "bad" (or "" for values with no verdict,
// e.g. unknown/null). wrap() renders the value with the matching class.
function wrap(value, cls) {
  return cls ? `<span class="value-${cls}">${value}</span>` : `<span>${value}</span>`;
}

function capacityClass(pct) {
  if (pct >= 90) return "bad";
  if (pct >= 75) return "warn";
  return "good";
}

function fragClass(pct) {
  if (pct >= 50) return "bad";
  if (pct >= 30) return "warn";
  return "good";
}

// Hit ratio: higher is better, thresholds run in reverse.
function hitRatioClass(pct) {
  if (pct < 80) return "bad";
  if (pct < 90) return "warn";
  return "good";
}

function tempClass(celsius) {
  if (celsius === null || celsius === undefined) return "";
  if (celsius >= 50) return "bad";
  if (celsius >= 40) return "warn";
  return "good";
}

// Rough rule of thumb: consumer drives ~3yr warranty, NAS-rated ~5yr.
// Not a hard failure signal — just a "worth keeping an eye on" nudge.
function powerOnHoursClass(hours) {
  if (hours === null || hours === undefined) return "";
  if (hours >= 43800) return "bad";   // 5 years
  if (hours >= 26280) return "warn";  // 3 years
  return "good";
}

function reallocatedClass(n) {
  if (n === null || n === undefined) return "";
  if (n > 10) return "bad";
  if (n > 0) return "warn";
  return "good";
}

// NVMe's own percentage_used estimate of endurance consumed against its
// rated write endurance. The spec allows values over 100 for a drive
// that's outlived its rated life but still functions, so this can't
// just be treated as a 0-100 capacity bar.
function nvmeWearClass(pct) {
  if (pct === null || pct === undefined) return "";
  if (pct >= 90) return "bad";
  if (pct >= 70) return "warn";
  return "good";
}

// Unlike wear_pct's gradual endurance consumption, media_errors counts
// unrecovered data-integrity errors — any nonzero count is a real
// signal, so this is binary like the ZFS vdev error counters.
function mediaErrorsClass(n) {
  if (n === null || n === undefined) return "";
  return n > 0 ? "bad" : "good";
}

// ZFS read/write/checksum error counters: any nonzero count is a real
// data-integrity signal, not a gradient — so this one is binary.
function errorCountClass(n) {
  return n > 0 ? "bad" : "good";
}

function capacityBarClass(pct) {
  // separate from capacityClass() only so the bar-fill CSS class names
  // ("", "warn", "bad") stay distinct from the value-* text classes
  if (pct >= 90) return "bad";
  if (pct >= 75) return "warn";
  return "";
}

const expandedPools = new Set();

function renderDiskRow(d, labelFor) {
  const basename = d.device.split("/").pop();
  const label = escapeHtml(labelFor(basename));
  const fullId = escapeHtml(basename);
  const sizeStr = d.capacity_bytes ? bytesToHuman(d.capacity_bytes) : "—";
  const modelStr = escapeHtml(d.model || "unknown model");
  const health = escapeHtml(d.health);
  const tempCell = (d.temperature_c === null || d.temperature_c === undefined)
    ? "—" : wrap(d.temperature_c + "°C", tempClass(d.temperature_c));
  // title= gives the full by-id device name on hover, without forcing
  // a truncated/ellipsized version of it into the row itself.
  return `
    <div class="disk-row">
      <span class="disk-name" title="${fullId}">${label}</span>
      <span class="disk-detail">${sizeStr} · ${modelStr}</span>
      <span class="disk-detail">${tempCell} <span class="badge ${health}">${health}</span></span>
    </div>`;
}

function renderPools(pools, poolDisks) {
  const grid = document.getElementById("pools-grid");
  // Build the full HTML first and assign once — innerHTML += inside a
  // loop re-parses the whole grid on every iteration (O(n²) DOM work).
  grid.innerHTML = pools.map(p => {
    const name = escapeHtml(p.name);
    const health = escapeHtml(p.health);
    const barCls = capacityBarClass(p.capacity_pct);
    const isExpanded = expandedPools.has(p.name);
    const disks = (poolDisks && poolDisks[p.name]) || [];
    const labelFor = makeDeviceLabeler();
    const disksHtml = disks.length
      ? disks.map(d => renderDiskRow(d, labelFor)).join("")
      : `<div class="disk-row disk-row-empty">No disk info yet — available after the first SMART poll</div>`;

    return `
      <div class="card">
        <div class="title">${name} <span class="badge ${health}">${health}</span></div>
        <div class="topology-label">${escapeHtml(p.topology || "Unknown")}</div>
        <div class="bar-bg"><div class="bar-fill ${barCls}" style="width:${p.capacity_pct}%"></div></div>
        <div class="stat-line"><span>${bytesToHuman(p.alloc_bytes)} used</span>${wrap(p.capacity_pct + "%", capacityClass(p.capacity_pct))}</div>
        <div class="stat-line"><span>${bytesToHuman(p.free_bytes)} free</span><span>of ${bytesToHuman(p.size_bytes)}</span></div>
        <div class="stat-line"><span>Fragmentation</span>${wrap(escapeHtml(p.fragmentation_pct) + "%", fragClass(p.fragmentation_pct))}</div>
        <div class="stat-line"><span>Dedup ratio</span><span>${escapeHtml(p.dedup_ratio)}</span></div>
        <button class="more-info-btn" data-pool="${name}">${isExpanded ? "Less info ▴" : "More info ▾"}</button>
        <div class="pool-disks" style="display:${isExpanded ? "block" : "none"}">${disksHtml}</div>
      </div>`;
  }).join("");

  grid.querySelectorAll(".more-info-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const pool = btn.dataset.pool;
      if (expandedPools.has(pool)) expandedPools.delete(pool);
      else expandedPools.add(pool);
      renderPools(pools, poolDisks);
    });
  });
}

function renderArc(arc) {
  const grid = document.getElementById("arc-grid");
  if (!arc.available) {
    grid.innerHTML = `<div class="card">ARC stats unavailable (not on this kernel/container).</div>`;
    return;
  }
  grid.innerHTML = `
    <div class="card">
      <div class="title">Hit Ratio</div>
      <div class="stat-line">${wrap(arc.hit_ratio_pct + "%", hitRatioClass(arc.hit_ratio_pct))}<span>${countToHuman(arc.hits)} hits / ${countToHuman(arc.misses)} misses</span></div>
    </div>
    <div class="card">
      <div class="title">ARC Size</div>
      <div class="stat-line"><span>${bytesToHuman(arc.size_bytes)}</span><span>target ${bytesToHuman(arc.target_size_bytes)}</span></div>
      <div class="stat-line"><span>min ${bytesToHuman(arc.min_size_bytes)}</span><span>max ${bytesToHuman(arc.max_size_bytes)}</span></div>
    </div>
    <div class="card">
      <div class="title">MFU / MRU</div>
      <div class="stat-line"><span>MFU ${bytesToHuman(arc.mfu_size_bytes)}</span><span>MRU ${bytesToHuman(arc.mru_size_bytes)}</span></div>
    </div>`;
}

function scanErrorClass(scanText) {
  const m = scanText.match(/with (\d+) errors?/);
  if (!m) return "";
  return errorCountClass(parseInt(m[1], 10));
}

function renderScrub(statuses) {
  const list = document.getElementById("scrub-list");
  list.innerHTML = Object.values(statuses).map(s => {
    const scanLine = s.scan.split("\n")[0];
    const scanCls = scanErrorClass(scanLine);
    const vdevRows = s.vdevs.map(v => {
      const state = escapeHtml(v.state);
      const r = wrap(`R:${escapeHtml(v.read_errors)}`, errorCountClass(parseZfsCount(v.read_errors)));
      const w = wrap(`W:${escapeHtml(v.write_errors)}`, errorCountClass(parseZfsCount(v.write_errors)));
      const c = wrap(`C:${escapeHtml(v.cksum_errors)}`, errorCountClass(parseZfsCount(v.cksum_errors)));
      return `<div class="stat-line"><span>${escapeHtml(v.name)} <span class="badge ${state}">${state}</span></span><span>${r} ${w} ${c}</span></div>`;
    }).join("");
    return `
      <div class="card">
        <div class="title">${escapeHtml(s.pool)}</div>
        <div class="stat-line">${wrap(escapeHtml(scanLine), scanCls)}</div>
        ${vdevRows}
      </div>`;
  }).join("");
}

function renderSmartUpdated(lastSmartUpdate, serverNow) {
  const el = document.getElementById("smart-last-updated");
  if (!lastSmartUpdate) { el.textContent = ""; return; }
  // Compare against the server's clock, not the browser's — clock skew
  // between the host and the viewing machine misreports the age.
  const nowSec = serverNow || (Date.now() / 1000);
  const mins = Math.round((nowSec - lastSmartUpdate) / 60);
  el.textContent = mins < 1 ? "(just now)" : `(last checked ${mins} min ago)`;
}

function renderSmart(devices) {
  const tbody = document.querySelector("#smart-table tbody");
  const labelFor = makeDeviceLabeler();
  tbody.innerHTML = devices.map(d => {
    const basename = d.device.split("/").pop();
    // Same labeling as the pool "more info" panel: by-id names carry a
    // serial that isn't meaningful at a glance (and two drives of the
    // same model look near-identical), so a stable "NVMe N" label plus
    // the model name underneath reads far better than the raw by-id
    // path. Full path is still one hover away.
    const label = escapeHtml(labelFor(basename));
    const modelStr = d.model ? escapeHtml(d.model) : "";
    const fullPath = escapeHtml(d.device);
    const health = escapeHtml(d.health);
    const tempCell = d.temperature_c === null ? "—" : wrap(d.temperature_c, tempClass(d.temperature_c));
    const hoursCell = d.power_on_hours === null ? "—" : wrap(d.power_on_hours, powerOnHoursClass(d.power_on_hours));
    const reallocCell = d.reallocated_sectors === null ? "—" : wrap(d.reallocated_sectors, reallocatedClass(d.reallocated_sectors));
    // NVMe-only fields (null on ATA/SATA drives — the reallocated-sectors
    // column is the mirror-image case, null on NVMe).
    const wearCell = (d.nvme_wear_pct === null || d.nvme_wear_pct === undefined)
      ? "—" : wrap(d.nvme_wear_pct + "%", nvmeWearClass(d.nvme_wear_pct));
    const mediaErrCell = (d.nvme_media_errors === null || d.nvme_media_errors === undefined)
      ? "—" : wrap(d.nvme_media_errors, mediaErrorsClass(d.nvme_media_errors));
    const writtenCell = (d.nvme_data_written_bytes === null || d.nvme_data_written_bytes === undefined)
      ? "—" : bytesToHuman(d.nvme_data_written_bytes);
    return `
      <tr>
        <td>
          <div class="device-cell" title="${fullPath}">
            <span class="device-label">${label}</span>
            ${modelStr ? `<span class="device-model">${modelStr}</span>` : ""}
          </div>
        </td>
        <td><span class="badge ${health}">${health}</span></td>
        <td>${tempCell}</td>
        <td>${hoursCell}</td>
        <td>${reallocCell}</td>
        <td>${wearCell}</td>
        <td>${mediaErrCell}</td>
        <td>${writtenCell}</td>
        <td><button class="smart-btn" data-device="${fullPath}">S.M.A.R.T.</button></td>
      </tr>`;
  }).join("");
}

// --- Theme --------------------------------------------------------------
// Dark stays the default look, but the OS preference is honoured until
// the user makes an explicit choice, after which that choice sticks.
const THEME_KEY = "deeppool-theme";

// Pure so the precedence rule (explicit choice > OS setting > dark) is
// testable. Mirrored by the inline no-flash script in index.html —
// change both together.
function resolveTheme(stored, prefersDark) {
  if (stored === "light" || stored === "dark") return stored;
  return prefersDark ? "dark" : "light";
}

function otherTheme(theme) {
  return theme === "dark" ? "light" : "dark";
}

// The button advertises what you'd switch *to*, not what you're on.
function themeToggleLabel(theme) {
  return theme === "dark" ? "Light mode" : "Dark mode";
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

// Chart.js takes concrete colour values, not CSS variables, so they're
// read out of the stylesheet at render time and re-read on every theme
// change.
function chartColors() {
  if (typeof getComputedStyle === "undefined") {
    return { grid: "#2a2f3a", tick: "#8b93a3", label: "#e6e9ef" };
  }
  const s = getComputedStyle(document.documentElement);
  return {
    grid: s.getPropertyValue("--chart-grid").trim() || "#2a2f3a",
    tick: s.getPropertyValue("--chart-tick").trim() || "#8b93a3",
    label: s.getPropertyValue("--chart-label").trim() || "#e6e9ef",
  };
}

// Every live Chart instance, so a theme switch can restyle them all
// without re-creating them (which would drop the live rolling buffers).
function allCharts() {
  const list = [];
  Object.values(poolCharts).forEach(c => list.push(c.bw, c.iops));
  Object.values(historyPoolCharts).forEach(c => list.push(c.bw, c.iops));
  [historyCapacityChart, historyArcChart, historyTempChart].forEach(c => {
    if (c) list.push(c);
  });
  return list.filter(Boolean);
}

function applyChartTheme() {
  const c = chartColors();
  allCharts().forEach(chart => {
    const o = chart.options;
    if (o.scales) {
      if (o.scales.x) {
        o.scales.x.ticks.color = c.tick;
        o.scales.x.grid.color = c.grid;
      }
      if (o.scales.y) {
        o.scales.y.ticks.color = c.tick;
        o.scales.y.grid.color = c.grid;
      }
    }
    if (o.plugins) {
      if (o.plugins.legend) o.plugins.legend.labels.color = c.label;
      if (o.plugins.title) o.plugins.title.color = c.label;
    }
    chart.update();
  });
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = themeToggleLabel(theme);
  applyChartTheme();
}

// --- Section navigation -------------------------------------------------
// Which nav link to highlight for a given scroll position. Pure (takes
// measured tops, returns an id) so the selection rule is testable
// without a DOM.
//
// `offset` is the height of the sticky header + nav: a section counts as
// "current" once its top passes under those bars, not when it reaches
// the true viewport top, otherwise the highlight lags by a bar's height.
function activeSectionId(sections, scrollY, offset, viewportH, docH) {
  if (!sections || !sections.length) return null;

  // At the very bottom of the page the last section may be too short to
  // ever reach the offset line — without this, the final nav item could
  // never highlight no matter how far you scroll.
  if (docH && viewportH && scrollY + viewportH >= docH - 2) {
    return sections[sections.length - 1].id;
  }

  const line = scrollY + offset + 1;
  let current = sections[0].id;
  for (const s of sections) {
    if (s.top <= line) current = s.id;
    else break;
  }
  return current;
}

// --- S.M.A.R.T. detail panel -------------------------------------------
// The dashboard table shows the handful of fields worth watching at a
// glance; this panel shows the drive's complete attribute set, keyed the
// way the underlying standard keys it (NVMe health-log byte offsets, or
// ATA attribute IDs). Fetched per drive on demand rather than shipped in
// the 5s /api/all payload.

// Column layouts differ between the two: ATA carries normalised
// value/worst/threshold triplets that NVMe simply doesn't have.
const SMART_COLUMNS = {
  nvme: [
    { key: "id", label: "Byte" },
    { key: "status", label: "Status" },
    { key: "label", label: "Description" },
    { key: "value", label: "Raw Data" },
  ],
  ata: [
    { key: "id", label: "ID" },
    { key: "status", label: "Status" },
    { key: "label", label: "Attribute" },
    { key: "normalized", label: "Value" },
    { key: "worst", label: "Worst" },
    { key: "threshold", label: "Thresh" },
    { key: "value", label: "Raw Data" },
  ],
};

let smartDetailState = null;  // {device, model, type, rows} of the open panel

function smartCellValue(row, key) {
  const v = row[key];
  return (v === null || v === undefined || v === "") ? "—" : v;
}

// CSV per RFC 4180: wrap in quotes and double any embedded quote. Kept
// pure (and exported) so the escaping is unit-testable — a raw SMART
// string containing a comma would otherwise silently shift columns.
//
// Values starting with = + @ (or - followed by a non-numeric) also get
// a leading apostrophe: spreadsheet apps execute such cells as formulas
// on open, and every string in this export ultimately comes from drive
// firmware — a hostile device's model string shouldn't become code on
// the machine that opens the CSV. A plain negative number like -3
// (a valid winter disk temperature) is left alone.
function toCsvValue(value) {
  if (value === null || value === undefined) return "";
  let s = String(value);
  // Leading minus is only safe when the entire value is a plain number
  // ("-3" is a winter disk temperature; "-2+3" is a formula).
  const dangerous = /^[=+@]/.test(s) || (s.startsWith("-") && !/^-\d+(\.\d+)?$/.test(s));
  if (dangerous) s = "'" + s;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function buildSmartCsv(columns, rows) {
  const header = columns.map(c => toCsvValue(c.label)).join(",");
  const body = rows.map(r =>
    columns.map(c => toCsvValue(r[c.key] === undefined ? "" : r[c.key])).join(",")
  );
  return [header, ...body].join("\r\n");
}

function csvFilename(device) {
  const base = String(device).split("/").pop() || "drive";
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  return `smart-${base}-${stamp}.csv`;
}

function renderSmartDetail(payload) {
  const type = (payload.details && payload.details.type) || "none";
  const rows = (payload.details && payload.details.rows) || [];
  const columns = SMART_COLUMNS[type] || SMART_COLUMNS.nvme;
  smartDetailState = { device: payload.device, columns, rows, type };

  const bits = [payload.model, payload.serial, payload.firmware].filter(Boolean);
  document.getElementById("smart-modal-subtitle").innerHTML =
    `${escapeHtml(payload.device)}${bits.length ? " · " + escapeHtml(bits.join(" · ")) : ""}`;

  document.getElementById("smart-detail-head").innerHTML =
    `<tr>${columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("")}</tr>`;

  const body = document.getElementById("smart-detail-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="${columns.length}">No SMART attributes reported for this device.</td></tr>`;
    return;
  }

  body.innerHTML = rows.map(r => {
    const cells = columns.map(c => {
      if (c.key === "status") {
        const s = escapeHtml(r.status || "good");
        return `<td><i class="dot dot-${s}" title="${s}"></i></td>`;
      }
      return `<td>${escapeHtml(smartCellValue(r, c.key))}</td>`;
    }).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
}

async function openSmartDetail(device) {
  const modal = document.getElementById("smart-modal");
  modal.hidden = false;
  document.getElementById("smart-detail-body").innerHTML =
    `<tr><td>Loading…</td></tr>`;
  try {
    const res = await fetch(`/api/smart/details?device=${encodeURIComponent(device)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderSmartDetail(await res.json());
  } catch (e) {
    smartDetailState = null;
    document.getElementById("smart-detail-body").innerHTML =
      `<tr><td>Couldn't load SMART detail — the first SMART poll may not have completed yet.</td></tr>`;
  }
}

function closeSmartDetail() {
  document.getElementById("smart-modal").hidden = true;
  smartDetailState = null;
}

function exportSmartCsv() {
  if (!smartDetailState) return;
  const csv = buildSmartCsv(smartDetailState.columns, smartDetailState.rows);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = csvFilename(smartDetailState.device);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const poolHistory = {};   // poolName -> {labels, bwRead, bwWrite, iopsRead, iopsWrite}
const poolCharts = {};    // poolName -> {bw: Chart, iops: Chart}

function sanitizeId(name) {
  return name.replace(/[^a-zA-Z0-9_-]/g, "_");
}

function chartOptions(titleText, yTickFormatter) {
  const theme = chartColors();
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: { ticks: { color: theme.tick, maxTicksLimit: 6 }, grid: { color: theme.grid } },
      y: {
        ticks: {
          color: theme.tick,
          callback: yTickFormatter || (v => v)
        },
        grid: { color: theme.grid }, beginAtZero: true
      }
    },
    plugins: {
      legend: { labels: { color: theme.label } },
      title: { display: true, text: titleText, color: theme.label }
    }
  };
}

function ensurePoolCharts(poolName) {
  if (poolCharts[poolName]) return;

  const id = sanitizeId(poolName);
  const container = document.getElementById("iostat-pools");

  const wrap = document.createElement("div");
  wrap.className = "pool-chart-group";
  wrap.innerHTML = `
    <div class="pool-chart-label">${escapeHtml(poolName)}</div>
    <div class="charts-row">
      <div class="chart-card"><canvas id="bw-chart-${id}"></canvas></div>
      <div class="chart-card"><canvas id="iops-chart-${id}"></canvas></div>
    </div>`;
  container.appendChild(wrap);

  const bwCtx = document.getElementById(`bw-chart-${id}`).getContext("2d");
  const iopsCtx = document.getElementById(`iops-chart-${id}`).getContext("2d");

  const bwChart = new Chart(bwCtx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Read (MB/s)", data: [], borderColor: "#4f8cff", tension: 0.3, pointRadius: 0 },
      { label: "Write (MB/s)", data: [], borderColor: "#e0a63c", tension: 0.3, pointRadius: 0 }
    ]},
    options: chartOptions("Throughput (MB/s)", v => v.toFixed(1))
  });

  const iopsChart = new Chart(iopsCtx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Read IOPS", data: [], borderColor: "#3ecf8e", tension: 0.3, pointRadius: 0 },
      { label: "Write IOPS", data: [], borderColor: "#e05c5c", tension: 0.3, pointRadius: 0 }
    ]},
    options: chartOptions("IOPS")
  });

  poolCharts[poolName] = { bw: bwChart, iops: iopsChart };
  poolHistory[poolName] = { labels: [], bwRead: [], bwWrite: [], iopsRead: [], iopsWrite: [] };
}

function updatePoolCharts(iostat) {
  const label = new Date().toLocaleTimeString();

  iostat.forEach(entry => {
    ensurePoolCharts(entry.pool);
    const hist = poolHistory[entry.pool];

    hist.labels.push(label);
    hist.bwRead.push(entry.read_bw_bytes / (1024 * 1024));
    hist.bwWrite.push(entry.write_bw_bytes / (1024 * 1024));
    hist.iopsRead.push(entry.read_iops);
    hist.iopsWrite.push(entry.write_iops);
    if (hist.labels.length > HISTORY_LEN) {
      hist.labels.shift(); hist.bwRead.shift(); hist.bwWrite.shift();
      hist.iopsRead.shift(); hist.iopsWrite.shift();
    }

    const charts = poolCharts[entry.pool];
    charts.bw.data.labels = hist.labels;
    charts.bw.data.datasets[0].data = hist.bwRead;
    charts.bw.data.datasets[1].data = hist.bwWrite;
    charts.bw.update();

    charts.iops.data.labels = hist.labels;
    charts.iops.data.datasets[0].data = hist.iopsRead;
    charts.iops.data.datasets[1].data = hist.iopsWrite;
    charts.iops.update();
  });
}

async function refresh() {
  try {
    const res = await fetch("/api/all");
    const data = await res.json();
    renderPools(data.pools, data.pool_disks);
    renderArc(data.arc);
    renderScrub(data.statuses);
    renderSmart(data.smart);
    renderSmartUpdated(data.last_smart_update, data.server_time);
    updatePoolCharts(data.iostat);
    // Label with the collector's freshness, not the fetch time — the
    // fetch succeeding only proves the web server is up, not that the
    // data behind it is current.
    const age = dataAgeSeconds(data.server_time, data.last_fast_update);
    const el = document.getElementById("last-updated");
    el.textContent = updatedLabel(new Date().toLocaleTimeString(), age);
    el.classList.toggle("stale-warning", age !== null && age > 30);
  } catch (e) {
    const el = document.getElementById("last-updated");
    el.textContent = "Update failed — check server";
    el.classList.add("stale-warning");
  }
}

// --- Historical charts --------------------------------------------------
// Separate refresh cadence from the live dashboard: history only changes
// as fast as the recorder writes samples (default once a minute), so
// polling it every 5s like the live charts would be pure waste.
const HISTORY_REFRESH_MS = 60000;
let currentRange = "24h";
let historyTimer = null;

// Auto-refresh cadence scaled to the range on screen. The server buckets
// every range into ~500 points, so a 30d view has ~86-minute buckets and
// a 12m view ~17-hour ones — re-running the query every minute would
// redraw pixel-identical charts while re-aggregating a large slice of
// the samples table each time (a year at 1-minute resolution is a few
// hundred thousand rows per pool). Switching range always refetches
// immediately; this only governs the idle polling.
const HISTORY_REFRESH_BY_RANGE = {
  "1h": 60000,
  "24h": 60000,
  "7d": 300000,
  "30d": 300000,
  "90d": 1800000,
  "180d": 1800000,
  "365d": 1800000,
};

function historyRefreshMs(range) {
  return HISTORY_REFRESH_BY_RANGE[range] || HISTORY_REFRESH_MS;
}

function scheduleHistoryRefresh() {
  if (historyTimer) clearInterval(historyTimer);
  historyTimer = setInterval(refreshHistory, historyRefreshMs(currentRange));
}

const historyPoolCharts = {};  // poolName -> {bw: Chart, iops: Chart}
let historyCapacityChart = null;
let historyArcChart = null;
let historyTempChart = null;

const colorMap = {};
const palette = ["#4f8cff", "#3ecf8e", "#e0a63c", "#e05c5c", "#a685e2", "#5fd0d0"];
function colorForKey(key) {
  if (!colorMap[key]) {
    colorMap[key] = palette[Object.keys(colorMap).length % palette.length];
  }
  return colorMap[key];
}

function formatHistTimestamp(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// The history API returns one independent {timestamps, values} series per
// pool and per device. Those series are NOT guaranteed to share a
// timeline: a pool created last week, a disk swapped in yesterday, or a
// gap from host downtime all produce series of different lengths and
// different start points. Chart.js plots a dataset's array positionally
// against the shared category axis, so taking the x-axis labels from one
// representative series and plotting the others by index silently shifts
// every mismatched series onto the wrong timestamps.
//
// unionTimestamps() builds the merged, sorted, de-duplicated axis across
// every series; alignSeries() re-indexes one series onto that axis,
// filling missing buckets with null (drawn as a gap, or bridged when
// spanGaps is on) rather than shifting later values backwards.
function unionTimestamps(seriesTimestamps) {
  const all = new Set();
  seriesTimestamps.forEach(list => (list || []).forEach(ts => all.add(ts)));
  return Array.from(all).sort((a, b) => a - b);
}

function alignSeries(axis, timestamps, values) {
  const byTs = new Map();
  (timestamps || []).forEach((ts, i) => byTs.set(ts, (values || [])[i]));
  return axis.map(ts => (byTs.has(ts) ? byTs.get(ts) : null));
}

// Plain kernel device names (sda1, nvme0n1p2, ...) are already short
// and physically meaningful — used as-is. by-id names (e.g.
// "nvme-CT1000T500SSD8_0000000000A1") carry a serial that isn't
// meaningful to a human, and truncating it to a handful of characters
// (the old behaviour) just reads as clipped, cut-off text — especially
// when two disks of the same model differ only in that tail. Use a
// stable "NVMe N" / "Disk N" positional label instead; the full id is
// still available via a title tooltip on hover (disk rows) or the
// model name shown alongside it.
// Returns a labeling function scoped to one render pass, numbering
// by-id devices per bus type (NVMe 1, NVMe 2, Disk 1, ...) rather than
// by raw array position — devices are sorted by device-path string
// server-side, so a plain positional index would misnumber whenever an
// NVMe and a SATA by-id path don't happen to sort contiguously.
function makeDeviceLabeler() {
  const counts = {};
  return function labelFor(basename) {
    if (!basename.includes("_")) return basename;
    const bus = basename.toLowerCase().startsWith("nvme") ? "NVMe" : "Disk";
    counts[bus] = (counts[bus] || 0) + 1;
    return `${bus} ${counts[bus]}`;
  };
}

function ensureHistoryCapacityChart() {
  if (historyCapacityChart) return;
  const ctx = document.getElementById("history-capacity-chart").getContext("2d");
  historyCapacityChart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [] },
    options: chartOptions("Capacity (%)", v => v.toFixed(0))
  });
}

function ensureHistoryArcChart() {
  if (historyArcChart) return;
  const ctx = document.getElementById("history-arc-chart").getContext("2d");
  historyArcChart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Hit Ratio (%)", data: [], borderColor: "#3ecf8e", tension: 0.3, pointRadius: 0 }
    ]},
    options: chartOptions("ARC Hit Ratio (%)", v => v.toFixed(0))
  });
}

function ensureHistoryTempChart() {
  if (historyTempChart) return;
  const ctx = document.getElementById("history-temp-chart").getContext("2d");
  historyTempChart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [] },
    options: chartOptions("Disk Temperature (°C)", v => v.toFixed(0))
  });
}

function ensureHistoryPoolCharts(poolName) {
  if (historyPoolCharts[poolName]) return;

  const id = sanitizeId(poolName);
  const container = document.getElementById("history-iostat-pools");

  const wrap = document.createElement("div");
  wrap.className = "pool-chart-group";
  wrap.innerHTML = `
    <div class="pool-chart-label">${escapeHtml(poolName)}</div>
    <div class="charts-row">
      <div class="chart-card"><canvas id="hist-bw-chart-${id}"></canvas></div>
      <div class="chart-card"><canvas id="hist-iops-chart-${id}"></canvas></div>
    </div>`;
  container.appendChild(wrap);

  const bwCtx = document.getElementById(`hist-bw-chart-${id}`).getContext("2d");
  const iopsCtx = document.getElementById(`hist-iops-chart-${id}`).getContext("2d");

  const bwChart = new Chart(bwCtx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Read (MB/s)", data: [], borderColor: "#4f8cff", tension: 0.3, pointRadius: 0 },
      { label: "Write (MB/s)", data: [], borderColor: "#e0a63c", tension: 0.3, pointRadius: 0 }
    ]},
    options: chartOptions("Throughput (MB/s)", v => v.toFixed(1))
  });

  const iopsChart = new Chart(iopsCtx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Read IOPS", data: [], borderColor: "#3ecf8e", tension: 0.3, pointRadius: 0 },
      { label: "Write IOPS", data: [], borderColor: "#e05c5c", tension: 0.3, pointRadius: 0 }
    ]},
    options: chartOptions("IOPS")
  });

  historyPoolCharts[poolName] = { bw: bwChart, iops: iopsChart };
}

async function refreshHistory() {
  try {
    const res = await fetch(`/api/history?range=${currentRange}`);
    const data = await res.json();

    ensureHistoryCapacityChart();
    const poolNames = Object.keys(data.pools);
    // Align every pool onto a shared timeline rather than borrowing one
    // series' labels for all of them — see alignSeries().
    const capAxis = unionTimestamps(poolNames.map(n => data.pools[n].timestamps));
    historyCapacityChart.data.labels = capAxis.map(formatHistTimestamp);
    historyCapacityChart.data.datasets = poolNames.map(name => ({
      label: name,
      data: alignSeries(capAxis, data.pools[name].timestamps, data.pools[name].capacity_pct),
      borderColor: colorForKey(name),
      tension: 0.3,
      pointRadius: 0,
      spanGaps: true
    }));
    historyCapacityChart.update();

    poolNames.forEach(name => {
      ensureHistoryPoolCharts(name);
      const p = data.pools[name];
      const labels = p.timestamps.map(formatHistTimestamp);
      const charts = historyPoolCharts[name];

      charts.bw.data.labels = labels;
      charts.bw.data.datasets[0].data = p.read_bw_bytes.map(v => v === null ? null : v / (1024 * 1024));
      charts.bw.data.datasets[1].data = p.write_bw_bytes.map(v => v === null ? null : v / (1024 * 1024));
      charts.bw.update();

      charts.iops.data.labels = labels;
      charts.iops.data.datasets[0].data = p.read_iops;
      charts.iops.data.datasets[1].data = p.write_iops;
      charts.iops.update();
    });

    ensureHistoryArcChart();
    historyArcChart.data.labels = data.arc.timestamps.map(formatHistTimestamp);
    historyArcChart.data.datasets[0].data = data.arc.hit_ratio_pct;
    historyArcChart.update();

    ensureHistoryTempChart();
    const devices = Object.keys(data.smart);
    // Disks are the most likely series to have differing history depths
    // (a drive swapped in later has no samples before that point), so
    // the shared-timeline alignment matters most here.
    const tempAxis = unionTimestamps(devices.map(d => data.smart[d].timestamps));
    historyTempChart.data.labels = tempAxis.map(formatHistTimestamp);
    const tempLabelFor = makeDeviceLabeler();
    historyTempChart.data.datasets = devices.map(dev => ({
      label: tempLabelFor(dev.split("/").pop()),
      data: alignSeries(tempAxis, data.smart[dev].timestamps, data.smart[dev].temperature_c),
      borderColor: colorForKey(dev),
      tension: 0.3,
      pointRadius: 0,
      spanGaps: true
    }));
    historyTempChart.update();
  } catch (e) {
    // History is supplementary — a failed fetch shouldn't disrupt the live dashboard.
    console.error("history refresh failed", e);
  }
}

// Bootstrap only in a browser. Guarded so this file can also be loaded
// by the Node test runner (tests/app.test.js), which exercises the pure
// helpers below without a DOM — importing it must not start timers or
// touch document.
if (typeof document !== "undefined") {
  // Theme. The inline script in <head> has already applied the theme to
  // avoid a flash; this only syncs the button label and wires the click.
  (function setupTheme() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.textContent = themeToggleLabel(currentTheme());

    btn.addEventListener("click", () => {
      const next = otherTheme(currentTheme());
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        // Private browsing or storage disabled — the theme still
        // switches, it just won't persist across reloads.
      }
      setTheme(next);
    });

    // Follow the OS if the user hasn't made an explicit choice.
    if (window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = (e) => {
        let stored = null;
        try { stored = localStorage.getItem(THEME_KEY); } catch (err) { /* ignore */ }
        if (stored !== "light" && stored !== "dark") {
          setTheme(resolveTheme(null, e.matches));
        }
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  })();

  // Section nav: measure the sticky bars once so anchor offsets match
  // what's actually rendered (font/zoom differences change their height),
  // then highlight the current section on scroll.
  (function setupSectionNav() {
    const nav = document.getElementById("section-nav");
    const header = document.querySelector("header");
    if (!nav || !header) return;

    function syncBarHeights() {
      const h = Math.round(header.getBoundingClientRect().height);
      const n = Math.round(nav.getBoundingClientRect().height);
      document.documentElement.style.setProperty("--header-h", `${h}px`);
      document.documentElement.style.setProperty("--nav-h", `${n}px`);
      return h + n;
    }

    let offset = syncBarHeights();
    const links = Array.from(nav.querySelectorAll("a[href^='#']"));
    const targets = links
      .map(a => ({ id: a.getAttribute("href").slice(1), link: a }))
      .filter(t => document.getElementById(t.id));

    let ticking = false;
    function updateActive() {
      ticking = false;
      const sections = targets.map(t => ({
        id: t.id,
        top: document.getElementById(t.id).getBoundingClientRect().top + window.scrollY,
      }));
      const active = activeSectionId(
        sections, window.scrollY, offset,
        window.innerHeight, document.documentElement.scrollHeight
      );
      targets.forEach(t => t.link.classList.toggle("active", t.id === active));
    }

    window.addEventListener("scroll", () => {
      // Coalesce scroll events into one update per frame.
      if (!ticking) { ticking = true; requestAnimationFrame(updateActive); }
    }, { passive: true });

    window.addEventListener("resize", () => {
      offset = syncBarHeights();
      updateActive();
    });

    updateActive();
  })();

  document.getElementById("history-range-picker").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (!btn) return;
    document.querySelectorAll("#history-range-picker button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentRange = btn.dataset.range;
    refreshHistory();
    // Re-arm the timer at the cadence appropriate to the new range.
    scheduleHistoryRefresh();
  });

  // Delegated: the SMART table is re-rendered every 5s, so per-button
  // listeners would be re-bound constantly (and lost on each refresh).
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".smart-btn");
    if (btn) openSmartDetail(btn.dataset.device);
  });

  document.getElementById("smart-modal-close").addEventListener("click", closeSmartDetail);
  document.getElementById("smart-export-btn").addEventListener("click", exportSmartCsv);

  // Click the backdrop (but not the dialog itself) to dismiss.
  document.getElementById("smart-modal").addEventListener("click", (e) => {
    if (e.target.id === "smart-modal") closeSmartDetail();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("smart-modal").hidden) {
      closeSmartDetail();
    }
  });

  refresh();
  setInterval(refresh, REFRESH_MS);
  refreshHistory();
  scheduleHistoryRefresh();
}

// Exported for tests only; ignored by the browser (no module global).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    escapeHtml, parseZfsCount, bytesToHuman, countToHuman,
    capacityClass, fragClass, hitRatioClass, tempClass,
    powerOnHoursClass, reallocatedClass, errorCountClass,
    nvmeWearClass, mediaErrorsClass, scanErrorClass,
    makeDeviceLabeler, sanitizeId, unionTimestamps, alignSeries,
    toCsvValue, buildSmartCsv, csvFilename, SMART_COLUMNS,
    historyRefreshMs, HISTORY_REFRESH_BY_RANGE,
    activeSectionId,
    resolveTheme, otherTheme, themeToggleLabel, THEME_KEY,
    dataAgeSeconds, updatedLabel,
  };
}
