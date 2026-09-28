#!/bin/bash
# gc_weekend_scrape.sh
# Runs every Saturday night — scrapes all teams for games played that day.
# Crontab entry: 0 23 * * 6 /Users/bryanhaviland/backpic-scouting-v2/gc_weekend_scrape.sh
# Manual catchup: ./gc_weekend_scrape.sh 2026-06-14

# ── Pull latest code from GitHub ───────────────────────────────────────────
cd "$(dirname "${BASH_SOURCE[0]}")" && git pull origin main

# ── Credentials (loaded from .env — never commit that file) ────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "ERROR: .env file not found at $SCRIPT_DIR/.env" >&2
    exit 1
fi

# ── Date: first arg overrides (e.g. ./gc_weekend_scrape.sh 2026-06-14) ─────
# Defaults to 8 days back so the weekly run always has overlap buffer.
SINCE_DATE=${1:-$(date -v-8d '+%Y-%m-%d')}

# ── Run ────────────────────────────────────────────────────────────────────
LOG="$SCRIPT_DIR/scrape_run.log"
echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "Run started: $(date)" >> "$LOG"
echo "Since date: $SINCE_DATE" >> "$LOG"
echo "========================================" >> "$LOG"

/opt/homebrew/bin/python3 -u \
  "$SCRIPT_DIR/gc_scraper.py" \
  --headless \
  --all-teams \
  --since-date "$SINCE_DATE" \
  2>&1 | tee -a "$LOG"

# ── Opponents' real records for strength of schedule (Scout a Tourney) ──
echo "Opponent records started: $(date)" >> "$LOG"
/opt/homebrew/bin/python3 -u \
  "$SCRIPT_DIR/gc_opponent_records.py" \
  --all-teams --season "Fall 2026" --max-age-hours 24 \
  2>&1 | tee -a "$LOG"

# ── Rebuild season records/aggregates so the app shows the new games now ──
echo "Refreshing aggregates: $(date)" >> "$LOG"
/opt/homebrew/bin/python3 - <<'PY' 2>&1 | tee -a "$LOG"
import os, requests
url = os.environ["SUPABASE_URL"]
key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
r = requests.post(f"{url}/rest/v1/rpc/exec_sql",
                  headers={"apikey": key, "Authorization": "Bearer " + key},
                  json={"query_text": "select refresh_aggregate_stats()"}, timeout=300)
print("refresh_aggregate_stats:", r.status_code)
PY

echo "Run finished: $(date)" >> "$LOG"
