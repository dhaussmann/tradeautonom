#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy.sh — Deploy TradeAutonom V3 (testing container)
#
# tradeautonom-v3 is the testing instance (port 8005).
# All user containers (ta-user-*) share the same code folder:
#   /opt/tradeautonom-v3/app  (bind-mounted read-only into every container)
#
# Usage:
#   ./deploy.sh                 # full deploy: sync + rebuild + (re)start
#   ./deploy.sh --deploy-code   # fast: push app/ only — all containers hot-reload
#   ./deploy.sh --restart       # restart tradeautonom-v3 only
#   ./deploy.sh --logs          # tail live container logs
#   ./deploy.sh --stop          # stop the container
#   ./deploy.sh --status        # show container + health status
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load NAS connection from .env
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    NAS_HOST=$(grep -E '^NAS_HOST=' "$PROJECT_ROOT/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_USER=$(grep -E '^NAS_USER=' "$PROJECT_ROOT/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_DEPLOY_PATH=$(grep -E '^NAS_DEPLOY_PATH=' "$PROJECT_ROOT/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST in .env (e.g. 192.168.1.100)}"
NAS_USER="${NAS_USER:-admin}"
NAS_DEPLOY_PATH="${NAS_DEPLOY_PATH:-/volume1/docker/tradeautonom}"

# V3-specific overrides
V3_DEPLOY_PATH="${NAS_DEPLOY_PATH}-v3"
IMAGE_NAME="tradeautonom"
IMAGE_TAG="v3"
CONTAINER_NAME="tradeautonom-v3"
APP_PORT="8005"
# Shared code directory — mounted read-only into tradeautonom-v3 AND all user containers.
# Pushing to this path is sufficient to update every container (uvicorn --reload picks it up).
SHARED_CODE_PATH="/opt/tradeautonom-v3/app"

SSH_TARGET="${NAS_USER}@${NAS_HOST}"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-o ConnectTimeout=5 -o IdentitiesOnly=yes -i ${SSH_KEY}"

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }

P="/usr/bin"
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

# ── Commands ──────────────────────────────────────────────────

cmd_sync() {
    info "Syncing code to ${SSH_TARGET}:${V3_DEPLOY_PATH}"
    tar -C "$PROJECT_ROOT" \
        --exclude='.venv' \
        --exclude='venv' \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='data' \
        --exclude='data-v2' \
        --exclude='data-v3' \
        --exclude='server.log' \
        --exclude='.mypy_cache' \
        --exclude='.ruff_cache' \
        --exclude='.windsurf' \
        -czf - . \
    | ssh_nas "mkdir -p '${V3_DEPLOY_PATH}' && tar -C '${V3_DEPLOY_PATH}' -xzf -"
    ok "Code synced"
}

cmd_build() {
    info "Building Docker image on NAS (${IMAGE_NAME}:${IMAGE_TAG})"
    ssh_nas "cd '${V3_DEPLOY_PATH}' && ${P}/docker build -f deploy/prod/Dockerfile -t ${IMAGE_NAME}:${IMAGE_TAG} ."
    ok "Image built"
}

cmd_up() {
    info "Starting v3 container on NAS (port ${APP_PORT})"
    # Stop and remove existing v3 container if present
    ssh_nas "${P}/docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    ssh_nas "mkdir -p '${V3_DEPLOY_PATH}/data'"
    # Create minimal .env — NO exchange API keys (user sets them via UI)
    ssh_nas "cat > '${V3_DEPLOY_PATH}/.env.container' << 'ENVEOF'
APP_HOST=0.0.0.0
APP_PORT=${APP_PORT}
GRVT_ENV=prod
HISTORY_INGEST_URL=https://bot.defitool.de/api/history/ingest
HISTORY_INGEST_TOKEN=qW3b6n2uDwZg6krrEbdqcpgihIgLzRc6mkF9dnjnTcw
HISTORY_INGEST_INTERVAL_S=300
FN_OPT_SHARED_MONITOR_URL=http://192.168.133.100:8099
ENVEOF"
    ssh_nas "${P}/docker run -d \
        --name ${CONTAINER_NAME} \
        --hostname ${CONTAINER_NAME} \
        --restart unless-stopped \
        -p ${APP_PORT}:${APP_PORT} \
        --env-file '${V3_DEPLOY_PATH}/.env.container' \
        -v '${V3_DEPLOY_PATH}/data:/app/data' \
        -v '${SHARED_CODE_PATH}:/app/app:ro' \
        ${IMAGE_NAME}:${IMAGE_TAG}"
    ok "Container started"
}

cmd_deploy_code() {
    info "Deploying app code to ${SSH_TARGET}:${SHARED_CODE_PATH}"
    # Abort if any bots are actively trading (state != IDLE/ERROR)
    local active
    active=$(curl -s --connect-timeout 3 "http://${NAS_HOST}:${APP_PORT}/fn/bots" \
        | python3 -c "
import sys, json
try:
    bots = json.load(sys.stdin)
    active = [b for b in bots if b.get('state','') not in ('IDLE','ERROR','')]
    print(len(active))
except Exception:
    print('?')
" 2>/dev/null || echo "?")
    if [[ "$active" != "0" && "$active" != "?" ]]; then
        err "Aborted — ${active} bot(s) are actively trading on ${CONTAINER_NAME}"
        exit 1
    fi
    rsync -avz --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
        "${PROJECT_ROOT}/app/" \
        "${SSH_TARGET}:${SHARED_CODE_PATH}/"
    ok "Code deployed — all containers will hot-reload in ~3s"
}

cmd_restart() {
    info "Restarting v3 container on NAS"
    ssh_nas "${P}/docker restart ${CONTAINER_NAME}"
    ok "Container restarted"
}

cmd_stop() {
    info "Stopping v3 container on NAS"
    ssh_nas "${P}/docker stop ${CONTAINER_NAME} && ${P}/docker rm ${CONTAINER_NAME}"
    ok "Container stopped"
}

cmd_logs() {
    info "Tailing v3 container logs (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs ${CONTAINER_NAME} --tail 100 -f"
}

cmd_status() {
    info "V3 Container status:"
    ssh_nas "${P}/docker ps --filter name=${CONTAINER_NAME} --format 'table {{.Status}}	{{.Ports}}'"
    echo ""
    info "Health check:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${APP_PORT}/health" | python3 -m json.tool 2>/dev/null || err "Cannot reach http://${NAS_HOST}:${APP_PORT}/health"
}

cmd_deploy() {
    cmd_sync
    cmd_build
    cmd_up
    echo ""
    ok "V3 Deployed! UI: http://${NAS_HOST}:${APP_PORT}/ui"
}

# ── Main ──────────────────────────────────────────────────────

case "${1:-deploy}" in
    --deploy-code|-c)  cmd_deploy_code ;;
    --restart|-r)      cmd_restart ;;
    --logs|-l)         cmd_logs ;;
    --stop|-s)         cmd_stop ;;
    --status|-t)       cmd_status ;;
    --sync)            cmd_sync ;;
    --build)           cmd_sync; cmd_build ;;
    deploy|"")         cmd_deploy ;;
    *)
        echo "Usage: $0 [option]"
        echo "  (no args)         Full deploy: sync + build + start"
        echo "  --deploy-code     Fast: push app/ to shared folder — all containers hot-reload"
        echo "  --restart         Restart tradeautonom-v3 only (no rebuild)"
        echo "  --logs            Tail live container logs"
        echo "  --stop            Stop the container"
        echo "  --status          Show container & health status"
        echo "  --sync            Sync entire project code to server (no rebuild)"
        echo "  --build           Sync + rebuild image (no restart)"
        exit 1
        ;;
esac
