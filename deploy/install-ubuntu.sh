#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root: sudo deploy/install-ubuntu.sh" >&2
  exit 1
fi
if [[ ! -f /etc/os-release ]] || ! grep -qi '^ID=ubuntu' /etc/os-release; then
  echo "This installer supports Ubuntu only." >&2
  exit 1
fi

APP_DIR="/opt/cbrs"
PYTHON_BIN="${CBRS_PYTHON_BIN:-python3.14}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg software-properties-common restic xvfb x11vnc novnc websockify

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update
  apt-get install -y python3.14 python3.14-venv
fi

if ! command -v google-chrome-stable >/dev/null 2>&1; then
  install -d -m 0755 /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/google-chrome.gpg ]]; then
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
  fi
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update
  apt-get install -y google-chrome-stable
fi

if ! getent group cbrs >/dev/null; then
  groupadd --system cbrs
fi
if ! id cbrs >/dev/null 2>&1; then
  useradd --system --gid cbrs --home-dir /var/lib/cbrs --shell /usr/sbin/nologin cbrs
fi

install -d -o cbrs -g cbrs -m 0750 /var/lib/cbrs /var/lib/cbrs/outputs /var/log/cbrs /srv/cbrs-backup
install -d -o root -g cbrs -m 0750 /etc/cbrs
install -d -o root -g root -m 0755 "${APP_DIR}"

if [[ "${SOURCE_DIR}" != "${APP_DIR}" ]]; then
  rsync_args=(
    -a
    --exclude .git
    --exclude .cbrs
    --exclude .env
    --exclude .env.local
    --exclude outputs
    --exclude .venv
    --exclude .pytest_cache
    --exclude .pytest_tmp
    --exclude __pycache__
  )
  apt-get install -y rsync
  rsync "${rsync_args[@]}" "${SOURCE_DIR}/" "${APP_DIR}/"
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"
"${APP_DIR}/.venv/bin/python" -m playwright install-deps chromium

if [[ ! -f /etc/cbrs/cbrs.env ]]; then
  install -o root -g cbrs -m 0640 "${APP_DIR}/deploy/cbrs.env.example" /etc/cbrs/cbrs.env
fi
if [[ ! -f /var/lib/cbrs/account-pool.json ]]; then
  install -o cbrs -g cbrs -m 0640 \
    "${APP_DIR}/deploy/account-pool.json.example" /var/lib/cbrs/account-pool.json
fi

for unit in cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service \
            cbrs-worker.service cbrs-dashboard.service cbrs-backup.service \
            cbrs-backup.timer; do
  install -o root -g root -m 0644 "${APP_DIR}/deploy/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload

echo "Ubuntu runtime installed. Complete /etc/cbrs/cbrs.env and"
echo "/var/lib/cbrs/account-pool.json, run the documented preflight, then enable services."
