    #!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# manage.sh — Manage per-user TradeAutonom Docker instances
#
# Each user gets their own container with isolated data volume,
# port, and encrypted API key storage.
#
# Usage:
#   ./manage.sh create <user_id> [--port 9001]
#   ./manage.sh list
#   ./manage.sh start <user_id>
#   ./manage.sh stop <user_id>
#   ./manage.sh destroy <user_id>
#   ./manage.sh logs <user_id>
#   ./manage.sh status <user_id>
#   ./manage.sh update              # rebuild image + restart all
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

IMAGE_NAME="tradeautonom"
IMAGE_TAG="v3"
CONTAINER_PREFIX="ta-"
BASE_DEPLOY="${NAS_DEPLOY_PATH}-v3"
BASE_PORT=9001
REGISTRY_FILE="${BASE_DEPLOY}/users.json"
# Single shared code directory mounted read-only into every user container.
# Updating this path pushes code to all containers (uvicorn --reload picks it up).
SHARED_CODE_PATH="/opt/tradeautonom-v3/app"

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
warn()  { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }

P="/usr/bin"
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

# ── Registry helpers ──────────────────────────────────────────

_ensure_registry() {
    ssh_nas "mkdir -p '${BASE_DEPLOY}' && test -f '${REGISTRY_FILE}' || echo '{}' > '${REGISTRY_FILE}'"
}

_read_registry() {
    ssh_nas "cat '${REGISTRY_FILE}'"
}

_get_user_port() {
    local user_id="$1"
    _read_registry | python3 -c "
import sys,json
d=json.load(sys.stdin)
u=d.get('$user_id')
print(u['port'] if u else '')
"
}

_next_free_port() {
    _read_registry | python3 -c "
import sys,json
d=json.load(sys.stdin)
used={v['port'] for v in d.values() if 'port' in v}
p=$BASE_PORT
while p in used: p+=1
print(p)
"
}

_add_to_registry() {
    local user_id="$1" port="$2"
    ssh_nas "python3 -c \"
import json
with open('${REGISTRY_FILE}') as f: d=json.load(f)
d['$user_id']={'port':$port}
with open('${REGISTRY_FILE}','w') as f: json.dump(d,f,indent=2)
\""
}

_remove_from_registry() {
    local user_id="$1"
    ssh_nas "python3 -c \"
import json
with open('${REGISTRY_FILE}') as f: d=json.load(f)
d.pop('$user_id',None)
with open('${REGISTRY_FILE}','w') as f: json.dump(d,f,indent=2)
\""
}

# ── Commands ──────────────────────────────────────────────────

cmd_create() {
    local user_id="${1:?Usage: manage.sh create <user_id> [--port PORT] [--env-file /path/to/user.env]}"
    shift
    local port=""
    local env_file=""

    # Parse optional flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port) port="$2"; shift 2 ;;
            --env-file) env_file="$2"; shift 2 ;;
            *) err "Unknown option: $1"; exit 1 ;;
        esac
    done

    _ensure_registry

    # Check if user already exists
    local existing_port
    existing_port=$(_get_user_port "$user_id")
    if [[ -n "$existing_port" ]]; then
        err "User '$user_id' already exists on port $existing_port"
        exit 1
    fi

    # Assign port
    if [[ -z "$port" ]]; then
        port=$(_next_free_port)
    fi

    local container_name="${CONTAINER_PREFIX}${user_id}"
    local data_dir="${BASE_DEPLOY}/data-${user_id}"

    info "Creating user '$user_id' on port $port"

    # Create data directory (incl. app-data for the bind mount)
    ssh_nas "mkdir -p '${data_dir}/app-data'"

    if [[ -n "$env_file" && -f "$env_file" ]]; then
        # Admin provided a custom .env with infra keys — upload it
        info "Uploading custom .env from $env_file"
        cat "$env_file" | ssh_nas "cat > '${data_dir}/.env'"
        # Ensure APP_HOST/APP_PORT are set
        ssh_nas "grep -q '^APP_HOST=' '${data_dir}/.env' || echo 'APP_HOST=0.0.0.0' >> '${data_dir}/.env'"
        ssh_nas "grep -q '^APP_PORT=' '${data_dir}/.env' || echo 'APP_PORT=${port}' >> '${data_dir}/.env'"
    else
        # Minimal .env — all exchange keys are set by the user via the dashboard UI
        ssh_nas "cat > '${data_dir}/.env' << ENVEOF
