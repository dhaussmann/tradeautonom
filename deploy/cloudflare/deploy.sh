#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy-cloudflare.sh — Build frontend + deploy Worker to Cloudflare
#
# Usage:
#   ./deploy.sh              # full: build frontend + deploy worker
#   ./deploy.sh --worker     # deploy worker only (skip frontend build)
#   ./deploy.sh --build      # build frontend only (no deploy)
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
WORKER_DIR="$SCRIPT_DIR"

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

cmd_build_frontend() {
    info "Building frontend..."
    cd "$FRONTEND_DIR"
    npm ci --silent
    npm run build
    ok "Frontend built → $FRONTEND_DIR/dist/"
}

cmd_install_worker_deps() {
    info "Installing worker dependencies..."
    cd "$WORKER_DIR"
    npm ci --silent 2>/dev/null || npm install --silent
    ok "Worker deps installed"
}

cmd_deploy_worker() {
    info "Deploying worker to Cloudflare..."
    cd "$WORKER_DIR"

    # Check that VPC service ID is configured (check actual value, not comments)
    if grep '"service_id"' wrangler.jsonc | grep -q '<YOUR_VPC_SERVICE_ID>'; then
        err "Please set your VPC Service ID in wrangler.jsonc before deploying"
    fi

    npx wrangler deploy
    ok "Worker deployed!"
}

cmd_full() {
    cmd_build_frontend
    cmd_install_worker_deps
    cmd_deploy_worker
}

# ── Main ──────────────────────────────────────────────────────
case "${1:-full}" in
    --worker|-w)   cmd_install_worker_deps; cmd_deploy_worker ;;
    --build|-b)    cmd_build_frontend ;;
    full|"")       cmd_full ;;
    *)
        echo "Usage: $0 [--worker|--build]"
        echo "  (no args)    Full: build frontend + deploy worker"
        echo "  --worker     Deploy worker only (skip frontend build)"
        echo "  --build      Build frontend only (no deploy)"
        exit 1
        ;;
esac
