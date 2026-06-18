# GC Scripts — Command Reference

---

## gc_boxscore_patch.py

Patches `game_batting_stats`, `game_pitching_stats`, and `game_catching_stats` for teams already in Supabase.
If no `--team-id` is given, runs against **all teams** in the DB.

| Flag | Description |
|------|-------------|
| `--headless` | Run browser without a visible window (required for background runs) |
| `--team-id ID` | Target a specific team by GC ID (repeat for multiple) |
| `--team NAME` | Team name paired with each `--team-id` (must match count) |
| `--limit N` | Max games to process per team (default: all) |
| `--dry-run` | Scrape but don't write anything to Supabase |
| `--force` | Re-patch games that already have batting stats |
| `--catching-only` | Only patch games that have batting but are missing catching stats |

**Examples:**
```bash
# All teams, headless
python3 gc_boxscore_patch.py --headless 2>&1 | tee boxscore_patch.log

# Specific teams
python3 gc_boxscore_patch.py --headless \
  --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \
  --team-id 62gwwZMxWShR --team "CF Flamingos"

# Backfill catching stats for games that already have batting
python3 gc_boxscore_patch.py --headless --catching-only 2>&1 | tee catching_patch.log

# Test without writing to DB
python3 gc_boxscore_patch.py --headless --dry-run --limit 2

# Force re-patch everything
python3 gc_boxscore_patch.py --headless --force
```

---

## gc_update.py

Incremental updater — reads all teams from Supabase, finds the most recent `game_date` per team, and scrapes only games that occurred after that date. Imports all parsing logic from `gc_scraper.py`.

| Flag | Description |
|------|-------------|
| `--headless` | Headless browser |
| `--team-id ID` | Limit to specific team(s) by GC ID (repeat for multiple) |
| `--team NAME` | Team name paired with `--team-id` |
| `--since YYYY-MM-DD` | Override per-team cutoff with a fixed date for all teams |
| `--dry-run` | Scrape but don't write to DB |
| `--timeout N` | Nav timeout in ms (default: 15000) |
| `--supabase-url URL` | Falls back to `SUPABASE_URL` env var |
| `--supabase-key KEY` | Falls back to `SUPABASE_KEY` env var |
| `--gc-email EMAIL` | Falls back to `GC_EMAIL` env var |
| `--gc-password PASS` | Falls back to `GC_PASSWORD` env var |

**Examples:**
```bash
# Update all teams (auto-detects cutoff per team)
python3 gc_update.py --headless 2>&1 | tee update.log

# Update specific teams only
python3 gc_update.py --headless \
  --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \
  --team-id 62gwwZMxWShR --team "CF Flamingos"

# Force reprocess everything since May 1
python3 gc_update.py --headless --since 2026-05-01

# Test without writing to DB
python3 gc_update.py --headless --dry-run
```

---

## gc_scraper.py

Main scraper — builds teams, games, plays, and all stats from scratch.

| Flag | Description |
|------|-------------|
| `--team-id ID` | GC team ID to scrape (repeat for multiple) |
| `--team NAME` | Team name paired with `--team-id` |
| `--sport` | `softball` or `baseball` (default: `softball`) |
| `--headless` | Headless browser mode |
| `--timeout N` | Nav timeout in milliseconds (default: `15000`) |
| `--supabase-url URL` | Supabase URL (falls back to `SUPABASE_URL` env var) |
| `--supabase-key KEY` | Supabase key (falls back to `SUPABASE_KEY` env var) |
| `--gc-email EMAIL` | GC login email (falls back to `GC_EMAIL` env var) |
| `--gc-password PASS` | GC login password (falls back to `GC_PASSWORD` env var) |
| `--division` | Filter by division, e.g. `10U`, `12U` |
| `--state` | Filter by state, e.g. `FL`, `TX` |
| `--season` | Filter by season, e.g. `Spring 2026` |

**Examples:**
```bash
# Scrape a single team
python3 gc_scraper.py \
  --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \
  --headless

# Scrape multiple teams
python3 gc_scraper.py \
  --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \
  --team-id 62gwwZMxWShR --team "CF Flamingos" \
  --headless 2>&1 | tee scraper.log
```

---

## gc_team_finder.py

Scrapes a team's schedule to find opponent names and GC team IDs.
Outputs to `gc_teams_found.csv`.

| Flag | Description |
|------|-------------|
| `--team-id ID` | Seed GC team ID (repeat for multiple) |
| `--file FILE` | Text file with one team ID per line |
| `--no-headless` | Show the browser window (default is headless) |

**Examples:**
```bash
# Find all opponents for one team
python3 gc_team_finder.py --team-id mDCClxcC1LQs

# Multiple seed teams
python3 gc_team_finder.py --team-id mDCClxcC1LQs --team-id 62gwwZMxWShR

# From a file
python3 gc_team_finder.py --file team_ids.txt

# With visible browser for debugging
python3 gc_team_finder.py --team-id mDCClxcC1LQs --no-headless
```

---

## Environment Variables

All scripts read credentials from env vars if not passed as flags:

| Variable | Used By | Description |
|----------|---------|-------------|
| `SUPABASE_URL` | scraper, patch | e.g. `https://fzndpqbjmhwwouxktmol.supabase.co` |
| `SUPABASE_KEY` | scraper, patch | Service-role or anon key |
| `GC_EMAIL` | scraper | GameChanger login email |
| `GC_PASSWORD` | scraper | GameChanger login password |
