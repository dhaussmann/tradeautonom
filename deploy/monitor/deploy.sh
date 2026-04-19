#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy-oms.sh — Deploy OMS (Orderbook Monitor Service) to Synology NAS
#
# Usage:
#   ./deploy.sh              # sync + build + start
#   ./deploy.sh --restart    # restart container only
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

OMS_DEPLOY_PATH="${NAS_DEPLOY_PATH}/oms"
IMAGE_NAME="oms"
IMAGE_TAG="latest"
CONTAINER_NAME="oms"
OMS_PORT="8099"

SSH_TARGET="${NAS_USER}@${NAS_HOST}"
SSH_KEY="${HOME}/.ssh/id_ed25519"
if [[ -f "$SSH_KEY" ]]; then
    SSH_OPTS="-o ConnectTimeout=5 -o IdentitiesOnly=yes -i ${SSH_KEY}"
else
    SSH_OPTS="-o ConnectTimeout=5"
fi

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }

P="/usr/bin"
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

# ── Commands ──────────────────────────────────────────────────

cmd_sync() {
    info "Syncing OMS to ${SSH_TARGET}:${OMS_DEPLOY_PATH}"
    ssh_nas "mkdir -p '${OMS_DEPLOY_PATH}'"
    # Only sync the monitor directory (Dockerfile, requirements.txt, monitor_service.py)
    tar -C "$SCRIPT_DIR" -czf - Dockerfile requirements.txt monitor_service.py \
    | ssh_nas "tar -C '${OMS_DEPLOY_PATH}' -xzf -"
    ok "OMS code synced"
}

cmd_build() {
    info "Building OMS Docker image on NAS (${IMAGE_NAME}:${IMAGE_TAG})"
    ssh_nas "cd '${OMS_DEPLOY_PATH}' && ${P}/docker build -t ${IMAGE_NAME}:${IMAGE_TAG} ."
    ok "Image built"
}

cmd_up() {
    info "Starting OMS container on NAS (port ${OMS_PORT})"
    ssh_nas "${P}/docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    ssh_nas "${P}/docker run -d \
        --name ${CONTAINER_NAME} \
        --restart unless-stopped \
        -p ${OMS_PORT}:${OMS_PORT} \
        -e OMS_TRACKED_PAIRS=auto \
        -e OMS_GRVT_ENV=prod \
        -e OMS_NADO_ENV=mainnet \
        -e OMS_MIN_EXCHANGES=2 \
        ${IMAGE_NAME}:${IMAGE_TAG}"
    ok "OMS container started"
}

cmd_restart() {
    info "Restarting OMS container on NAS"
    ssh_nas "${P}/docker restart ${CONTAINER_NAME}"
    ok "Container restarted"
}

cmd_stop() {
    info "Stopping OMS container on NAS"
    ssh_nas "${P}/docker stop ${CONTAINER_NAME} && ${P}/docker rm ${CONTAINER_NAME}"
    ok "Container stopped"
}

cmd_logs() {
    info "Tailing OMS container logs (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs ${CONTAINER_NAME} --tail 100 -f"
}

cmd_status() {
    info "OMS container status:"
    ssh_nas "${P}/docker ps --filter name=${CONTAINER_NAME} --format 'table {{.Status}}\t{{.Ports}}'"
    echo ""
    info "Health check:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${OMS_PORT}/health" | python3 -m json.tool 2>/dev/null || err "Cannot reach http://${NAS_HOST}:${OMS_PORT}/health"
    echo ""
    info "Feed summary:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${OMS_PORT}/status" | python3 -c "
import sys,json
d=json.load(sys.stdin)
total=len(d)
connected=sum(1 for v in d.values() if v['connected'])
has_data=sum(1 for v in d.values() if v['has_data'])
print(f'  Feeds: {total} | Connected: {connected} | Has data: {has_data}')
from collections import Counter
c=Counter(k.split(':')[0] for k in d)
for ex,cnt in c.most_common():
    conn=sum(1 for k,v in d.items() if k.startswith(ex+':') and v['connected'])
    print(f'  {ex}: {cnt} feeds, {conn} connected')
" 2>/dev/null || err "Cannot reach status endpoint"
}

cmd_deploy() {
    cmd_sync
    cmd_build
    cmd_up
    echo ""
    sleep 15
    cmd_status
    echo ""
    ok "OMS deployed! API: http://${NAS_HOST}:${OMS_PORT}/health"
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
