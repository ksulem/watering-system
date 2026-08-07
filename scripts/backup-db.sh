#!/usr/bin/env bash
# Nightly TimescaleDB backup to Oracle Object Storage.
#
# Authenticates by instance principal — there is no credential on this box. The
# VM's identity is asserted by the metadata service, and an IAM policy lets the
# `plant-monitor-vm` dynamic group write to the `plant-monitor-backups` bucket
# and nothing else. Nothing to steal, and access is revoked by deleting the
# policy.
#
# Retention is enforced server-side by an Object Storage lifecycle rule
# (delete under daily/ after 30 days), so a broken script can't silently stop
# expiring old dumps.
#
# RESTORE (TimescaleDB needs more than a plain pg_restore):
#   gunzip is not needed — pg_dump -Fc output is already compressed.
#   createdb -U plantuser plantmonitor_restore
#   psql -U plantuser -d plantmonitor_restore -c "CREATE EXTENSION timescaledb;"
#   psql -U plantuser -d plantmonitor_restore -c "SELECT timescaledb_pre_restore();"
#   pg_restore -U plantuser -d plantmonitor_restore --no-owner backup.dump
#   psql -U plantuser -d plantmonitor_restore -c "SELECT timescaledb_post_restore();"
# Skipping the pre/post_restore calls corrupts the hypertable catalog — the
# restore appears to succeed and then chunk routing misbehaves.
set -euo pipefail

CONTAINER=${CONTAINER:-plant-monitor-timescaledb-1}
DB_NAME=${DB_NAME:-plantmonitor}
DB_USER=${DB_USER:-plantuser}
BUCKET=${BUCKET:-plant-monitor-backups}
LOCAL_DIR=${LOCAL_DIR:-/var/backups/plant-monitor}
LOCAL_KEEP=${LOCAL_KEEP:-3}
OCI=${OCI:-/usr/local/bin/oci}

# A dump smaller than this means pg_dump produced nothing useful — an empty
# database, a permissions failure, or a truncated pipe. Better to fail loudly
# than to upload a file that looks like a backup and isn't one.
MIN_BYTES=${MIN_BYTES:-5000}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
fail() { log "ERROR: $*"; exit 1; }

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="plantmonitor-${STAMP}.dump"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

log "dumping $DB_NAME from $CONTAINER"
# -Fc is the custom format: compressed already, and the only format pg_restore
# can filter/reorder — which the TimescaleDB restore procedure relies on.
# --no-owner/--no-acl so the dump restores into a differently-named role.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
    -Fc --no-owner --no-acl > "$TMP/$FILE" || fail "pg_dump failed"

size=$(stat -c%s "$TMP/$FILE")
[ "$size" -ge "$MIN_BYTES" ] || fail "dump is only ${size}B (min ${MIN_BYTES}B) — refusing to upload"

# Integrity check, not just a size check: pg_restore --list parses the archive's
# table of contents, so a truncated or corrupt dump fails here rather than on
# the day you actually need it.
docker exec -i "$CONTAINER" pg_restore --list > /dev/null < "$TMP/$FILE" \
    || fail "dump failed its pg_restore --list integrity check"
log "dump OK (${size} bytes, archive readable)"

log "uploading daily/$FILE to $BUCKET"
"$OCI" os object put --auth instance_principal --bucket-name "$BUCKET" \
    --name "daily/$FILE" --file "$TMP/$FILE" --no-multipart >/dev/null \
    || fail "upload failed"

# Confirm the stored bytes are the bytes we sent. An upload that reports success
# but stored something else is exactly the failure a backup must not have.
# Compare MD5 rather than length — same cost, and it catches corruption that a
# matching size would hide. Object Storage returns it base64-encoded, and only
# computes it over the whole object for non-multipart uploads (hence
# --no-multipart above).
# NB: `os object head` returns headers at the TOP level of the response, not
# under `data` — querying 'data."content-md5"' silently yields nothing.
remote_md5=$("$OCI" os object head --auth instance_principal --bucket-name "$BUCKET" \
    --name "daily/$FILE" --query '"content-md5"' --raw-output) \
    || fail "could not stat the uploaded object"
local_md5=$(openssl md5 -binary "$TMP/$FILE" | base64)
[ -n "$remote_md5" ] || fail "object head returned no content-md5"
[ "$remote_md5" = "$local_md5" ] || fail "checksum mismatch: local $local_md5, remote $remote_md5"
log "upload verified (${size} bytes, md5 $local_md5)"

# A few local copies for a fast restore; the bucket is the real backup.
mkdir -p "$LOCAL_DIR"
cp "$TMP/$FILE" "$LOCAL_DIR/$FILE"
ls -1t "$LOCAL_DIR"/plantmonitor-*.dump 2>/dev/null | tail -n +$((LOCAL_KEEP + 1)) | xargs -r rm -f

log "done: daily/$FILE"
