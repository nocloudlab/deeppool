// Logic tests for the frontend helpers in static/app.js.
// Run with: node --test tests/
//
// app.js guards its bootstrap block behind `typeof document !== "undefined"`,
// so requiring it here is side-effect free — no timers, no fetches, no DOM.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const app = require(path.join(__dirname, "..", "static", "app.js"));

// ------------------------------------------------------------- escaping

test("escapeHtml neutralizes markup in CLI-derived strings", () => {
  assert.strictEqual(
    app.escapeHtml('<img src=x onerror="alert(1)">'),
    "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
  );
  assert.strictEqual(app.escapeHtml("tank&rpool"), "tank&amp;rpool");
  assert.strictEqual(app.escapeHtml("it's"), "it&#39;s");
});

test("escapeHtml leaves ordinary pool names untouched", () => {
  assert.strictEqual(app.escapeHtml("rpool"), "rpool");
  assert.strictEqual(app.escapeHtml("/dev/sda1"), "/dev/sda1");
});

// -------------------------------------------------------- count parsing

test("parseZfsCount handles zpool's abbreviated error counts", () => {
  assert.strictEqual(app.parseZfsCount("0"), 0);
  assert.strictEqual(app.parseZfsCount("7"), 7);
  // The bug this guards: parseInt("1.2K") returns 1, understating by 1200x.
  assert.strictEqual(app.parseZfsCount("1.2K"), 1200);
  assert.strictEqual(app.parseZfsCount("3M"), 3000000);
  assert.strictEqual(app.parseZfsCount("2.5G"), 2500000000);
});

test("parseZfsCount treats unparseable values as zero", () => {
  assert.strictEqual(app.parseZfsCount("-"), 0);
  assert.strictEqual(app.parseZfsCount(""), 0);
  assert.strictEqual(app.parseZfsCount("garbage"), 0);
});

// ----------------------------------------------------------- formatting

test("bytesToHuman scales into sensible units", () => {
  assert.strictEqual(app.bytesToHuman(0), "0.0 B");
  assert.strictEqual(app.bytesToHuman(1024), "1.0 KB");
  assert.strictEqual(app.bytesToHuman(1024 ** 4), "1.0 TB");
  assert.strictEqual(app.bytesToHuman(null), "—");
  assert.strictEqual(app.bytesToHuman(undefined), "—");
});

test("countToHuman compacts large hit/miss counts", () => {
  assert.strictEqual(app.countToHuman(999), "999");
  assert.strictEqual(app.countToHuman(1500), "1.5K");
  assert.strictEqual(app.countToHuman(null), "—");
});

// ----------------------------------------------------------- thresholds

test("capacity thresholds escalate green -> amber -> red", () => {
  assert.strictEqual(app.capacityClass(10), "good");
  assert.strictEqual(app.capacityClass(80), "warn");
  assert.strictEqual(app.capacityClass(95), "bad");
});

test("hit ratio thresholds run in reverse (higher is better)", () => {
  assert.strictEqual(app.hitRatioClass(99), "good");
  assert.strictEqual(app.hitRatioClass(85), "warn");
  assert.strictEqual(app.hitRatioClass(50), "bad");
});

test("NVMe wear thresholds, including past-rated-life values", () => {
  assert.strictEqual(app.nvmeWearClass(0), "good");
  assert.strictEqual(app.nvmeWearClass(75), "warn");
  assert.strictEqual(app.nvmeWearClass(95), "bad");
  // The NVMe spec permits percentage_used > 100 on a drive that has
  // outlived its rated endurance but still works.
  assert.strictEqual(app.nvmeWearClass(140), "bad");
  // Null (an ATA drive) has no verdict at all, rather than a false green.
  assert.strictEqual(app.nvmeWearClass(null), "");
  assert.strictEqual(app.nvmeWearClass(undefined), "");
});

test("media errors are binary, not a gradient", () => {
  assert.strictEqual(app.mediaErrorsClass(0), "good");
  assert.strictEqual(app.mediaErrorsClass(1), "bad");
  assert.strictEqual(app.mediaErrorsClass(999), "bad");
  assert.strictEqual(app.mediaErrorsClass(null), "");
});

test("temperature and power-on-hours tolerate missing values", () => {
  assert.strictEqual(app.tempClass(30), "good");
  assert.strictEqual(app.tempClass(45), "warn");
  assert.strictEqual(app.tempClass(55), "bad");
  assert.strictEqual(app.tempClass(null), "");
  assert.strictEqual(app.powerOnHoursClass(null), "");
  assert.strictEqual(app.powerOnHoursClass(1000), "good");
});

