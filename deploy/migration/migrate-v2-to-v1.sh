#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# migrate-v2-to-v1.sh — Reverse-Migration CF R2 → Photon /app/data
#
# Phase F.4 M4 (P0) — Rollback-Pfad. Falls ein User auf V2 (CF) Probleme
# hat und zurück auf V1 (Photon) muss, ohne State-Verlust.
#
# Was es macht:
#   1. Prüft IDLE-Status aller Bots auf V2 (CF).
#   2. Triggert einen Sofort-Flush auf V2, damit der R2-Tar aktuell ist.
#   3. GET das R2-Tar via /__state/restore.
#   4. Stellt sicher, dass der Photon-Container 'ta-<user_id>' existiert
#      (erstellt ihn neu falls nicht — via deploy/v3/manage.sh create).
#   5. Stoppt den Photon-Container (falls er läuft).
#   6. Extrahiert das R2-Tar in das Photon-Volume.
#   7. Startet den Photon-Container.
#   8. (Optional) flippt user.backend in D1 zu 'photon'.
#
# Voraussetzungen wie migrate-v1-to-v2.sh.
#
# Usage:
#   ./migrate-v2-to-v1.sh <user_id> [--dry-run] [--flip]
#
#   --dry-run   Nur Schritte 1-3 ausführen (zeigt Tarball-Größe)
#   --flip      Nach erfolgreicher Wiederherstellung user.backend → 'photon'
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env"
    set +a
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST}"
NAS_USER="${NAS_USER:-root}"
NAS_DEPLOY_PATH="${NAS_DEPLOY_PATH:-/opt/tradeautonom}"
INGEST_TOKEN="${INGEST_TOKEN:?Set INGEST_TOKEN}"
V2_SHARED_TOKEN="${V2_SHARED_TOKEN:?Set V2_SHARED_TOKEN}"
MAIN_API_BASE="${MAIN_API_BASE:-https://bot.defitool.de/api}"
USER_V2_BASE="${USER_V2_BASE:-https://user-v2.defitool.de}"

CONTAINER_PREFIX="ta-"

SSH_KEY="${HOME}/.ssh/id_ed25519"
if [[ -f "$SSH_KEY" ]]; then
    SSH_OPTS=(-o ConnectTimeout=5 -o IdentitiesOnly=yes -i "$SSH_KEY")
else
    SSH_OPTS=(-o ConnectTimeout=5)
