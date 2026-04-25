#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# migrate-v1-to-v2.sh — Manuelle State-Migration Photon → CF R2
#
# Phase F.4 M4 (P0) — minimaler Migration-Pfad bis der vollautomatische
# Flip-Orchestrator (M5) fertig ist.
#
# Was es macht:
#   1. Prüft IDLE-Status aller Bots des Users auf Photon (V1).
#   2. Erzeugt einen tar.gz Snapshot von /app/data/ aus dem
#      ta-<user_id> Docker-Container auf Photon.
#   3. POSTet das Tarball an https://user-v2.defitool.de/__state/flush
#      mit dem korrekten X-Internal-Token (V2_SHARED_TOKEN).
#   4. Verifiziert R2 hat das Object empfangen.
#   5. (Optional) flippt user.backend in D1 zu 'cf'.
#   6. (Optional) stoppt den Photon-Container.
#
# Es macht NICHT (bewusst):
#   - automatischen Bot-Stopp (User muss selbst alle in IDLE bringen)
#   - Rollback bei Fehler (manuell rückgängig machen)
#   - Validierung der Tar-Inhalte
#
# Voraussetzungen:
#   - SSH-Zugriff auf Photon NAS (NAS_HOST, NAS_USER, ~/.ssh/id_ed25519)
#   - INGEST_TOKEN (für /api/admin/probe und /api/admin/user/.../backend)
#   - V2_SHARED_TOKEN (für den /__state/flush Worker-Aufruf direkt)
#
# Usage:
#   ./migrate-v1-to-v2.sh <user_id> [--dry-run] [--flip] [--stop-photon]
#
#   --dry-run     Macht alles bis vor Schritt 3 (kein Upload), zeigt Größe
#   --flip        Flippt nach erfolgreichem Upload user.backend → 'cf'
#   --stop-photon Stoppt nach erfolgreichem Flip den Photon-Container
#
# Env-Vars (aus Repo-root .env oder shell):
#   NAS_HOST, NAS_USER, NAS_DEPLOY_PATH       — wie in deploy/v3/manage.sh
#   INGEST_TOKEN                              — Worker secret
#   V2_SHARED_TOKEN                           — Worker secret (beide V2 Workers)
#   MAIN_API_BASE  (default https://bot.defitool.de/api)
#   USER_V2_BASE   (default https://user-v2.defitool.de)
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Env loading ────────────────────────────────────────────────
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env"
    set +a
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST in .env or shell (e.g. 192.168.133.100)}"
NAS_USER="${NAS_USER:-root}"
INGEST_TOKEN="${INGEST_TOKEN:?Set INGEST_TOKEN in .env or shell}"
V2_SHARED_TOKEN="${V2_SHARED_TOKEN:?Set V2_SHARED_TOKEN in .env or shell}"
MAIN_API_BASE="${MAIN_API_BASE:-https://bot.defitool.de/api}"
USER_V2_BASE="${USER_V2_BASE:-https://user-v2.defitool.de}"

CONTAINER_PREFIX="ta-"

SSH_KEY="${HOME}/.ssh/id_ed25519"
if [[ -f "$SSH_KEY" ]]; then
    SSH_OPTS=(-o ConnectTimeout=5 -o IdentitiesOnly=yes -i "$SSH_KEY")
else
    SSH_OPTS=(-o ConnectTimeout=5)
fi

# ── Pretty print ──────────────────────────────────────────────
info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }
warn()  { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;35m═══ %s ═══\033[0m\n' "$*"; }

# ── Args ──────────────────────────────────────────────────────
USER_ID=""
DRY_RUN=false
DO_FLIP=false
STOP_PHOTON=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=true; shift ;;
        --flip)        DO_FLIP=true; shift ;;
        --stop-photon) STOP_PHOTON=true; shift ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            if [[ -z "$USER_ID" ]]; then
                USER_ID="$1"; shift
            else
                err "Unknown arg: $1"; exit 1
            fi
            ;;
    esac
done

if [[ -z "$USER_ID" ]]; then
    err "Usage: $0 <user_id> [--dry-run] [--flip] [--stop-photon]"
    exit 1
fi

CONTAINER_NAME="${CONTAINER_PREFIX}${USER_ID}"

info "Migration plan for user_id='$USER_ID':"
echo "  Photon container: $CONTAINER_NAME"
echo "  NAS host: $NAS_USER@$NAS_HOST"
echo "  R2 endpoint: $USER_V2_BASE"
echo "  Dry run: $DRY_RUN"
echo "  Flip backend after upload: $DO_FLIP"
echo "  Stop Photon after flip: $STOP_PHOTON"

# ── Step 0: Pre-flight checks ─────────────────────────────────
step "Step 0: Pre-flight checks"

info "Checking SSH connectivity to NAS..."
if ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" "echo ok" >/dev/null 2>&1; then
    ok "SSH OK"