test("scanErrorClass flags a scrub that finished with errors", () => {
  assert.strictEqual(
    app.scanErrorClass("scrub repaired 0B in 08:15:23 with 0 errors on Sun Jul 13"),
    "good"
  );
  assert.strictEqual(
    app.scanErrorClass("scrub repaired 16K in 08:15:23 with 3 errors on Sun Jul 13"),
    "bad"
  );
  // No error phrase at all (e.g. "none requested") gets no verdict.
  assert.strictEqual(app.scanErrorClass("none requested"), "");
});

// -------------------------------------------------------- device labels

test("device labeler numbers by bus type, not array position", () => {
  const labelFor = app.makeDeviceLabeler();
  // Interleaved buses: a purely positional index would produce
  // "NVMe 1", "Disk 2", "NVMe 3" — wrong on both counts.
  assert.strictEqual(labelFor("nvme-CT1000T500SSD8_25395334B282"), "NVMe 1");
  assert.strictEqual(labelFor("ata-ST8000VN004_ZR10ABCD"), "Disk 1");
  assert.strictEqual(labelFor("nvme-CT1000T500SSD8_25395337E3C7"), "NVMe 2");
  assert.strictEqual(labelFor("ata-ST8000VN004_ZR10EFGH"), "Disk 2");
});

test("plain kernel device names pass through unchanged", () => {
  const labelFor = app.makeDeviceLabeler();
  assert.strictEqual(labelFor("sda1"), "sda1");
  assert.strictEqual(labelFor("nvme0n1p2"), "nvme0n1p2");
});

test("each labeler instance numbers independently", () => {
  const a = app.makeDeviceLabeler();
  const b = app.makeDeviceLabeler();
  assert.strictEqual(a("nvme-X_1"), "NVMe 1");
  assert.strictEqual(b("nvme-Y_2"), "NVMe 1");
});

test("sanitizeId strips characters unsafe for element ids", () => {
  assert.strictEqual(app.sanitizeId("tank/data"), "tank_data");
  assert.strictEqual(app.sanitizeId("rpool"), "rpool");
});

// ------------------------------------------------- history alignment (#1)

test("unionTimestamps merges, sorts and de-duplicates series axes", () => {
  const axis = app.unionTimestamps([[30, 10, 20], [20, 40], []]);
  assert.deepStrictEqual(axis, [10, 20, 30, 40]);
});

test("unionTimestamps tolerates missing/empty series", () => {
  assert.deepStrictEqual(app.unionTimestamps([]), []);
  assert.deepStrictEqual(app.unionTimestamps([null, undefined, [5]]), [5]);
});

test("alignSeries places values at their own timestamps, not by index", () => {
  // The regression this locks in: a pool added later starts at ts 30.
  // Plotting its array positionally against a shared axis starting at
  // ts 10 would draw its first value three buckets too early.
  const axis = [10, 20, 30, 40];
  const aligned = app.alignSeries(axis, [30, 40], [77, 88]);
  assert.deepStrictEqual(aligned, [null, null, 77, 88]);
});

test("alignSeries fills interior gaps with null", () => {
  const axis = [10, 20, 30];
  // A disk that missed the middle bucket (host downtime).
  assert.deepStrictEqual(app.alignSeries(axis, [10, 30], [1, 3]), [1, null, 3]);
});

test("alignSeries handles a full-length series unchanged", () => {
  const axis = [10, 20, 30];
  assert.deepStrictEqual(app.alignSeries(axis, [10, 20, 30], [1, 2, 3]), [1, 2, 3]);
});

test("alignSeries ignores samples outside the shared axis", () => {
  const axis = [20, 30];
  assert.deepStrictEqual(app.alignSeries(axis, [10, 20, 30], [1, 2, 3]), [2, 3]);
});

test("alignSeries tolerates null/undefined inputs", () => {
  assert.deepStrictEqual(app.alignSeries([10, 20], null, null), [null, null]);
});

// ------------------------------------------------------ CSV export

test("toCsvValue leaves plain values unquoted", () => {
  assert.strictEqual(app.toCsvValue("Power On Hours"), "Power On Hours");
  assert.strictEqual(app.toCsvValue(11849), "11849");
  assert.strictEqual(app.toCsvValue("143:128"), "143:128");
});

