#!/bin/bash
# Run this directly on the host running your ZFS pool, as root. Works on
# any Debian-based distro (Debian, Ubuntu, Proxmox VE, ...) with ZFS
# already set up.
# Self-contained: copies the app files from wherever this script currently
# lives into /opt/zfs-monitor, then installs deps and starts the service.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# zfsutils-linux is already installed and version-matched to your kernel
# module — you can't have a pool without it — so it's deliberately NOT
# reinstalled/touched here. smartmontools and the Python tooling are
# unrelated to the ZFS kernel module, so installing them is safe and
# won't affect the pool.
apt-get update
apt-get install -y smartmontools python3-venv python3-pip curl

mkdir -p /opt/zfs-monitor
cp -r "$SCRIPT_DIR"/app.py "$SCRIPT_DIR"/templates "$SCRIPT_DIR"/static \
      "$SCRIPT_DIR"/requirements.txt "$SCRIPT_DIR"/zfs-monitor.service \
      /opt/zfs-monitor/

# Chart.js is fetched once here and served locally from then on — the
# running dashboard has no CDN dependency, only this one-time install
# step does (same as the apt-get install above already requires internet).
#
# The download is verified against a pinned SHA-256 in chartjs.sha256.
# TLS only protects the bytes in transit; it says nothing about whether
# the CDN served what we expect. Without a pin, a compromised or
# MITM'd response would be written into static/vendor/ and then served
# to every visitor of the dashboard from then on, with nothing to catch
# it. Verification is fail-closed: a mismatched download is deleted, not
# installed.
CHART_JS_URL="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
CHART_JS_DEST="/opt/zfs-monitor/static/vendor/chart.umd.min.js"
CHART_JS_PIN_FILE="$SCRIPT_DIR/chartjs.sha256"

mkdir -p /opt/zfs-monitor/static/vendor

verify_chartjs() {
  # $1 = file to check. Returns 0 if it matches the pin, 1 otherwise.
  local expected
  expected="$(grep -oE '^[a-f0-9]{64}' "$CHART_JS_PIN_FILE" 2>/dev/null || true)"
  if [[ -z "$expected" ]]; then
    echo "WARNING: no SHA-256 pin found in $CHART_JS_PIN_FILE." >&2
    echo "Chart.js will NOT be integrity-checked. To pin it, run:" >&2
    echo "  sha256sum '$1' | awk '{print \$1}' > '$CHART_JS_PIN_FILE'" >&2
    echo "after verifying the file came from a source you trust." >&2
    return 0
  fi
  local actual
  actual="$(sha256sum "$1" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: Chart.js SHA-256 mismatch — refusing to install it." >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    return 1
  fi
  echo "Chart.js integrity verified against pin."
  return 0
}

if [[ ! -f "$CHART_JS_DEST" ]]; then
  TMP_CHART="$(mktemp)"
  if curl -fsSL "$CHART_JS_URL" -o "$TMP_CHART"; then
    if verify_chartjs "$TMP_CHART"; then
      mv "$TMP_CHART" "$CHART_JS_DEST"
      echo "Downloaded Chart.js for local serving."
    else
      rm -f "$TMP_CHART"
      echo "Charts will not render until a verified copy is placed at" >&2
      echo "$CHART_JS_DEST." >&2
    fi
  else
    rm -f "$TMP_CHART"
    echo "WARNING: couldn't download Chart.js (no internet right now?)." >&2
    echo "The dashboard's charts won't render until you place a copy at" >&2
    echo "$CHART_JS_DEST — re-run this script once you have internet" >&2
    echo "access, or download it manually." >&2
  fi
else
  # Already installed: re-verify on every run so a tampered vendor file
  # is surfaced on the next upgrade rather than persisting silently.
  verify_chartjs "$CHART_JS_DEST" || true
fi

cd /opt/zfs-monitor

if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cp zfs-monitor.service /etc/systemd/system/zfs-monitor.service
systemctl daemon-reload
systemctl enable zfs-monitor
systemctl restart zfs-monitor

echo
echo "Done. Dashboard should be reachable at http://<host-ip>:8087"
systemctl status zfs-monitor --no-pager