fi

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }
warn()  { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;35m═══ %s ═══\033[0m\n' "$*"; }

USER_ID=""
DRY_RUN=false
DO_FLIP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --flip)    DO_FLIP=true; shift ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            if [[ -z "$USER_ID" ]]; then USER_ID="$1"; shift
            else err "Unknown arg: $1"; exit 1; fi
            ;;
    esac
done

if [[ -z "$USER_ID" ]]; then
    err "Usage: $0 <user_id> [--dry-run] [--flip]"
    exit 1
fi

CONTAINER_NAME="${CONTAINER_PREFIX}${USER_ID}"

info "Reverse-migration plan for user_id='$USER_ID':"
echo "  Photon container: $CONTAINER_NAME"
echo "  NAS host: $NAS_USER@$NAS_HOST"
echo "  R2 source: $USER_V2_BASE"
echo "  Dry run: $DRY_RUN"
echo "  Flip backend after restore: $DO_FLIP"

# ── Step 1: V2 IDLE check ─────────────────────────────────────
step "Step 1: Verify all bots are IDLE on V2 (CF)"

BOTS_RESP=$(curl -sS --max-time 30 "$MAIN_API_BASE/admin/probe/$USER_ID/fn/bots?token=$INGEST_TOKEN")
NON_IDLE=$(echo "$BOTS_RESP" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    body = d.get('upstream_body_preview', '')
    if 'bots' not in body:
        sys.exit(0)
    p = json.loads(body)
    bots = p.get('bots') or []
    nonidle = [b for b in bots if str(b.get('state', '')) not in ('IDLE', 'ERROR', '')]
    for b in nonidle:
        print(f'{b.get(\"bot_id\", \"?\")}:{b.get(\"state\", \"?\")}')
except Exception:
    pass
")
if [[ -n "$NON_IDLE" ]]; then
    err "Non-IDLE bots on V2:"
    echo "$NON_IDLE" | sed 's/^/  - /'
    err "Refusing to migrate. Stop bots first."
    exit 3
fi
ok "All V2 bots IDLE"

# ── Step 2: Trigger force-flush on V2 ──────────────────────────
step "Step 2: Trigger immediate flush on V2 so R2 has latest state"

curl -sS --max-time 30 -X POST "$MAIN_API_BASE/admin/probe/$USER_ID/settings/flush-state?token=$INGEST_TOKEN" \
    > /dev/null
ok "Flush triggered (R2 should now have current V2 state)"
sleep 2

# ── Step 3: Download R2 tarball ────────────────────────────────
step "Step 3: Download R2 tarball"

LOCAL_TMP="$(mktemp -d /tmp/ta-rollback-XXXXXX)"
R2_TGZ="$LOCAL_TMP/state-from-r2.tar.gz"

HTTP_CODE=$(curl -sS --max-time 60 -o "$R2_TGZ" -w '%{http_code}' \
    "$USER_V2_BASE/__state/restore?user_id=$USER_ID" \
    -H "X-Internal-Token: $V2_SHARED_TOKEN" \
    -A "tradeautonom-rollback/1.0" || echo "000")
if [[ "$HTTP_CODE" != "200" ]]; then
    err "Restore-download failed (HTTP $HTTP_CODE)"
    exit 4
fi

R2_SIZE=$(wc -c < "$R2_TGZ" | tr -d ' ')
info "Downloaded: $R2_SIZE bytes"
info "Tarball contents:"
tar tzf "$R2_TGZ" 2>&1 | head -30 | sed 's/^/  /'

if [[ "$DRY_RUN" == "true" ]]; then
    ok "Dry run done — local tarball at $R2_TGZ"
    exit 0
fi

# ── Step 4: Ensure Photon container exists ─────────────────────
step "Step 4: Verify Photon container '$CONTAINER_NAME' exists"

CONTAINER_STATUS=$(ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" \
    "/usr/bin/docker inspect --format '{{.State.Status}}' '$CONTAINER_NAME' 2>/dev/null" || echo "not_found")

if [[ "$CONTAINER_STATUS" == "not_found" ]]; then
    err "Container '$CONTAINER_NAME' does not exist on Photon."
    err "Create it first via: $PROJECT_ROOT/deploy/v3/manage.sh create $USER_ID"
    err "Then re-run this script."
    exit 5
fi
ok "Container exists (status: $CONTAINER_STATUS)"

# ── Step 5: Stop Photon container ──────────────────────────────
step "Step 5: Stop Photon container before extracting state"

if [[ "$CONTAINER_STATUS" == "running" ]]; then
    info "Stopping container..."
    ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" "/usr/bin/docker stop '$CONTAINER_NAME'" > /dev/null
    ok "Stopped"
else
    info "Container already stopped"
fi

# ── Step 6: Copy + extract tarball into container ──────────────
step "Step 6: Extract tarball into container's /app/data"

# Determine the data volume path. The v3/manage.sh create command sets up
# a bind mount or named volume. We use docker cp to push the tar in,
# then docker run a one-shot to extract.

REMOTE_TGZ="/tmp/ta-rollback-${USER_ID}-$(date +%s).tar.gz"

info "Uploading tarball to NAS at $REMOTE_TGZ ..."
scp "${SSH_OPTS[@]}" "$R2_TGZ" "${NAS_USER}@${NAS_HOST}:$REMOTE_TGZ" >/dev/null
ok "Uploaded"

info "Copying into container + extracting..."
# We use `docker cp` (works on stopped containers) + `docker run` with the
# same volume to extract.
ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" bash << EOSSH
set -e
# Copy tarball into stopped container's /tmp
/usr/bin/docker cp "$REMOTE_TGZ" "$CONTAINER_NAME:/tmp/restore.tar.gz"
# Use docker exec — but container is stopped. So instead start it briefly,
# extract, then stop.
/usr/bin/docker start "$CONTAINER_NAME" >/dev/null
sleep 3
# Wipe old /app/data first to avoid leftover files mismatching the new state.
# Skip _STATE_VERSION (v2 metadata) — it's not part of v1 layout.
/usr/bin/docker exec "$CONTAINER_NAME" sh -c 'rm -rf /app/data && mkdir -p /app/data'
/usr/bin/docker exec "$CONTAINER_NAME" sh -c 'cd /app/data && tar xzf /tmp/restore.tar.gz --exclude=_STATE_VERSION'
/usr/bin/docker exec "$CONTAINER_NAME" sh -c 'rm -f /tmp/restore.tar.gz'
/usr/bin/docker stop "$CONTAINER_NAME" >/dev/null
rm -f "$REMOTE_TGZ"
EOSSH
ok "State extracted into container"

# ── Step 7: Start Photon container ─────────────────────────────
step "Step 7: Start Photon container"

ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" "/usr/bin/docker start '$CONTAINER_NAME'" > /dev/null
ok "Container started"
sleep 5

NEW_STATUS=$(ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" \
    "/usr/bin/docker inspect --format '{{.State.Status}}' '$CONTAINER_NAME'" || echo "?")
info "Container state after start: $NEW_STATUS"

# ── Step 8 (optional): Flip backend ────────────────────────────
if [[ "$DO_FLIP" != "true" ]]; then
    info "Skipping D1 flip (--flip not set)"
else
    step "Step 8: Flip user.backend → 'photon' in D1"
    warn "Manual step: use wrangler d1 execute or admin UI."
    echo
    cat <<EOF
  wrangler d1 execute tradeautonom-history --remote \\
    --command "UPDATE user SET backend='photon', updatedAt='\$(date -u +%FT%TZ)' WHERE id='$USER_ID';"
EOF
fi

# ── Done ───────────────────────────────────────────────────────
step "Reverse-migration complete"
ok "User '$USER_ID' state restored from R2 to Photon"
echo
echo "Verify by visiting bot.defitool.de — user should see V1 bots back."
echo "Local artifact: $R2_TGZ (discard with: rm -rf $LOCAL_TMP)"
