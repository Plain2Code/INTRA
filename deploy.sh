#!/bin/bash
# INTRA Deploy Script – Oracle Cloud Free Tier
# Usage: ./deploy.sh [demo|live]
# Default: demo

set -e

SERVER="opc@138.2.183.68"
SSH_KEY="~/.ssh/ssh-key-2026-02-12.key"
SSH_OPTS="-i $SSH_KEY -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
MODE="${1:-demo}"
APP_DIR="~/intra"

echo "=== INTRA Deploy → $SERVER ($MODE mode) ==="

# 0. Connectivity check
echo "[0/4] Checking server connectivity..."
if ! ssh $SSH_OPTS "$SERVER" 'echo ok' 2>/dev/null | grep -q ok; then
  echo "ERROR: Cannot reach $SERVER – check VPN/network and try again."
  exit 1
fi

# 1. Copy files to server (exclude .env – must be on server already)
echo "[1/4] Syncing files..."
rsync -avz --progress \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '.git' \
  -e "ssh $SSH_OPTS" \
  . "$SERVER:$APP_DIR/"

# 2. Build and restart on server
echo "[2/4] Building Docker image..."
if [ "$MODE" = "live" ]; then
  CMD="python main.py --live"
else
  CMD="python main.py"
fi

ssh $SSH_OPTS "$SERVER" bash << EOF
  set -e
  cd $APP_DIR

  # Stop + remove old INTRA container if exists
  docker stop intra 2>/dev/null || true
  docker rm   intra 2>/dev/null || true

  # Remove old prio container if still running
  docker stop prio 2>/dev/null || true
  docker rm   prio 2>/dev/null || true

  # Build
  docker build -t intra .

  # Run (dashboard only accessible via SSH tunnel – not exposed to internet)
  docker run -d \
    --name intra \
    --env-file .env \
    -p 127.0.0.1:8080:8080 \
    --restart unless-stopped \
    intra $CMD

  echo "Container started:"
  docker ps | grep intra
EOF

echo ""
echo "[3/4] Done! Access dashboard via SSH tunnel:"
echo ""
echo "  ssh $SSH_OPTS -L 8080:127.0.0.1:8080 $SERVER -N &"
echo "  open http://localhost:8080"
echo ""
echo "[4/4] Useful commands:"
echo "  Logs:    ssh $SSH_OPTS $SERVER 'docker logs -f intra'"
echo "  Stop:    ssh $SSH_OPTS $SERVER 'docker stop intra'"
echo "  Restart: ssh $SSH_OPTS $SERVER 'docker restart intra'"
