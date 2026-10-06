#!/bin/sh
set -e

CONFIG_FILE="${ZCODE_PROXY_CONFIG:-/data/config.yaml}"
STORE_DIR="${ZCODE_PROXY_STORE_DIR:-/store}"
CREDENTIALS_FILE="${STORE_DIR}/credentials.json"

# Auto-initialize config if not found
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[ZCode Proxy] Initializing default configuration at $CONFIG_FILE..."
    mkdir -p "$(dirname "$CONFIG_FILE")"
    cp /app/config.example.yaml "$CONFIG_FILE"
fi

if [ "$1" = "serve" ] || [ -z "$1" ]; then
    if [ ! -f "$CREDENTIALS_FILE" ]; then
        echo "======================================================================"
        echo "[ZCode Proxy] No credentials found at $CREDENTIALS_FILE"
        echo ""
        echo "To sign in, choose one of these options:"
        echo "  1. Web Dashboard: Open the dashboard UI and click 'Sign in'"
        echo "  2. CLI: Run this command in another terminal:"
        echo "     docker compose exec proxy bun run src/index.ts auth login zai"
        echo ""
        echo "Waiting for credentials to appear..."
        echo "======================================================================"
        while [ ! -f "$CREDENTIALS_FILE" ]; do
            sleep 3
        done
        echo "[ZCode Proxy] Credentials detected! Starting proxy server..."
    fi
    exec bun --smol run src/index.ts serve "$CONFIG_FILE"
else
    exec "$@"
fi
