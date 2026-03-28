#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# monitor.sh — Remote monitoring for TradeAutonom on Synology NAS
#
# Usage:
#   ./monitor.sh logs              # tail live container logs
#   ./monitor.sh trades [N]        # show last N trade log entries (default 20)
#   ./monitor.sh status            # health + job + position status
#   ./monitor.sh positions         # show open exchange positions
#   ./monitor.sh restart           # restart the container
#   ./monitor.sh errors [N]        # show last N error lines from logs
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load NAS connection from .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    NAS_HOST=$(grep -E '^NAS_HOST=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    NAS_USER=$(grep -E '^NAS_USER=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
    APP_PORT=$(grep -E '^APP_PORT=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
fi

NAS_HOST="${NAS_HOST:?Set NAS_HOST in .env}"
NAS_USER="${NAS_USER:-admin}"
APP_PORT="${APP_PORT:-8002}"
BASE_URL="http://${NAS_HOST}:${APP_PORT}"
SSH_TARGET="${NAS_USER}@${NAS_HOST}"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-o ConnectTimeout=5 -o IdentitiesOnly=yes -i ${SSH_KEY}"

info()  { printf '\033[1;34m▸ %s\033[0m\n' "$*"; }
ok()    { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; }
dim()   { printf '\033[0;37m%s\033[0m\n' "$*"; }

api() { curl -sf --connect-timeout 5 "${BASE_URL}$1" 2>/dev/null; }
P="/usr/local/bin"  # docker lives here on Synology
ssh_nas() { ssh ${SSH_OPTS} "$SSH_TARGET" "$@"; }

# ── Commands ──────────────────────────────────────────────────

cmd_logs() {
    info "Live container logs (Ctrl+C to stop)"
    ssh_nas "${P}/docker logs tradeautonom --tail 100 -f"
}

cmd_trades() {
    local limit="${1:-20}"
    info "Last ${limit} trade log entries"
    api "/jobs/default/log?limit=${limit}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
entries = d.get('entries', [])
if not entries:
    print('  No trades recorded yet.')
    sys.exit(0)
for e in entries:
    ts = e['timestamp'][:19]
    ok = '✔' if e['success'] else '✖'
    act = e['action']
    sp = e.get('spread_at_execution', 0)
    fA = e.get('leg_a_fill_price', '?')
    fB = e.get('leg_b_fill_price', '?')
    err = e.get('error', '')
    color = '\033[32m' if e['success'] else '\033[31m'
    print(f'{color}{ok}\033[0m {ts} {act:5s} spread=\${sp:.4f} A={fA} B={fB}')
    if err:
        print(f'  \033[31m{err[:120]}\033[0m')
" || err "Cannot reach API at ${BASE_URL}"
}

cmd_status() {
    info "Container:"
    ssh_nas "${P}/docker ps --filter name=tradeautonom --format 'table {{.Status}}	{{.Ports}}'" 2>/dev/null || err "SSH failed"
    echo ""

    info "Health:"
    api "/health" | python3 -m json.tool 2>/dev/null || err "Health endpoint unreachable"
    echo ""

    info "Job status:"
    api "/jobs/default" | python3 -c "
import sys, json
j = json.load(sys.stdin)
print(f\"  Status:     {j.get('status','?')}\" )
print(f\"  Auto-trade: {j.get('auto_trade','?')}\")
print(f\"  Position:   {'YES' if j.get('has_position') else 'NO'}\")
if j.get('long_sym'):
    print(f\"  Long:       {j.get('long_sym')}\")
    print(f\"  Short:      {j.get('short_sym')}\")
print(f\"  Spread:     entry>={j.get('spread_entry_low','?')}  exit<={j.get('spread_exit_high','?')}\")
" 2>/dev/null || err "Job API unreachable"
    echo ""

    info "Current spread:"
    api "/jobs/default/check" | python3 -c "
import sys, json
d = json.load(sys.stdin)
s = d.get('snapshot', {})
print(f\"  Spread:  \${s.get('spread_abs',0):.4f}  (mid_a={s.get('mid_price_a','?')} mid_b={s.get('mid_price_b','?')})\")
print(f\"  Action:  {d.get('action','?')}\")
print(f\"  Reason:  {d.get('reason','?')}\")
" 2>/dev/null || err "Spread check unreachable"
}

cmd_positions() {
    info "Exchange positions:"
    api "/account/all" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for acc in data:
    ex = acc.get('exchange', '?').upper()
    eq = acc.get('equity', '?')
    upnl = acc.get('unrealized_pnl', '?')
    print(f'  {ex}: equity=\${eq} uPnL=\${upnl}')
    for p in acc.get('positions', []):
        inst = p.get('instrument', '?')
        size = p.get('size', '?')
        side = p.get('side', '?')
        entry = p.get('entry_price', '?')
        pnl = p.get('unrealized_pnl', '?')
        print(f'    {inst}: {side} {size} @ {entry} uPnL=\${pnl}')
    if not acc.get('positions'):
        print('    No positions')
" 2>/dev/null || err "Cannot reach API at ${BASE_URL}"
}

cmd_errors() {
    local lines="${1:-50}"
    info "Last ${lines} error/warning lines from container logs"
    ssh_nas "${P}/docker logs tradeautonom --tail 500 2>&1 | grep -iE 'ERROR|FAIL|WARNING|exception|traceback' | tail -${lines}"
}

cmd_restart() {
    info "Restarting container..."
    ssh_nas "${P}/docker restart tradeautonom"
    ok "Container restarted"
}

# ── Main ──────────────────────────────────────────────────────

case "${1:-status}" in
    logs|log|-l)         cmd_logs ;;
    trades|trade|-t)     cmd_trades "${2:-20}" ;;
    status|-s)           cmd_status ;;
    positions|pos|-p)    cmd_positions ;;
    errors|err|-e)       cmd_errors "${2:-50}" ;;
    restart|-r)          cmd_restart ;;
    *)
        echo "Usage: $0 <command>"
        echo ""
        echo "  logs              Tail live container logs"
        echo "  trades [N]        Show last N trade entries (default 20)"
        echo "  status            Health + job + spread status"
        echo "  positions         Show exchange positions & equity"
        echo "  errors [N]        Show last N error lines from logs"
        echo "  restart           Restart the container"
        exit 1
        ;;
esac