test("toCsvValue quotes and escapes per RFC 4180", () => {
  // A raw SMART string like "35 (Min/Max 20/45)" is fine, but ATA raw
  // strings can contain commas — unquoted, they'd shift every later
  // column in the exported file.
  assert.strictEqual(app.toCsvValue("35 (Min, Max)"), '"35 (Min, Max)"');
  assert.strictEqual(app.toCsvValue('say "hi"'), '"say ""hi"""');
  assert.strictEqual(app.toCsvValue("line1\nline2"), '"line1\nline2"');
});

test("toCsvValue renders null and undefined as empty", () => {
  assert.strictEqual(app.toCsvValue(null), "");
  assert.strictEqual(app.toCsvValue(undefined), "");
});

test("buildSmartCsv emits a header plus one line per row", () => {
  const csv = app.buildSmartCsv(app.SMART_COLUMNS.nvme, [
    { id: "0", status: "good", label: "Critical Warning", value: 0 },
    { id: "5", status: "fair", label: "Percentage Used (%)", value: 75 },
  ]);
  const lines = csv.split("\r\n");
  assert.strictEqual(lines[0], "Byte,Status,Description,Raw Data");
  assert.strictEqual(lines[1], "0,good,Critical Warning,0");
  assert.strictEqual(lines[2], "5,fair,Percentage Used (%),75");
  assert.strictEqual(lines.length, 3);
});

test("buildSmartCsv fills missing ATA columns rather than misaligning", () => {
  const csv = app.buildSmartCsv(app.SMART_COLUMNS.ata, [
    { id: 5, status: "good", label: "Reallocated Sector Ct", value: "0" },
  ]);
  const cells = csv.split("\r\n")[1].split(",");
  // 7 ATA columns; normalized/worst/threshold absent here but still
  // occupy their positions.
  assert.strictEqual(cells.length, app.SMART_COLUMNS.ata.length);
  assert.strictEqual(cells[0], "5");
  assert.strictEqual(cells[6], "0");
});

test("buildSmartCsv handles an empty row set", () => {
  const csv = app.buildSmartCsv(app.SMART_COLUMNS.nvme, []);
  assert.strictEqual(csv, "Byte,Status,Description,Raw Data");
});

test("csvFilename is derived from the device basename", () => {
  const name = app.csvFilename("/dev/disk/by-id/nvme-CT1000T500SSD8_2539");
  assert.match(name, /^smart-nvme-CT1000T500SSD8_2539-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.csv$/);
  assert.match(app.csvFilename("/dev/sda"), /^smart-sda-/);
});

// ------------------------------------------- history refresh cadence

test("history refresh cadence scales with the range on screen", () => {
  // Short ranges keep the 1-minute cadence (the recorder's own interval).
  assert.strictEqual(app.historyRefreshMs("1h"), 60000);
  assert.strictEqual(app.historyRefreshMs("24h"), 60000);
  // Long ranges back off: a 12m view buckets into ~17h points, so a
  // per-minute refetch would redraw identical charts while re-aggregating
  // a large slice of the samples table.
  assert.ok(app.historyRefreshMs("30d") > app.historyRefreshMs("24h"));
  assert.ok(app.historyRefreshMs("365d") > app.historyRefreshMs("30d"));
});

test("history refresh cadence is defined for every picker range", () => {
  for (const range of ["1h", "24h", "7d", "30d", "90d", "180d", "365d"]) {
    assert.strictEqual(typeof app.HISTORY_REFRESH_BY_RANGE[range], "number", range);
  }
});

test("history refresh cadence falls back for an unknown range", () => {
  assert.strictEqual(app.historyRefreshMs("bogus"), 60000);
});

// --------------------------------------------------- section scroll-spy

const NAV_SECTIONS = [
  { id: "pools-section", top: 0 },
  { id: "iostat-section", top: 600 },
  { id: "history-section", top: 1200 },
  { id: "history-arc", top: 2000 },
  { id: "history-temp", top: 2400 },
  { id: "arc-section", top: 3000 },
  { id: "scrub-section", top: 3400 },
  { id: "smart-section", top: 3800 },
];

test("scroll-spy picks the section under the sticky bars", () => {
  const offset = 112;
  // Just past the I/O section's top, accounting for the bars.
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 600 - offset + 5, offset, 800, 4400),
    "iostat-section"
  );
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 3000 - offset + 5, offset, 800, 4400),
    "arc-section"
  );
});

test("scroll-spy highlights the first section at the top of the page", () => {
  assert.strictEqual(app.activeSectionId(NAV_SECTIONS, 0, 112, 800, 4400), "pools-section");
});

test("scroll-spy stays on a section until the next one arrives", () => {
  const offset = 112;
  // Between history-section and history-arc.
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 1500, offset, 800, 4400),
    "history-section"
  );
});

