#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${1:-8787}"
exec python3 scripts/dashboard_server.py --share-lan --port "$PORT"