else
    err "SSH to ${NAS_USER}@${NAS_HOST} failed"
    exit 2
fi

info "Checking Photon container '$CONTAINER_NAME' exists and is running..."
CONTAINER_STATUS=$(ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" \
    "/usr/bin/docker inspect --format '{{.State.Status}}' '$CONTAINER_NAME' 2>/dev/null" || echo "not_found")
if [[ "$CONTAINER_STATUS" == "not_found" ]]; then
    err "Container '$CONTAINER_NAME' not found on NAS"
    exit 2
elif [[ "$CONTAINER_STATUS" != "running" ]]; then
    warn "Container is in state '$CONTAINER_STATUS' (not 'running'). Continuing anyway."
else
    ok "Container is running"
fi

# ── Step 1: Bot IDLE check ────────────────────────────────────
step "Step 1: Verify all bots are IDLE on V1"

info "Querying /fn/bots via admin probe..."
BOTS_RESP=$(curl -sS --max-time 30 "$MAIN_API_BASE/admin/probe/$USER_ID/fn/bots?token=$INGEST_TOKEN")
NON_IDLE=$(echo "$BOTS_RESP" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    body = d.get('upstream_body_preview', '')
    if 'bots' not in body:
        # Bot registry may not be running — treat as IDLE
        print('')
        sys.exit(0)
    p = json.loads(body)
    bots = p.get('bots') or []
    nonidle = [b for b in bots if str(b.get('state', '')) not in ('IDLE', 'ERROR', '')]
    if nonidle:
        for b in nonidle:
            print(f'{b.get(\"bot_id\", \"?\")}:{b.get(\"state\", \"?\")}')
except Exception as e:
    print(f'parse_err:{e}', file=sys.stderr)
    sys.exit(1)
")
if [[ -n "$NON_IDLE" ]]; then
    err "Non-IDLE bots found:"
    echo "$NON_IDLE" | sed 's/^/  - /'
    err "Refusing to migrate. Stop / pause these bots first."
    exit 3
fi
ok "All bots IDLE/ERROR or none active"

# ── Step 2: Snapshot /app/data via SSH+docker exec ──────────
step "Step 2: Snapshot /app/data from Photon container"

LOCAL_TMP="$(mktemp -d /tmp/ta-migrate-XXXXXX)"
SNAPSHOT_TGZ="$LOCAL_TMP/state.tar.gz"

info "Streaming tar.gz from container to $SNAPSHOT_TGZ ..."
# tar inside container, stream over SSH stdout, save locally.
# `-C /app data` so the archive contains 'data/...' (we'll repackage to top-level)
# Using `--ignore-failed-read` in case files are written during snapshot.
if ! ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" \
       "/usr/bin/docker exec '$CONTAINER_NAME' tar czf - --warning=no-file-changed -C /app data 2>/dev/null" \
       > "$SNAPSHOT_TGZ"; then
    err "Snapshot failed"
    exit 4
fi

ORIG_SIZE=$(wc -c < "$SNAPSHOT_TGZ" | tr -d ' ')
ok "Container tar saved: $ORIG_SIZE bytes"

# Repackage so the archive is rooted at /app/data files (no leading 'data/').
# This matches the format that cloud_persistence._make_tarball produces and
# what restore_sync expects.
REPACK_TGZ="$LOCAL_TMP/state-repack.tar.gz"
EXTRACT_DIR="$LOCAL_TMP/extract"
mkdir -p "$EXTRACT_DIR"
tar xzf "$SNAPSHOT_TGZ" -C "$EXTRACT_DIR"

if [[ ! -d "$EXTRACT_DIR/data" ]]; then
    err "Extracted snapshot missing 'data/' directory — unexpected"
    exit 4
fi

# Add a _STATE_VERSION marker so the v2 container's restore_sync recognizes it
echo "1" > "$EXTRACT_DIR/data/_STATE_VERSION"

(cd "$EXTRACT_DIR/data" && tar czf "$REPACK_TGZ" .)
REPACK_SIZE=$(wc -c < "$REPACK_TGZ" | tr -d ' ')
info "Repackaged tarball: $REPACK_SIZE bytes (with _STATE_VERSION=1)"

info "Tarball contents (first 30 entries):"
tar tzf "$REPACK_TGZ" | head -30 | sed 's/^/  /'

if [[ "$DRY_RUN" == "true" ]]; then
    ok "Dry run done — local tarball at $REPACK_TGZ"
    echo
    echo "  Inspect: tar tzvf $REPACK_TGZ"
    echo "  Discard: rm -rf $LOCAL_TMP"
    exit 0
fi

# ── Step 3: Upload to R2 via /__state/flush ──────────────────
step "Step 3: Upload tarball to R2 via user-v2 /__state/flush"

info "POSTing $REPACK_SIZE bytes to $USER_V2_BASE/__state/flush?user_id=$USER_ID ..."
HTTP_CODE_FILE="$LOCAL_TMP/http_code"
RESP_BODY_FILE="$LOCAL_TMP/resp_body"
HTTP_CODE=$(curl -sS --max-time 60 -o "$RESP_BODY_FILE" -w '%{http_code}' \
    -X POST "$USER_V2_BASE/__state/flush?user_id=$USER_ID" \
    -H "X-Internal-Token: $V2_SHARED_TOKEN" \
    -H "Content-Type: application/gzip" \
    -A "tradeautonom-migrate/1.0" \
    --data-binary "@$REPACK_TGZ" || echo "000")

echo "  HTTP $HTTP_CODE"
echo "  Response: $(cat "$RESP_BODY_FILE")"

if [[ "$HTTP_CODE" != "200" ]]; then
    err "Upload failed (HTTP $HTTP_CODE)"
    err "Local tarball preserved at $REPACK_TGZ for retry"
    exit 5
fi
ok "R2 upload accepted"

# ── Step 4: Verify R2 has the object ──────────────────────────
step "Step 4: Verify R2 round-trip via /__state/restore"

info "Downloading the just-uploaded tarball back from R2..."
VERIFY_TGZ="$LOCAL_TMP/verify.tar.gz"
HTTP_CODE_GET=$(curl -sS --max-time 60 -o "$VERIFY_TGZ" -w '%{http_code}' \
    "$USER_V2_BASE/__state/restore?user_id=$USER_ID" \
    -H "X-Internal-Token: $V2_SHARED_TOKEN" \
    -A "tradeautonom-migrate/1.0" || echo "000")
echo "  HTTP $HTTP_CODE_GET"

if [[ "$HTTP_CODE_GET" != "200" ]]; then
    err "Restore-readback failed (HTTP $HTTP_CODE_GET) — uploaded but unreadable?"
    exit 6
fi

VERIFY_SIZE=$(wc -c < "$VERIFY_TGZ" | tr -d ' ')
echo "  Read-back size: $VERIFY_SIZE bytes (uploaded: $REPACK_SIZE)"

if [[ "$VERIFY_SIZE" -lt 100 ]]; then
    err "Read-back tarball implausibly small — aborting"
    exit 6
fi

# Spot-check the version marker is in there
if tar tzf "$VERIFY_TGZ" 2>/dev/null | grep -q '_STATE_VERSION'; then
    ok "_STATE_VERSION marker present in R2 tarball"
else
    warn "_STATE_VERSION not found in R2 tarball — restore may treat as legacy"
fi
ok "R2 round-trip OK"

# ── Step 5 (optional): Flip backend in D1 ──────────────────────
if [[ "$DO_FLIP" != "true" ]]; then
    info "Skipping D1 flip (--flip not set)"
else
    step "Step 5: Flip user.backend → 'cf' in D1"
    warn "This requires admin session auth — see deploy/cloudflare/src/index.ts:227"
    warn "The /api/admin/user/.../backend endpoint uses session cookies, not INGEST_TOKEN."
    warn "Manual step: log in to bot.defitool.de as admin, click 'Flip' for $USER_ID."
    echo
    info "Alternative: use wrangler d1 execute to update directly:"
    echo
    cat <<EOF
  wrangler d1 execute tradeautonom-history --remote \\
    --command "UPDATE user SET backend='cf', updatedAt='\$(date -u +%FT%TZ)' WHERE id='$USER_ID';"
EOF
    echo
    warn "Skipping automated flip in this script. Continuing to next step."
fi

# ── Step 6 (optional): Stop Photon container ──────────────────
if [[ "$STOP_PHOTON" != "true" ]]; then
    info "Skipping Photon container stop (--stop-photon not set)"
else
    step "Step 6: Stop Photon container '$CONTAINER_NAME'"
    info "ssh ${NAS_USER}@${NAS_HOST} docker stop ${CONTAINER_NAME}"
    if ssh "${SSH_OPTS[@]}" "${NAS_USER}@${NAS_HOST}" "/usr/bin/docker stop '$CONTAINER_NAME'" >/dev/null; then
        ok "Photon container stopped"
    else
        err "Stop failed (manual cleanup required)"
        exit 7
    fi
fi

# ── Done ──────────────────────────────────────────────────────
step "Migration complete"
ok "User '$USER_ID' state successfully copied to R2"
echo
echo "Local artifacts kept for inspection:"
echo "  $REPACK_TGZ ($REPACK_SIZE bytes)"
echo "  Discard: rm -rf $LOCAL_TMP"
echo
echo "Next steps if you didn't pass --flip:"
echo "  1. Log in as admin on bot.defitool.de"
echo "  2. Open Admin → Users, find '$USER_ID'"
echo "  3. Click 'Flip' (will refuse if any bot is non-IDLE; force only if needed)"
echo "  4. Stop Photon container manually:"
echo "       ssh ${NAS_USER}@${NAS_HOST} 'docker stop ${CONTAINER_NAME}'"
