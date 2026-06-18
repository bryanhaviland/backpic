#!/bin/bash
# gc_weekend_scrape.sh
# Runs every Saturday night — scrapes all teams for games played that day.
# Crontab entry: 0 23 * * 6 /Users/bryanhaviland/Documents/scripts/gc_weekend_scrape.sh
# Manual catchup: ./gc_weekend_scrape.sh 2026-06-14

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
SINCE_DATE=${1:-$(date '+%Y-%m-%d')}

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

echo "Run finished: $(date)" >> "$LOG"
