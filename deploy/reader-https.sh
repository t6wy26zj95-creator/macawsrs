#!/usr/bin/env bash
# Gives the book reader Mini App a web address with https, which Telegram requires.
#
#   sudo bash /opt/macaw/app/deploy/reader-https.sh            (free address: <your-ip>.sslip.io)
#   sudo bash /opt/macaw/app/deploy/reader-https.sh books.example.com   (your own domain)
#
# It installs Caddy, a small web server that gets and renews the https
# certificate by itself, and points it at the reader (which listens on this
# server only). It never takes a port another program already uses: if 443 is
# busy (a VPN often uses it), the reader gets port 8443 instead.
# Touches: the caddy package, /etc/caddy, the firewall rules for its ports (only
# if ufw is on), and READER_URL in /opt/macaw/.env.
set -euo pipefail

BASE=/opt/macaw
ENV_FILE="$BASE/.env"
SITE_FILE=/etc/caddy/macaw-reader.caddy

say() { printf '\n%s\n' "$*"; }
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run it with sudo: sudo bash $0"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE is missing. Is the bot installed in $BASE?"

port_user() {  # prints the program listening on a TCP port, empty if the port is free
  ss -Hltnp "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n1
}
port_busy() { [[ -n "$(ss -Hltn "sport = :$1" 2>/dev/null)" ]]; }

# 1. The address.
DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
  [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "couldn't find this server's public IP address. Run the script again with a domain name after it."
  DOMAIN="${IP//./-}.sslip.io"
fi
say "Address: $DOMAIN"

READER_PORT="$(sed -n 's/^READER_PORT=\([0-9]*\).*/\1/p' "$ENV_FILE" | tail -n1)"
READER_PORT="${READER_PORT:-8090}"

# 2. Ports. Caddy needs port 80 to get the certificate; https goes on 443, or 8443 if 443 is taken.
HAD_CADDY=0
command -v caddy >/dev/null && HAD_CADDY=1
user80="$(port_user 80)"
if port_busy 80 && [[ "$user80" != "caddy" ]]; then
  fail "port 80 is already used by '${user80:-another program}'. The certificate needs it, so nothing was changed."
fi
user443="$(port_user 443)"
if ! port_busy 443 || [[ "$user443" == "caddy" ]]; then
  HTTPS_PORT=443
else
  user8443="$(port_user 8443)"
  if port_busy 8443 && [[ "$user8443" != "caddy" ]]; then
    fail "ports 443 and 8443 are both in use (by '${user443:-?}' and '${user8443:-?}'). Nothing was changed."
  fi
  HTTPS_PORT=8443
  say "Port 443 is used by '${user443:-another program}', so the reader will use port 8443. That program is not touched."
fi

# 3. Caddy.
if [[ $HAD_CADDY -eq 0 ]]; then
  say "Installing Caddy (this can take a minute)"
  apt-get update -qq
  if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
fi

if [[ $HTTPS_PORT -eq 443 ]]; then SITE="$DOMAIN"; URL="https://$DOMAIN/"; else SITE="https://$DOMAIN:$HTTPS_PORT"; URL="https://$DOMAIN:$HTTPS_PORT/"; fi
cat > "$SITE_FILE" <<EOF
# Book reader for the Macaw bot (written by deploy/reader-https.sh)
$SITE {
	encode gzip
	reverse_proxy 127.0.0.1:$READER_PORT
}
EOF
if [[ $HAD_CADDY -eq 0 ]]; then
  # Fresh install: replace the example page Caddy comes with.
  printf 'import %s\n' "$SITE_FILE" > /etc/caddy/Caddyfile
elif ! grep -qF "import $SITE_FILE" /etc/caddy/Caddyfile; then
  printf '\nimport %s\n' "$SITE_FILE" >> /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || fail "the Caddy settings didn't validate; see: caddy validate --config /etc/caddy/Caddyfile"

# 4. Firewall, only if ufw is switched on.
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow "$HTTPS_PORT"/tcp >/dev/null
  say "Opened ports 80 and $HTTPS_PORT in the firewall (ufw)"
fi

systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# 5. Tell the bot its address.
if grep -q '^READER_URL=' "$ENV_FILE"; then
  sed -i "s#^READER_URL=.*#READER_URL=$URL#" "$ENV_FILE"
else
  printf '\nREADER_URL=%s\n' "$URL" >> "$ENV_FILE"
fi
systemctl restart macaw

# 6. Wait for the certificate.
say "Getting the https certificate (up to 2 minutes)"
for _ in $(seq 1 24); do
  if curl -fsS --max-time 5 -o /dev/null "$URL"; then
    say "Done. The reader is at $URL
In Telegram, open the bot: the Books button next to the message field opens your library (you may need to close and reopen the chat once)."
    exit 0
  fi
  sleep 5
done
say "Caddy is set up, but the certificate isn't there yet. The most common reason: the
hosting company's firewall blocks ports 80 and $HTTPS_PORT. Open both (TCP) in your
hosting control panel, then wait a minute; Caddy keeps trying by itself.
To see what Caddy says: sudo journalctl -u caddy --no-pager -n 30"
exit 1
