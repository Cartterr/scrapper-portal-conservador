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

PYTHON_BIN="${CBRS_PYTHON_BIN:-python3.14}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APP_DIR="${SOURCE_DIR}"
STATE_DIR="${APP_DIR}/.cbrs/runtime"

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
  useradd --system --gid cbrs --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin cbrs
fi

# Let the interactive WSL operator inspect repository-local state without
# running the whole console as root. A new shell is required after first install.
OPERATOR_USER="${SUDO_USER:-}"
if [[ -n "${OPERATOR_USER}" && "${OPERATOR_USER}" != "root" ]]; then
  usermod -a -G cbrs "${OPERATOR_USER}"
fi

install -d -o cbrs -g cbrs -m 0750 "${STATE_DIR}" "${STATE_DIR}"/outputs "${STATE_DIR}"/control "${STATE_DIR}/logs" "${STATE_DIR}/backup/restic"
install -d -o root -g cbrs -m 0750 "${STATE_DIR}/secrets"
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

if [[ ! -f "${APP_DIR}/.env" ]]; then
  install -o root -g cbrs -m 0640 "${APP_DIR}/deploy/cbrs.env.example" "${APP_DIR}/.env"
fi
if [[ ! -f "${STATE_DIR}"/account-pool.json ]]; then
  install -o cbrs -g cbrs -m 0640 \
    "${APP_DIR}/deploy/account-pool.json.example" "${STATE_DIR}"/account-pool.json
fi

"${APP_DIR}/.venv/bin/python" "${APP_DIR}/deploy/render_units.py"
systemctl daemon-reload

# Stable global entrypoint: callers can use `cbrs` from any WSL directory while
# Python imports and repository-relative configuration stay bound to this install.
cat > /usr/local/bin/cbrs <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd '${APP_DIR}'
exec '${APP_DIR}/.venv/bin/python' -m cbrs "\$@"
EOF
chmod 0755 /usr/local/bin/cbrs

install -d -m 0755 /usr/local/share/bash-completion/completions
cat > /usr/local/share/bash-completion/completions/cbrs <<'EOF'
_cbrs_complete() {
  local commands="overview service config accounts health commands doctor preflight readiness captcha-health init search download validate captcha-test soak pool jobs"
  local services="owner worker dashboard display novnc backup watchdog all"
  if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${commands}" -- "${COMP_WORDS[COMP_CWORD]}") )
  elif [[ ${COMP_CWORD} -eq 2 && ${COMP_WORDS[1]} == service ]]; then
    COMPREPLY=( $(compgen -W "status start stop restart logs" -- "${COMP_WORDS[COMP_CWORD]}") )
  elif [[ ${COMP_CWORD} -eq 3 && ${COMP_WORDS[1]} == service ]]; then
    COMPREPLY=( $(compgen -W "${services}" -- "${COMP_WORDS[COMP_CWORD]}") )
  fi
}
complete -F _cbrs_complete cbrs
EOF

echo "Ubuntu runtime installed. Complete ${APP_DIR}/.env and"
echo "${STATE_DIR}/account-pool.json, run the documented preflight, then enable services."
echo "Global CLI installed: cbrs commands"
if [[ -n "${OPERATOR_USER}" && "${OPERATOR_USER}" != "root" ]]; then
  echo "Open a new WSL shell once so ${OPERATOR_USER} receives cbrs group access."
fi
