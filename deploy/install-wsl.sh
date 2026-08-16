#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${WSL_DISTRO_NAME:-}" ]]; then
  echo "Run this script inside an Ubuntu WSL2 distribution." >&2
  exit 1
fi

PYTHON_BIN="${CBRS_PYTHON_BIN:-python3.14}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg software-properties-common xvfb restic
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update
  sudo apt-get install -y python3.14 python3.14-venv
fi
if ! command -v google-chrome-stable >/dev/null 2>&1; then
  sudo install -d -m 0755 /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/google-chrome.gpg ]]; then
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor \
      | sudo tee /etc/apt/keyrings/google-chrome.gpg >/dev/null
  fi
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y google-chrome-stable
fi

if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv"
fi
"${ROOT_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${ROOT_DIR}/.venv/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"
"${ROOT_DIR}/.venv/bin/python" -m playwright install-deps chromium

# The dashboard is rendered by the native Windows browser in development so it
# can use the host monitor refresh rate. Ubuntu still owns every CBRS process.
# deploy/windows/Start-CbrsWslHidden.vbs is the login-only host bridge.
echo "WSL2 runtime ready. Install the Windows login bridge to open the native dashboard viewer."
