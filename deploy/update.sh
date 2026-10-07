#!/usr/bin/env bash
# Pulls the latest code and restarts the bot. Touches nothing outside /opt/macaw.
set -euo pipefail
BASE=/opt/macaw
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi
cd "$BASE"
sudo -u macaw git -C "$BASE/app" pull --ff-only
sudo -u macaw "$BASE/venv/bin/pip" install --quiet -r "$BASE/app/requirements.txt"
install -m 644 "$BASE/app/deploy/macaw.service" /etc/systemd/system/macaw.service
systemctl daemon-reload
systemctl restart macaw
systemctl --no-pager --lines=5 status macaw
