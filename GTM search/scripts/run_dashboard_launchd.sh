#!/bin/zsh
set -u

PROJECT_DIR="/Users/WIll/Desktop/CODEX/tool/GTM search"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 78

{
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') starting dashboard ----"
  echo "cwd=$(pwd)"
  echo "python=$(/usr/bin/python3 -c 'import sys; print(sys.executable)')"
} >> "$LOG_DIR/dashboard_launchd_boot.log" 2>&1

exec /usr/bin/python3 "$PROJECT_DIR/scripts/dashboard_server.py" --share-lan --port 8787
