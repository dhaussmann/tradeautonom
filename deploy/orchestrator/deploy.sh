#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy-orchestrator.sh — Deploy the Orchestrator to Synology NAS
#
# Usage:
#   ./deploy.sh              # sync + build + start
#   ./deploy.sh --restart    # restart only
#   ./deploy.sh --logs       # tail logs
#   ./deploy.sh --stop       # stop
#   ./deploy.sh --status     # show status
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    NAS_HOST=$(grep -E '^NAS_HOST=' "$PROJECT_ROOT/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_USER=$(grep -E '^NAS_USER=' "$PROJECT_ROOT/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST in .env}"
NAS_USER="${NAS_USER:-admin}"

DEPLOY_PATH="/opt/tradeautonom-orchestrator"
IMAGE_NAME="tradeautonom-orchestrator"
IMAGE_TAG="latest"
CONTAINER_NAME="ta-orchestrator"
ORCH_PORT="8090"

# Orchestrator token — must match the wrangler secret ORCH_TOKEN
ORCH_TOKEN="${ORCH_TOKEN:?Set ORCH_TOKEN env var}"

SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-o ConnectTimeout=5 -o IdentitiesOnly=yes -i ${SSH_KEY}"
SSH_TARGET="${NAS_USER}@${NAS_HOST}"

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }

P="/usr/bin"
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

cmd_sync() {
    info "Syncing orchestrator to ${SSH_TARGET}:${DEPLOY_PATH}"
    ssh_nas "mkdir -p '${DEPLOY_PATH}'"
    scp ${SSH_OPTS} \
        "$SCRIPT_DIR/orchestrator.py" \
        "$SCRIPT_DIR/requirements.txt" \
        "$SCRIPT_DIR/Dockerfile" \
        "${SSH_TARGET}:${DEPLOY_PATH}/"
    ok "Files synced"
}

cmd_build() {
    info "Building orchestrator image on NAS"
    ssh_nas "cd '${DEPLOY_PATH}' && ${P}/docker build -t ${IMAGE_NAME}:${IMAGE_TAG} ."
    ok "Image built"
}

cmd_up() {
    info "Starting orchestrator (port ${ORCH_PORT})"
    ssh_nas "${P}/docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    ssh_nas "mkdir -p '${DEPLOY_PATH}/data'"
    ssh_nas "${P}/docker run -d \
        --name ${CONTAINER_NAME} \
        --restart unless-stopped \
        --network host \
        -e ORCH_TOKEN='${ORCH_TOKEN}' \
        -e TRADEAUTONOM_IMAGE=tradeautonom:v3 \
        -e BASE_PORT=9001 \
        -e ORCH_PORT=${ORCH_PORT} \
        -e STATE_FILE=/app/data/orchestrator_state.json \
        -e SHARED_CODE_DIR=/opt/tradeautonom-v3/app \
        -e DOCKER_HOST_IP=127.0.0.1 \
        -v '${DEPLOY_PATH}/data:/app/data' \
        -v /var/run/docker.sock:/var/run/docker.sock \
        ${IMAGE_NAME}:${IMAGE_TAG}"
    ok "Orchestrator started"
}

cmd_restart() {
    info "Restarting orchestrator"
    ssh_nas "${P}/docker restart ${CONTAINER_NAME}"
    ok "Restarted"
}

cmd_stop() {
    info "Stopping orchestrator"
    ssh_nas "${P}/docker stop ${CONTAINER_NAME} && ${P}/docker rm ${CONTAINER_NAME}"
    ok "Stopped"
}

cmd_logs() {
    info "Tailing orchestrator logs (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs ${CONTAINER_NAME} --tail 100 -f"
}

cmd_status() {
    info "Orchestrator container:"
    ssh_nas "${P}/docker ps --filter name=${CONTAINER_NAME} --format 'table {{.Status}}\t{{.Ports}}'"
    echo ""
    info "Health:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${ORCH_PORT}/orch/health" | python3 -m json.tool 2>/dev/null || err "Unreachable"
}

cmd_deploy() {
    cmd_sync
    cmd_build
    cmd_up
    echo ""
    ok "Orchestrator deployed! Health: http://${NAS_HOST}:${ORCH_PORT}/orch/health"
}

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
        exit 1
        ;;
esac
