#!/bin/bash
# ── Hot-Deploy to NAS ──────────────────────────────────────────
# Usage: ./deploy.sh [file1.py file2.py ...]
#   No args = deploy ALL .py files from app/
#   With args = deploy only specified files
#
# Copies files to the shared code directory on the NAS.
# Uvicorn auto-reload detects changes and restarts the Python process.
# Vault auto-unlocks from persisted session — no manual unlock needed.
#
# Safety: checks all containers for active bots before deploying.

set -euo pipefail

NAS="dhaussmann@192.168.133.253"
NAS_IP="192.168.133.253"
REMOTE_APP="/volume1/docker/tradeautonom/app"
PORTS=(8005 9001 9002 9003 9004 9005 9006)

# ── Idle-Guard: abort if any bot is active ─────────────────────

echo "Checking containers for active bots..."

for port in "${PORTS[@]}"; do
    result=$(curl -sf "http://${NAS_IP}:${port}/health" 2>/dev/null || echo "OFFLINE")
    if [ "$result" = "OFFLINE" ]; then
        echo "  Port ${port}: offline (skipping)"
        continue
    fi

    # Check bot states via /fn/bots endpoint
    active=$(curl -sf "http://${NAS_IP}:${port}/fn/bots" 2>/dev/null | python3 -c "
import sys, json
try:
    bots = json.load(sys.stdin)
    if isinstance(bots, dict):
        bots = bots.get('bots', [])
    active = [b for b in bots if isinstance(b, dict) and b.get('state', 'IDLE').upper() not in ('IDLE', 'OFF', 'HOLDING')]
    if active:
        names = ', '.join(b.get('bot_id', '?') for b in active)
        print(f'BLOCKED: port {port} has active bot(s): {names}')
        sys.exit(1)
    else:
        print('idle')
except Exception as e:
    print('idle')  # If endpoint fails, assume idle (vault locked)
" 2>/dev/null || echo "idle")

    if [[ "$active" == BLOCKED* ]]; then
        echo "  $active"
        echo ""
        echo "ABORTED. Wait for bots to finish or stop them first."
        exit 1
    fi
    echo "  Port ${port}: ${active}"
done

echo ""

# ── Determine files to deploy ──────────────────────────────────

if [ $# -eq 0 ]; then
    echo "No files specified — deploying ALL app/*.py files"
    FILES=(app/*.py)
else
    FILES=()
    for f in "$@"; do
        if [ -f "app/$f" ]; then
            FILES+=("app/$f")
        elif [ -f "$f" ]; then
            FILES+=("$f")
        else
            echo "ERROR: File not found: $f"
            exit 1
        fi
    done
fi

# ── Deploy ─────────────────────────────────────────────────────

echo "Deploying ${#FILES[@]} file(s) to ${NAS}:${REMOTE_APP}/"
echo ""

for f in "${FILES[@]}"; do
    basename=$(basename "$f")
    cat "$f" | ssh "$NAS" "cat > ${REMOTE_APP}/${basename}" 2>/dev/null
    echo "  ✓ ${basename}"
done

echo ""
echo "Done. Uvicorn will auto-reload all containers within ~3 seconds."
echo "Vault sessions persist — no manual unlock needed."
