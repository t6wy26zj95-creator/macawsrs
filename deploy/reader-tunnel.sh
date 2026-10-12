#!/usr/bin/env bash
# Gives the book reader Mini App an https address without opening any port.
#
#   sudo bash /opt/macaw/app/deploy/reader-tunnel.sh
#
# For servers where ports 80 and 443 are already taken (a VPN, another web app),
# so deploy/reader-https.sh can't be used. It runs cloudflared, which makes an
# outgoing connection to Cloudflare and gets a free https address on
# trycloudflare.com that leads to the reader. Nothing listens on the internet,
# no firewall rule changes, and other programs are not touched.
# The address is random and changes when the tunnel restarts; the bot notices
# within a minute and updates its Books button by itself.
#
# Touches: the cloudflared package, /etc/systemd/system/macaw-tunnel.service,
# and READER_TUNNEL in /opt/macaw/.env.
# To undo: sudo systemctl disable --now macaw-tunnel
set -euo pipefail

BASE=/opt/macaw
ENV_FILE="$BASE/.env"
UNIT=/etc/systemd/system/macaw-tunnel.service

say() { printf '\n%s\n' "$*"; }
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run it with sudo: sudo bash $0"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE is missing. Is the bot installed in $BASE?"

READER_PORT="$(sed -n 's/^READER_PORT=\([0-9]*\).*/\1/p' "$ENV_FILE" | tail -n1)"
READER_PORT="${READER_PORT:-8090}"

# cloudflared's local status page, on this server only. Keep the port it had, else take a free one.
STATUS_PORT="$(sed -n 's/^READER_TUNNEL=127\.0\.0\.1:\([0-9]*\).*/\1/p' "$ENV_FILE" | tail -n1)"
if [[ -z "$STATUS_PORT" ]]; then
  for p in $(seq 8091 8099); do
    if [[ -z "$(ss -Hltn "sport = :$p" 2>/dev/null)" ]]; then STATUS_PORT=$p; break; fi
  done
fi
[[ -n "$STATUS_PORT" ]] || fail "no free local port between 8091 and 8099. Nothing was changed."

if ! command -v cloudflared >/dev/null; then
  say "Installing cloudflared (this can take a minute)"
  ARCH="$(dpkg --print-architecture)"
  TMP="$(mktemp -d)"
  curl -fsSL --max-time 120 -o "$TMP/cloudflared.deb" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb" \
    || fail "couldn't download cloudflared. Nothing was changed."
  dpkg -i "$TMP/cloudflared.deb" >/dev/null
  rm -rf "$TMP"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Tunnel for the Macaw book reader (Cloudflare quick tunnel)
After=network-online.target
Wants=network-online.target

[Service]
DynamicUser=yes
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:$STATUS_PORT --url http://127.0.0.1:$READER_PORT
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable macaw-tunnel >/dev/null 2>&1
systemctl restart macaw-tunnel

if grep -q '^READER_TUNNEL=' "$ENV_FILE"; then
  sed -i "s#^READER_TUNNEL=.*#READER_TUNNEL=127.0.0.1:$STATUS_PORT#" "$ENV_FILE"
else
  printf '\n# Set by deploy/reader-tunnel.sh: where to ask cloudflared for the reader address.\nREADER_TUNNEL=127.0.0.1:%s\n' "$STATUS_PORT" >> "$ENV_FILE"
fi

say "Waiting for the tunnel's address (up to 1 minute)"
HOST=""
for _ in $(seq 1 30); do
  HOST="$(curl -fsS --max-time 3 "http://127.0.0.1:$STATUS_PORT/quicktunnel" 2>/dev/null | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')"
  [[ -n "$HOST" ]] && break
  sleep 2
done
if [[ -z "$HOST" ]]; then
  say "The tunnel didn't start. What cloudflared says:"
  journalctl -u macaw-tunnel --no-pager -n 15
  exit 1
fi

systemctl restart macaw
say "Checking that https://$HOST works (up to 1 minute)"
for _ in $(seq 1 20); do
  if curl -fsS --max-time 5 -o /dev/null "https://$HOST/"; then
    say "Done. The reader is at https://$HOST/
In Telegram, open the bot: the Books button next to the message field opens your library
(close and reopen the chat once if you don't see it). You can also send /books."
    exit 0
  fi
  sleep 3
done
say "The tunnel is running (https://$HOST/) but the reader didn't answer yet. The bot takes
about a minute to start; send /books to the bot in a minute and tap the button."
