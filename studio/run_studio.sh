#!/usr/bin/env bash
# Vectorizer OS - Test Studio Launcher
cd "$(dirname "$0")"
PORT="${1:-8088}"
echo "Starting Vectorizer OS Studio on port $PORT..."
python3 server.py --port "$PORT"