APP_HOST=0.0.0.0
APP_PORT=${port}
GRVT_ENV=prod
ENVEOF"
    fi

    # Start container
    ssh_nas "${P}/docker run -d \
        --name ${container_name} \
        --hostname ${container_name} \
        --restart unless-stopped \
        -p ${port}:${port} \
        --ulimit nofile=65536:65536 \
        --env-file '${data_dir}/.env' \
        -v '${data_dir}/app-data:/app/data' \
        -v '${SHARED_CODE_PATH}:/app/app:ro' \
        ${IMAGE_NAME}:${IMAGE_TAG}"

    # Register user
    _add_to_registry "$user_id" "$port"

    ok "User '$user_id' created"
    echo ""
    echo "  Container: ${container_name}"
    echo "  Port:      ${port}"
    echo "  Data:      ${data_dir}"
    echo "  Dashboard: http://${NAS_HOST}:${port}/ui"
    echo ""
    echo "  User opens dashboard → sets password → enters exchange keys → done."
}

cmd_list() {
    _ensure_registry
    info "Registered users:"
    echo ""
    printf "  %-20s %-8s %-15s %s\n" "USER" "PORT" "CONTAINER" "STATUS"
    printf "  %-20s %-8s %-15s %s\n" "----" "----" "---------" "------"

    _read_registry | python3 -c "
import sys,json
d=json.load(sys.stdin)
for uid,info in sorted(d.items()):
    print(f'{uid}|{info.get(\"port\",\"?\")}')
" | while IFS='|' read -r uid port; do
        local container_name="${CONTAINER_PREFIX}${uid}"
        local status
        status=$(ssh_nas "${P}/docker inspect --format '{{.State.Status}}' ${container_name} 2>/dev/null" || echo "not found")
        printf "  %-20s %-8s %-15s %s\n" "$uid" "$port" "$container_name" "$status"
    done
    echo ""
}

cmd_start() {
    local user_id="${1:?Usage: manage.sh start <user_id>}"
    local container_name="${CONTAINER_PREFIX}${user_id}"
    info "Starting container for '$user_id'"
    ssh_nas "${P}/docker start ${container_name}"
    ok "Container started"
}

cmd_stop() {
    local user_id="${1:?Usage: manage.sh stop <user_id>}"
    local container_name="${CONTAINER_PREFIX}${user_id}"
    info "Stopping container for '$user_id'"
    ssh_nas "${P}/docker stop ${container_name}"
    ok "Container stopped"
}

cmd_destroy() {
    local user_id="${1:?Usage: manage.sh destroy <user_id>}"
    local container_name="${CONTAINER_PREFIX}${user_id}"
    local data_dir="${BASE_DEPLOY}/data-${user_id}"

    warn "This will permanently delete user '$user_id' — container + all data!"
    read -p "  Type the user ID to confirm: " confirm
    if [[ "$confirm" != "$user_id" ]]; then
        err "Aborted"
        exit 1
    fi

    info "Destroying user '$user_id'"
    ssh_nas "${P}/docker rm -f ${container_name} 2>/dev/null || true"
    # Use a temporary container to delete as root (Docker files are owned by root)
    ssh_nas "${P}/docker run --rm -v '${data_dir}:/cleanup' alpine rm -rf /cleanup/app-data"
    ssh_nas "rm -rf '${data_dir}'"
    _remove_from_registry "$user_id"
    ok "User '$user_id' destroyed"
}

cmd_logs() {
    local user_id="${1:?Usage: manage.sh logs <user_id>}"
    local container_name="${CONTAINER_PREFIX}${user_id}"
    info "Tailing logs for '$user_id' (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs ${container_name} --tail 100 -f"
}

