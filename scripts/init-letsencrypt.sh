#!/usr/bin/env bash
# First-time TLS certificate issuance for the Plant Monitor.
#
# Solves the chicken-and-egg problem: nginx refuses to start without the
# certificate files its config references, but certbot's HTTP-01 challenge needs
# something already serving port 80. So: write a throwaway self-signed cert,
# start nginx with it, let certbot replace it over the webroot, reload.
#
# Deliberately NOT `certbot --nginx` (which the plan suggested): that plugin
# edits a host-installed nginx's config, and this nginx runs in a container
# holding port 80. Renewal would then need the container stopped every 60 days.
# The webroot method leaves nginx running.
#
# Run once, from the compose project root, after DNS resolves to this host:
#   ./scripts/init-letsencrypt.sh
set -euo pipefail

cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env found in $(pwd)"; exit 1; }
# shellcheck disable=SC1091
set -a && . ./.env && set +a

: "${DOMAIN:?Set DOMAIN in .env first}"
: "${LETSENCRYPT_EMAIL:?Set LETSENCRYPT_EMAIL in .env (used for expiry warnings)}"

CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
STAGING=${STAGING:-0}

echo "==> Checking that $DOMAIN resolves to this host"
# Query a PUBLIC resolver, not this host's. Let's Encrypt looks the domain up
# from its own servers, so the local cache is irrelevant — and actively
# misleading: a lookup made before the domain existed leaves a cached NXDOMAIN
# that lingers for the zone's negative TTL (30 minutes for a fresh Cloudflare
# zone), long after the record is live everywhere else.
if command -v dig >/dev/null 2>&1; then
    resolved=$(dig +short @1.1.1.1 "$DOMAIN" A 2>/dev/null | tail -1)
else
    echo "    (dig not installed — falling back to this host's resolver, which"
    echo "     may hold a stale negative cache entry; install dnsutils to avoid)"
    resolved=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
fi
public=$(curl -fsS --max-time 10 https://api.ipify.org || echo "unknown")
echo "    $DOMAIN -> ${resolved:-<nothing>} ; this host -> $public"
if [ -z "$resolved" ]; then
    echo "    DNS does not resolve yet. Let's Encrypt will fail — wait for propagation."
    exit 1
fi
if [ "$resolved" != "$public" ] && [ "$public" != "unknown" ]; then
    echo "    WARNING: DNS points somewhere else. Continuing anyway (proxied DNS?)."
fi

echo "==> Creating a throwaway self-signed cert so nginx can start"
sudo mkdir -p "$CERT_DIR" /var/www/certbot
sudo openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/fullchain.pem" \
    -subj "/CN=$DOMAIN" 2>/dev/null

echo "==> Starting nginx"
docker compose up -d nginx
sleep 3

echo "==> Deleting the throwaway cert and requesting the real one"
sudo rm -rf "$CERT_DIR" "/etc/letsencrypt/archive/$DOMAIN" \
            "/etc/letsencrypt/renewal/$DOMAIN.conf"

staging_flag=""
# STAGING=1 uses Let's Encrypt's test CA. Its certs are untrusted by browsers,
# but it has far looser rate limits — production allows only 5 failures per
# account/hostname/hour, which is easy to burn through while debugging DNS.
[ "$STAGING" != "0" ] && staging_flag="--staging"

docker run --rm \
    -v /etc/letsencrypt:/etc/letsencrypt \
    -v /var/www/certbot:/var/www/certbot \
    certbot/certbot certonly --webroot -w /var/www/certbot \
    $staging_flag \
    -d "$DOMAIN" \
    --email "$LETSENCRYPT_EMAIL" \
    --agree-tos --no-eff-email --non-interactive

echo "==> Reloading nginx with the real certificate"
docker compose exec nginx nginx -s reload

echo "==> Installing the renewal cron job"
# Certs last 90 days; certbot renews when under 30 remain, so a daily attempt is
# a no-op most days. --webroot means nginx keeps serving throughout; the reload
# only fires when a renewal actually happened.
sudo tee /etc/cron.d/plantmonitor-certbot >/dev/null <<CRON
0 4 * * * root docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/www/certbot:/var/www/certbot certbot/certbot renew --quiet --webroot -w /var/www/certbot && docker compose -f $(pwd)/docker-compose.yml exec -T nginx nginx -s reload
CRON

echo
echo "Done. Verify with:"
echo "  curl -I https://$DOMAIN/health"
echo "  docker run --rm -v /etc/letsencrypt:/etc/letsencrypt certbot/certbot renew --dry-run"
