#!/bin/bash
# ProjectPulse – Run Jira + Slack ingest (for cron)
# Usage: run_ingest_cron.sh
# Set env vars (JIRA_*, SLACK_BOT_TOKEN, DB_PATH, etc.) before running, or use .env

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
[ -f .env ] && set -a && source .env && set +a

# Activate venv if present
[ -f .venv/bin/activate ] && source .venv/bin/activate

export INCREMENTAL=1
export DB_PATH="${DB_PATH:-./projectpulse_demo.db}"

LOG="${LOG_PATH:-$SCRIPT_DIR/ingest.log}"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  python jira_ingest_from_db.py 2>&1
  echo "---"
  python slack_ingest_from_db.py 2>&1
  echo "---"
  python generate_status_snapshots.py 2>&1
  echo ""
} >> "$LOG" 2>&1
