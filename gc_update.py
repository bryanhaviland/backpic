"""
gc_update.py — Incremental GameChanger update
==============================================
Reads all teams from Supabase, finds the most recent game_date per team,
then scrapes only games that occurred AFTER that cutoff.

Imports the heavy lifting directly from gc_scraper.py so there's no
duplication of parsing / insert logic.

Usage:
    # Update all teams in the DB
    python3 gc_update.py --headless 2>&1 | tee update.log

    # Update specific teams only
    python3 gc_update.py --headless \
        --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \
        --team-id 62gwwZMxWShR --team "CF Flamingos"

    # Dry run (scrape but don't write to DB)
    python3 gc_update.py --headless --dry-run

    # Override cutoff — reprocess games on or after this date
    python3 gc_update.py --headless --since 2026-05-01

Flags:
    --headless          Headless browser
    --team-id ID        Limit to specific GC team ID (repeatable)
    --team NAME         Team name paired with --team-id
    --since YYYY-MM-DD  Override per-team cutoff with a fixed date
    --dry-run           Scrape but don't write to DB
    --timeout N         Navigation timeout ms (default 15000)
    --supabase-url URL  Falls back to SUPABASE_URL env var
    --supabase-key KEY  Falls back to SUPABASE_KEY env var
    --gc-email EMAIL    Falls back to GC_EMAIL env var
    --gc-password PASS  Falls back to GC_PASSWORD env var
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

# ── import everything we need from gc_scraper ───────────────────────────────
# Add the scripts directory to path in case this is run from elsewhere
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gc_scraper import (
    SupabaseClient,
    login,
    scrape_schedule,
    scrape_box_score,
    scrape_plays,
    upsert_game,
    insert_plays,
    insert_game_batting_stats,
    insert_game_pitching_stats,
    insert_game_catching_stats,
    insert_game_innings_played,
    infer_fielding_from_lineup_and_plays,
    get_team_slug,
    is_stats_locked,
    _load_game_stats_page,
    scrape_game_catching,
    scrape_game_innings,
    get_team_name_from_db,
    _parse_game_date,
    _load_box_score_page,
    _extract_date_from_lines,
)

from playwright.sync_api import sync_playwright


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Incremental GC update")
    p.add_argument("--headless",      action="store_true")
    p.add_argument("--team-id",       dest="team_ids",   action="append", default=[])
    p.add_argument("--team",          dest="team_names", action="append", default=[])
    p.add_argument("--since",         default=None,
                   help="Override cutoff date YYYY-MM-DD (apply to all teams)")
    p.add_argument("--dry-run",       action="store_true")
    p.add_argument("--dates-only",    action="store_true",
                   help="Only backfill missing game_date from schedule (no box scores)")
    p.add_argument("--dates-from-boxscores", action="store_true",
                   help="Backfill missing game_date by visiting each box score page (no stats)")
    p.add_argument("--timeout",       type=int, default=15000)
    p.add_argument("--supabase-url",  default=os.getenv("SUPABASE_URL"))
    p.add_argument("--supabase-key",  default=os.getenv("SUPABASE_KEY"))
    p.add_argument("--gc-email",      default=os.getenv("GC_EMAIL"))
    p.add_argument("--gc-password",   default=os.getenv("GC_PASSWORD"))
    return p.parse_args()


# ── Supabase helpers ─────────────────────────────────────────────────────────

def get_all_teams(sb: SupabaseClient) -> list[dict]:
    """Return all teams from DB as list of {id, gc_team_id, name}."""
    import requests as _req
    resp = _req.get(
        f"{sb.base}/teams",
        params={"select": "id,gc_team_id,name", "order": "name.asc"},
        headers=sb.headers,
    )
    resp.raise_for_status()
    return resp.json()


def get_team_cutoff(sb: SupabaseClient, db_team_id: int) -> Optional[date]:
    """
    Return the most recent game_date for this team.
    Returns None if no games with game_date exist yet.
    """
    import requests as _req
    resp = _req.get(
        f"{sb.base}/games",
        params={
            "select":    "game_date",
            "team_id":   f"eq.{db_team_id}",
            "game_date": "not.is.null",
            "order":     "game_date.desc",
            "limit":     "1",
        },
        headers=sb.headers,
    )
    resp.raise_for_status()
    rows = resp.json()
    if rows and rows[0].get("game_date"):
        return date.fromisoformat(rows[0]["game_date"])
    return None


# ── Per-game processing ──────────────────────────────────────────────────────

def process_game(sb, page, db_team_id, gc_team_id, team_name, g,
                 team_slug, stats_locked, timeout, dry_run):
    """Scrape one game and write stats to DB. Returns True on success."""
    event_id     = g["gc_event_id"]
    scouted_home = g.get("home_away") == "home"
    print(f"  [game] {event_id}  {g.get('game_date') or g.get('date', '')}  "
          f"{g.get('result','')} {g.get('runs_scored')}-{g.get('runs_allowed')}  "
          f"vs {g.get('opponent','')}")

    # Box score
    bs = scrape_box_score(page, gc_team_id, team_slug, event_id, team_name, timeout)

    # Play-by-play
    plays, _, pitcher_info, play_lines = scrape_plays(
        page, gc_team_id, event_id, scouted_home, timeout
    )
    if pitcher_info.get("starting_pitcher"):
        g["starting_pitcher"] = pitcher_info["starting_pitcher"]

    # Use precise date from box score header if available
    if bs and bs.get("game_date_raw"):
        g["full_date_raw"] = bs["game_date_raw"]

    if dry_run:
        print(f"    [dry-run] bs={'yes' if bs else 'no'}  "
              f"plays={len(plays)}  play_lines={len(play_lines)}")
        return True

    db_game_id = upsert_game(sb, db_team_id, g)

    if plays:
        insert_plays(sb, db_team_id, db_game_id, plays)

    if bs:
        insert_game_batting_stats(sb, db_team_id, db_game_id, event_id, bs)
        insert_game_pitching_stats(sb, db_team_id, db_game_id, event_id, bs)

    # Catching + innings played
    catching, innings = [], []
    if bs and play_lines:
        scouted_players = bs.get("scouted", {}).get("batting", [])
        catching, innings = infer_fielding_from_lineup_and_plays(
            scouted_players, play_lines, scouted_home
        )

    if not catching and not stats_locked:
        gs_loaded = _load_game_stats_page(page, gc_team_id, event_id, timeout, team_slug)
        if gs_loaded:
            catching = scrape_game_catching(page, gc_team_id, event_id, True, timeout, team_slug)
            innings  = scrape_game_innings(page, gc_team_id, event_id, True, timeout, team_slug)

    if catching:
        insert_game_catching_stats(sb, db_team_id, db_game_id, event_id, catching)
    if innings:
        insert_game_innings_played(sb, db_team_id, db_game_id, event_id, innings)

    return True


# ── Date-only backfill ───────────────────────────────────────────────────────

def backfill_dates(sb, page, db_team_id, gc_team_id, team_name, timeout):
    """Scrape schedule only and patch game_date for any games missing it."""
    import requests as _req
    print(f"\n[dates] {team_name}  ({gc_team_id})")

    all_games = scrape_schedule(page, gc_team_id, timeout)
    today = date.today()
    updated = skipped = 0

    for g in all_games:
        gd_str = _parse_game_date(g.get("full_date_raw", ""), g.get("date", ""))
        if not gd_str:
            skipped += 1
            continue
        if date.fromisoformat(gd_str) > today:
            continue

        resp = _req.patch(
            f"{sb.base}/games",
            params={
                "team_id":    f"eq.{db_team_id}",
                "gc_event_id": f"eq.{g['gc_event_id']}",
                "game_date":  "is.null",
            },
            headers={**sb.headers, "Prefer": "return=representation"},
            json={"game_date": gd_str},
        )
        if resp.ok and resp.json():
            updated += 1

    print(f"  updated={updated}  no-date-available={skipped}")
    return updated


def backfill_dates_from_boxscores(sb, page, db_team_id, gc_team_id, team_name, timeout):
    """Visit each undated game's box score, extract the date, and patch it."""
    import requests as _req
    print(f"\n[dates-bs] {team_name}  ({gc_team_id})")

    # Fetch all games missing a date for this team
    resp = _req.get(
        f"{sb.base}/games",
        params={
            "select":    "id,gc_event_id",
            "team_id":   f"eq.{db_team_id}",
            "game_date": "is.null",
        },
        headers=sb.headers,
    )
    resp.raise_for_status()
    undated = resp.json()
    print(f"  {len(undated)} games missing date")

    if not undated:
        return 0

    team_slug = get_team_slug(page, gc_team_id, timeout)
    updated = failed = 0

    for g in undated:
        event_id = g["gc_event_id"]
        try:
            lines = _load_box_score_page(page, gc_team_id, team_slug, event_id, timeout)
            date_raw = _extract_date_from_lines(lines) if lines else ""
            if not date_raw:
                failed += 1
                continue
            gd_str = _parse_game_date(date_raw)
            if not gd_str:
                failed += 1
                continue
            patch = _req.patch(
                f"{sb.base}/games",
                params={"id": f"eq.{g['id']}"},
                headers={**sb.headers, "Prefer": "return=representation"},
                json={"game_date": gd_str},
            )
            if patch.ok:
                updated += 1
                print(f"  ✓ {event_id} → {gd_str}")
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ {event_id}: {e}")
            failed += 1
        time.sleep(2)

    print(f"  updated={updated}  failed/no-date={failed}")
    return updated


