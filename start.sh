#!/bin/bash
# start.sh — Loads .env, starts Lavalink in background, then starts the bot

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Load .env ──────────────────────────────────────────────────────────────────
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
    echo -e "\033[32m[OK]\033[0m .env loaded"
else
    echo -e "\033[33m[WARN]\033[0m .env not found — copy .env.example to .env and fill in your values"
    exit 1
fi

# ── Start Lavalink ─────────────────────────────────────────────────────────────
LAVALINK_DIR="$ROOT/lavalink"
echo -e "\033[36m[1/2]\033[0m Starting Lavalink..."
cd "$LAVALINK_DIR"
java -jar Lavalink.jar &
LAVALINK_PID=$!
cd "$ROOT"

# Wait for Lavalink to be ready (check port 2333)
echo -e "\033[36m      \033[0m Waiting for Lavalink to be ready..."
for i in $(seq 1 30); do
    if command -v nc &> /dev/null; then
        nc -z localhost "${LAVALINK_PORT:-2333}" 2>/dev/null && break
    elif command -v curl &> /dev/null; then
        curl -s "http://localhost:${LAVALINK_PORT:-2333}" >/dev/null 2>&1 && break
    else
        sleep 1
    fi
    sleep 1
done
echo -e "\033[32m[OK]\033[0m Lavalink ready (PID: $LAVALINK_PID)"

# ── Start Bot ──────────────────────────────────────────────────────────────────
echo -e "\033[32m[2/2]\033[0m Starting bot..."
python3 bot.py

# ── Cleanup ────────────────────────────────────────────────────────────────────
echo ""
echo "Bot stopped. Shutting down Lavalink..."
kill $LAVALINK_PID 2>/dev/null
