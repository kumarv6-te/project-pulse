#!/bin/bash
# ProjectPulse – Run Jira + Slack ingest (for cron)
# Usage: run_ingest_cron.sh
# Set env vars (JIRA_*, SLACK_BOT_TOKEN, DB_PATH, etc.) before running, or use .env

# Cron runs with minimal PATH; ensure common locations
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Load .env if present
[ -f .env ] && set -a && source .env 2>/dev/null && set +a

# Activate venv if present (cron has no venv in PATH)
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

export INCREMENTAL=1
export DB_PATH="${DB_PATH:-$SCRIPT_DIR/projectpulse_demo.db}"

LOG="${LOG_PATH:-$SCRIPT_DIR/ingest.log}"
PYTHON="${PYTHON:-python3}"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  $PYTHON jira_ingest_from_db.py 2>&1 || true
  echo "---"
  $PYTHON slack_ingest_from_db.py 2>&1 || true
  echo "---"
  $PYTHON generate_status_snapshots.py 2>&1 || true
  echo ""
} >> "$LOG" 2>&1
