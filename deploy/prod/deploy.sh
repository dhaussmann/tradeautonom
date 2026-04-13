#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy.sh — Deploy TradeAutonom to Synology NAS
#
# Usage:
#   ./deploy.sh              # sync code + rebuild + restart
#   ./deploy.sh --restart    # restart container only (no rebuild)
#   ./deploy.sh --logs       # tail live container logs
#   ./deploy.sh --stop       # stop the container
#   ./deploy.sh --status     # show container + health status
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

SSH_TARGET="${NAS_USER}@${NAS_HOST}"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-o ConnectTimeout=5 -o IdentitiesOnly=yes -i ${SSH_KEY}"
IMAGE_NAME="tradeautonom"
CONTAINER_NAME="tradeautonom"
APP_PORT=$(grep -E '^APP_PORT=' "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "8002")

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }

P="/usr/local/bin"  # docker lives here on Synology
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

# ── Commands ──────────────────────────────────────────────────

cmd_sync() {
    info "Syncing code to ${SSH_TARGET}:${NAS_DEPLOY_PATH}"
    tar -C "$PROJECT_ROOT" \
        --exclude='.venv' \
        --exclude='venv' \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='data' \
        --exclude='server.log' \
        --exclude='.mypy_cache' \
        --exclude='.ruff_cache' \
        --exclude='.windsurf' \
        -czf - . \
    | ssh_nas "mkdir -p '${NAS_DEPLOY_PATH}' && tar -C '${NAS_DEPLOY_PATH}' -xzf -"
    ok "Code synced"
}

cmd_build() {
    info "Building Docker image on NAS"
    ssh_nas "cd '${NAS_DEPLOY_PATH}' && ${P}/docker build -f deploy/prod/Dockerfile -t ${IMAGE_NAME}:latest ."
    ok "Image built"
}

cmd_up() {
    info "Starting container on NAS"
    # Stop and remove existing container if present
    ssh_nas "${P}/docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    ssh_nas "${P}/docker run -d \
        --name ${CONTAINER_NAME} \
        --restart unless-stopped \
        -p ${APP_PORT}:${APP_PORT} \
        -e APP_HOST=0.0.0.0 \
        --env-file '${NAS_DEPLOY_PATH}/.env' \
        -v '${NAS_DEPLOY_PATH}/data:/app/data' \
        ${IMAGE_NAME}:latest"
    ok "Container started"
}

cmd_restart() {
    info "Restarting container on NAS"
    ssh_nas "${P}/docker restart ${CONTAINER_NAME}"
    ok "Container restarted"
}

cmd_stop() {
    info "Stopping container on NAS"
    ssh_nas "${P}/docker stop ${CONTAINER_NAME} && ${P}/docker rm ${CONTAINER_NAME}"
    ok "Container stopped"
}

cmd_logs() {
    info "Tailing container logs (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs ${CONTAINER_NAME} --tail 100 -f"
}

cmd_status() {
    info "Container status:"
    ssh_nas "${P}/docker ps --filter name=${CONTAINER_NAME} --format 'table {{.Status}}	{{.Ports}}'"
    echo ""
    APP_PORT=$(grep -E '^APP_PORT=' "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "8002")
    info "Health check:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${APP_PORT}/health" | python3 -m json.tool 2>/dev/null || err "Cannot reach http://${NAS_HOST}:${APP_PORT}/health"
    echo ""
    info "Job status:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${APP_PORT}/jobs" | python3 -m json.tool 2>/dev/null || err "Cannot reach API"
}

cmd_deploy() {
    cmd_sync
    cmd_build
    cmd_up
    echo ""
    APP_PORT=$(grep -E '^APP_PORT=' "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "8002")
    ok "Deployed! UI: http://${NAS_HOST}:${APP_PORT}/ui"
}

# ── Main ──────────────────────────────────────────────────────

case "${1:-deploy}" in
    --restart|-r)   cmd_restart ;;
    --logs|-l)      cmd_logs ;;
    --stop|-s)      cmd_stop ;;
    --status|-t)    cmd_status ;;
    --sync)         cmd_sync ;;
    --build)        cmd_sync; cmd_build ;;
    deploy|"")      cmd_deploy ;;
    *)
        echo "Usage: $0 [--restart|--logs|--stop|--status|--sync|--build]"
        echo "  (no args)    Full deploy: sync + build + start"
        echo "  --restart    Restart container only"
        echo "  --logs       Tail live container logs"
        echo "  --stop       Stop the container"
        echo "  --status     Show container & health status"
        echo "  --sync       Sync code only (no rebuild)"
        echo "  --build      Sync + rebuild (no restart)"
        exit 1
        ;;
esac
