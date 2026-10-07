#!/usr/bin/env bash
# Installs Macaw as its own Linux user and systemd service.
# Touches only: the 'macaw' user, /opt/macaw, and /etc/systemd/system/macaw.service.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/t6wy26zj95-creator/macawsrs.git}"
BASE=/opt/macaw
SERVICE=/etc/systemd/system/macaw.service

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

python3 - <<'PY' || { echo "Python 3.10 or newer is required." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "python3-venv is missing. Install it with: sudo apt install python3-venv" >&2
  exit 1
fi
command -v git >/dev/null || { echo "git is missing. Install it with: sudo apt install git" >&2; exit 1; }

if ! id macaw >/dev/null 2>&1; then
  echo "Creating system user 'macaw'"
  useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin macaw
fi
mkdir -p "$BASE/data"
chown macaw:macaw "$BASE" "$BASE/data"
chmod 750 "$BASE"
cd "$BASE"

if [[ ! -d "$BASE/app/.git" ]]; then
  echo "Downloading the code to $BASE/app"
  sudo -u macaw git clone --quiet "$REPO_URL" "$BASE/app"
else
  echo "Code already present; updating"
  sudo -u macaw git -C "$BASE/app" pull --ff-only --quiet
fi

echo "Installing Python packages (this can take a minute)"
[[ -d "$BASE/venv" ]] || sudo -u macaw python3 -m venv "$BASE/venv"
sudo -u macaw "$BASE/venv/bin/pip" install --quiet --upgrade pip
sudo -u macaw "$BASE/venv/bin/pip" install --quiet -r "$BASE/app/requirements.txt"

if [[ ! -f "$BASE/.env" ]]; then
  install -o macaw -g macaw -m 600 "$BASE/app/.env.example" "$BASE/.env"
  echo "Created $BASE/.env from the example"
fi

install -m 644 "$BASE/app/deploy/macaw.service" "$SERVICE"
systemctl daemon-reload
systemctl enable macaw.service >/dev/null

echo
echo "Installed. Next steps:"
echo "  1. sudo nano $BASE/.env      (fill in BOT_TOKEN, OWNER_IDS, CLAUDE_CODE_OAUTH_TOKEN, DEFAULT_TIMEZONE)"
echo "  2. sudo systemctl restart macaw"
echo "  3. sudo journalctl -u macaw -f   (watch the log; Ctrl+C to stop watching)"
