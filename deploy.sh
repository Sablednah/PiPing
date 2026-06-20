#!/usr/bin/env bash
# deploy.sh - push the Pi-side app from your dev machine to the Pi over SSH.
#
# Usage:  ./deploy.sh [pi-host]
# Default host is sable@pi3-touch. Override:  ./deploy.sh sable@192.168.1.50
#
# Deploys only the pi/ folder (the agent/status.php goes to your web hosts
# separately, not to the Pi).

set -euo pipefail

PI_HOST="${1:-sable@192.168.6.130}"
DEST="/home/sable/pi-status-panel/pi/"

echo "Deploying to ${PI_HOST}:${DEST}"

rsync -avz --delete \
  --exclude '__pycache__' \
  --exclude 'config.json' \
  --exclude 'history.json' \
  --exclude 'notif_state.json' \
  "$(dirname "$0")/pi/" \
  "${PI_HOST}:${DEST}"

echo "Restarting service…"
ssh "${PI_HOST}" "sudo systemctl restart monitor.service && sleep 1 && systemctl --no-pager --lines=5 status monitor.service" || {
  echo "Service not installed yet? See CLAUDE.md 'First install' steps."
}

echo "Done."