# ── Per-team update ──────────────────────────────────────────────────────────

def update_team(sb, page, db_team_id, gc_team_id, team_name,
                global_since, timeout, dry_run):
    print(f"\n{'='*60}")
    print(f"TEAM: {team_name}  ({gc_team_id})")
    print(f"{'='*60}")

    # Determine cutoff
    if global_since:
        cutoff = global_since
        print(f"[cutoff] Using --since override: {cutoff}")
    else:
        cutoff = get_team_cutoff(sb, db_team_id)
        if cutoff:
            print(f"[cutoff] Last game_date in DB: {cutoff}")
        else:
            print("[cutoff] No game_date history — will scrape all games")

    team_slug    = get_team_slug(page, gc_team_id, timeout)
    stats_locked = is_stats_locked(page, gc_team_id, timeout)
    print(f"[stats] {'LOCKED — box scores only' if stats_locked else 'Admin access'}")

    # Fetch full schedule
    all_games = scrape_schedule(page, gc_team_id, timeout)
    print(f"[schedule] {len(all_games)} total games found")

    # Parse game_date for each and filter to new games only
    new_games = []
    today = date.today()
    for g in all_games:
        gd_str = _parse_game_date(
            g.get("full_date_raw", ""),
            g.get("date", ""),
        )
        g["game_date"] = gd_str   # attach so upsert_game can use it

        if not gd_str:
            # Can't determine date — include if no cutoff set
            if cutoff is None:
                new_games.append(g)
            continue

        gd = date.fromisoformat(gd_str)

        # Skip future games (not played yet)
        if gd > today:
            continue

        # Apply cutoff — strictly after the last known date
        if cutoff is None or gd > cutoff:
            new_games.append(g)

    print(f"[filter] {len(new_games)} new game(s) to process "
          f"(cutoff: {cutoff or 'none'})")

    if not new_games:
        print("[skip] Team is up to date.")
        return {"team": team_name, "new_games": 0}

    processed = 0
    errors    = 0
    for g in new_games:
        try:
            process_game(sb, page, db_team_id, gc_team_id, team_name,
                         g, team_slug, stats_locked, timeout, dry_run)
            processed += 1
        except Exception as e:
            print(f"  [ERROR] game {g.get('gc_event_id')}: {e}")
            errors += 1
        time.sleep(1)

    print(f"[done] {processed} processed, {errors} errors")
    return {"team": team_name, "new_games": processed, "errors": errors}


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if not args.supabase_url or not args.supabase_key:
        print("ERROR: Set SUPABASE_URL and SUPABASE_KEY (or pass --supabase-url/--supabase-key)")
        sys.exit(1)

    global_since = None
    if args.since:
        try:
            global_since = date.fromisoformat(args.since)
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD, got: {args.since}")
            sys.exit(1)

    sb = SupabaseClient(args.supabase_url, args.supabase_key)

    # Build team list: from CLI flags or all teams in DB
    if args.team_ids:
        import requests as _req
        team_list = []
        for i, gc_id in enumerate(args.team_ids):
            resp = _req.get(
                f"{sb.base}/teams",
                params={"select": "id,gc_team_id,name", "gc_team_id": f"eq.{gc_id}"},
                headers=sb.headers,
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                print(f"[warn] gc_team_id '{gc_id}' not found in DB — skipping")
                continue
            row  = rows[0]
            name = (args.team_names[i] if i < len(args.team_names) else None) or row["name"]
            team_list.append((row["id"], gc_id, name))
    else:
        db_teams  = get_all_teams(sb)
        team_list = [(t["id"], t["gc_team_id"], t["name"]) for t in db_teams]

    print(f"[run] {len(team_list)} team(s) queued")

    # Reuse saved auth state from gc_boxscore_patch.py / gc_scraper.py if present
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gc_state.json")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        if os.path.exists(state_file):
            print(f"[auth] Loading saved session from {state_file}")
            context = browser.new_context(
                storage_state=state_file,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
        else:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page    = context.new_page()
            login(page, args.timeout,
                  email=args.gc_email, password=args.gc_password)

        summaries = []
        for db_team_id, gc_team_id, team_name in team_list:
            try:
                if args.dates_only:
                    n = backfill_dates(sb, page, db_team_id, gc_team_id,
                                       team_name, args.timeout)
                    summaries.append({"team": team_name, "new_games": n})
                elif args.dates_from_boxscores:
                    n = backfill_dates_from_boxscores(sb, page, db_team_id, gc_team_id,
                                                      team_name, args.timeout)
                    summaries.append({"team": team_name, "new_games": n})
                else:
                    s = update_team(sb, page, db_team_id, gc_team_id, team_name,
                                    global_since, args.timeout, args.dry_run)
                    summaries.append(s)
            except Exception as e:
                print(f"[ERROR] {team_name} ({gc_team_id}): {e}")
                summaries.append({"team": team_name, "error": str(e)})

        browser.close()

    # Summary
    print(f"\n{'='*60}")
    print("UPDATE COMPLETE")
    print(f"{'='*60}")
    total_new = sum(s.get("new_games", 0) for s in summaries)
    total_err = sum(s.get("errors", 0) for s in summaries)
    label = "dates patched" if (args.dates_only or args.dates_from_boxscores) else "new games"
    for s in summaries:
        if "error" in s:
            print(f"  {s['team']}: ERROR — {s['error']}")
        else:
            print(f"  {s['team']}: {s.get('new_games', 0)} {label}")
    print(f"\nTotal: {total_new} {label}, {total_err} errors")


if __name__ == "__main__":
    main()
