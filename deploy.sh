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

# Load NAS connection from .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    NAS_HOST=$(grep -E '^NAS_HOST=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_USER=$(grep -E '^NAS_USER=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_DEPLOY_PATH=$(grep -E '^NAS_DEPLOY_PATH=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST in .env (e.g. 192.168.1.100)}"
NAS_USER="${NAS_USER:-admin}"
NAS_DEPLOY_PATH="${NAS_DEPLOY_PATH:-/volume1/docker/tradeautonom}"

SSH_TARGET="${NAS_USER}@${NAS_HOST}"
COMPOSE_FILE="docker/docker-compose.nas.yml"

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }

ssh_cmd() { ssh -o ConnectTimeout=5 "$SSH_TARGET" "$@"; }

# ── Commands ──────────────────────────────────────────────────

cmd_sync() {
    info "Syncing code to ${SSH_TARGET}:${NAS_DEPLOY_PATH}"
    rsync -avz --delete \
        --exclude '.venv/' \
        --exclude 'venv/' \
        --exclude '.git/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.env' \
        --exclude 'data/' \
        --exclude 'server.log' \
        --exclude '.mypy_cache/' \
        --exclude '.ruff_cache/' \
        "$SCRIPT_DIR/" "${SSH_TARGET}:${NAS_DEPLOY_PATH}/"
    ok "Code synced"
}

cmd_build() {
    info "Building Docker image on NAS"
    ssh_cmd "cd '${NAS_DEPLOY_PATH}' && docker compose -f '${COMPOSE_FILE}' build"
    ok "Image built"
}

cmd_up() {
    info "Starting container on NAS"
    ssh_cmd "cd '${NAS_DEPLOY_PATH}' && docker compose -f '${COMPOSE_FILE}' up -d"
    ok "Container started"
}

cmd_restart() {
    info "Restarting container on NAS"
    ssh_cmd "docker restart tradeautonom"
    ok "Container restarted"
}

cmd_stop() {
    info "Stopping container on NAS"
    ssh_cmd "cd '${NAS_DEPLOY_PATH}' && docker compose -f '${COMPOSE_FILE}' down"
    ok "Container stopped"
}

cmd_logs() {
    info "Tailing container logs (Ctrl+C to stop)"
    ssh_cmd "docker logs tradeautonom --tail 100 -f"
}

cmd_status() {
    info "Container status:"
    ssh_cmd "docker ps --filter name=tradeautonom --format 'table {{.Status}}\t{{.Ports}}'"
    echo ""
    APP_PORT=$(grep -E '^APP_PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "8002")
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
    APP_PORT=$(grep -E '^APP_PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "8002")
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