cmd_status() {
    local user_id="${1:?Usage: manage.sh status <user_id>}"
    local container_name="${CONTAINER_PREFIX}${user_id}"
    local port
    port=$(_get_user_port "$user_id")
    if [[ -z "$port" ]]; then
        err "User '$user_id' not found in registry"
        exit 1
    fi
    info "User: $user_id"
    ssh_nas "${P}/docker ps --filter name=${container_name} --format 'table {{.Status}}\t{{.Ports}}'"
    echo ""
    info "Health check:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${port}/health" | python3 -m json.tool 2>/dev/null || err "Cannot reach http://${NAS_HOST}:${port}/health"
    echo ""
    info "Auth status:"
    curl -s --connect-timeout 3 "http://${NAS_HOST}:${port}/auth/status" | python3 -m json.tool 2>/dev/null || err "Cannot reach auth endpoint"
}

cmd_deploy_code() {
    info "Deploying app code to ${SSH_TARGET}:${SHARED_CODE_PATH}"
    rsync -avz --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
        "${PROJECT_ROOT}/app/" \
        "${SSH_TARGET}:${SHARED_CODE_PATH}/"
    ok "Code deployed — all user containers will hot-reload in ~3s"
}

cmd_update() {
    info "Syncing code to ${SSH_TARGET}:${BASE_DEPLOY}"
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
    | ssh_nas "mkdir -p '${BASE_DEPLOY}' && tar -C '${BASE_DEPLOY}' -xzf -"
    ok "Code synced"

    info "Building Docker image (${IMAGE_NAME}:${IMAGE_TAG})"
    ssh_nas "cd '${BASE_DEPLOY}' && ${P}/docker build -f deploy/prod/Dockerfile -t ${IMAGE_NAME}:${IMAGE_TAG} ."
    ok "Image built"

    # Restart all user containers
    _ensure_registry
    info "Restarting all user containers..."
    _read_registry | python3 -c "
import sys,json
d=json.load(sys.stdin)
for uid in sorted(d): print(uid)
" | while read -r uid; do
        local container_name="${CONTAINER_PREFIX}${uid}"
        info "  Restarting ${container_name}..."
        ssh_nas "${P}/docker stop ${container_name} 2>/dev/null; ${P}/docker rm ${container_name} 2>/dev/null || true"
        local port
        port=$(_get_user_port "$uid")
        local data_dir="${BASE_DEPLOY}/data-${uid}"
        ssh_nas "${P}/docker run -d \
            --name ${container_name} \
            --hostname ${container_name} \
            --restart unless-stopped \
            -p ${port}:${port} \
            --ulimit nofile=65536:65536 \
            --env-file '${data_dir}/.env' \
            -v '${data_dir}/app-data:/app/data' \
            -v '${SHARED_CODE_PATH}:/app/app:ro' \
            ${IMAGE_NAME}:${IMAGE_TAG}"
        ok "  ${container_name} restarted on port ${port}"
    done
    echo ""
    ok "All containers updated!"
}

# ── Main ──────────────────────────────────────────────────────

case "${1:-help}" in
    create)          shift; cmd_create "$@" ;;
    list|ls)         cmd_list ;;
    start)           shift; cmd_start "$@" ;;
    stop)            shift; cmd_stop "$@" ;;
    destroy)         shift; cmd_destroy "$@" ;;
    logs)            shift; cmd_logs "$@" ;;
    status)          shift; cmd_status "$@" ;;
    deploy-code|dc)  cmd_deploy_code ;;
    update)          cmd_update ;;
    *)
        echo "TradeAutonom User Manager"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  deploy-code (dc)                 Push app/ to shared folder — all containers hot-reload"
        echo "  create <user_id> [--port PORT]   Create a new user container"
        echo "  list                             List all users + status"
        echo "  start <user_id>                  Start user's container"
        echo "  stop <user_id>                   Stop user's container"
        echo "  destroy <user_id>                Delete user + all data"
        echo "  logs <user_id>                   Tail container logs"
        echo "  status <user_id>                 Show container + health status"
        echo "  update                           Rebuild image + restart all users"
        exit 1
        ;;
esac