test("scroll-spy resolves the historical sub-anchors", () => {
  const offset = 112;
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 2000 - offset + 2, offset, 800, 4400),
    "history-arc"
  );
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 2400 - offset + 2, offset, 800, 4400),
    "history-temp"
  );
});

test("scroll-spy highlights the last section at the bottom of the page", () => {
  // A short final section may never reach the offset line; hitting the
  // bottom of the document must still select it.
  assert.strictEqual(
    app.activeSectionId(NAV_SECTIONS, 3600, 112, 800, 4400),
    "smart-section"
  );
});

test("scroll-spy tolerates an empty or missing section list", () => {
  assert.strictEqual(app.activeSectionId([], 0, 112, 800, 4400), null);
  assert.strictEqual(app.activeSectionId(null, 0, 112, 800, 4400), null);
});

// ---------------------------------------------------------------- theme

test("explicit choice beats the OS preference", () => {
  assert.strictEqual(app.resolveTheme("light", true), "light");
  assert.strictEqual(app.resolveTheme("dark", false), "dark");
});

test("OS preference applies when nothing is stored", () => {
  assert.strictEqual(app.resolveTheme(null, true), "dark");
  assert.strictEqual(app.resolveTheme(null, false), "light");
});

test("a junk stored value falls back to the OS preference", () => {
  // e.g. a value left by an older build, or hand-edited storage.
  assert.strictEqual(app.resolveTheme("chartreuse", true), "dark");
  assert.strictEqual(app.resolveTheme("", false), "light");
  assert.strictEqual(app.resolveTheme(undefined, true), "dark");
});

test("toggling flips between the two themes", () => {
  assert.strictEqual(app.otherTheme("dark"), "light");
  assert.strictEqual(app.otherTheme("light"), "dark");
  assert.strictEqual(app.otherTheme(app.otherTheme("dark")), "dark");
});

test("the button advertises the theme you'd switch to", () => {
  // Sitting in dark mode, the button offers "Light mode".
  assert.strictEqual(app.themeToggleLabel("dark"), "Light mode");
  assert.strictEqual(app.themeToggleLabel("light"), "Dark mode");
});

// ------------------------------------------------------ data freshness

test("dataAgeSeconds derives age from server clock only", () => {
  assert.strictEqual(app.dataAgeSeconds(1000, 995), 5);
  assert.strictEqual(app.dataAgeSeconds(1000, 1000), 0);
  // Collector timestamp slightly ahead (thread wrote mid-request): clamp.
  assert.strictEqual(app.dataAgeSeconds(1000, 1001), 0);
  // Missing either value (first poll not done yet) -> no verdict.
  assert.strictEqual(app.dataAgeSeconds(null, 995), null);
  assert.strictEqual(app.dataAgeSeconds(1000, null), null);
});

test("updatedLabel shows normal timestamp while fresh", () => {
  assert.strictEqual(app.updatedLabel("10:15:00", 4), "Updated 10:15:00");
  assert.strictEqual(app.updatedLabel("10:15:00", 30), "Updated 10:15:00");
  // Unknown age (older server without server_time) degrades gracefully.
  assert.strictEqual(app.updatedLabel("10:15:00", null), "Updated 10:15:00");
});

test("updatedLabel flags a stalled collector instead of lying", () => {
  assert.strictEqual(app.updatedLabel("10:15:00", 90), "Collector stalled — data is 90s old");
  assert.strictEqual(app.updatedLabel("10:15:00", 600), "Collector stalled — data is 10 min old");
});

// ---------------------------------------------- CSV formula injection

test("toCsvValue neutralizes formula-leading characters", () => {
  // A hostile drive's model string must not execute when the CSV is
  // opened in a spreadsheet.
  assert.strictEqual(app.toCsvValue("=HYPERLINK(\"http://x\")"),
    "\"'=HYPERLINK(\"\"http://x\"\")\"");
  assert.strictEqual(app.toCsvValue("+SUM(A1)"), "'+SUM(A1)");
  assert.strictEqual(app.toCsvValue("@cmd"), "'@cmd");
  assert.strictEqual(app.toCsvValue("-2+3"), "'-2+3");
});

test("toCsvValue leaves negative numbers alone", () => {
  // -3 is a legitimate disk temperature, not a formula.
  assert.strictEqual(app.toCsvValue(-3), "-3");
  assert.strictEqual(app.toCsvValue("-3"), "-3");
  assert.strictEqual(app.toCsvValue("-0.5"), "-0.5");
});
