#!/usr/bin/env python3
"""
Re-derive catching + innings-played for games where the box score lists more than
one catcher but we stored fewer catcher rows (catcher changes were never logged in
the web play-by-play, so the starting catcher got every inning/SB/PB).

Uses the fielding-text inference in gc_scraper.infer_fielding_from_lineup_and_plays.
Box-score positions come from game_batting_stats.positions (already in the DB);
only the plays page is re-fetched (through real Chrome, same as the scraper).

  python3 gc_fix_fielding.py --season "Fall 2026" --dry-run --limit 3
  python3 gc_fix_fielding.py --season "Fall 2026"                 # write
  python3 gc_fix_fielding.py --event c2f28689-7918-457a-80ac-5d1d61371f7c --dry-run
"""
import argparse
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gc_scraper as gc  # noqa: E402  (loads .env)


def rest_get(sb, path, params):
    out, offset = [], 0
    while True:
        p = dict(params)
        p["limit"] = "1000"
        p["offset"] = str(offset)
        r = requests.get(f"{sb.base}/{path}", headers=sb.headers, params=p)
        r.raise_for_status()
        rows = r.json()
        out.extend(rows)
        if len(rows) < 1000:
            return out
        offset += 1000


def has_c(positions):
    return any(x.strip().upper() == "C" for x in (positions or "").split(","))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="Fall 2026")
    ap.add_argument("--event", default=None, help="single gc_event_id (skips the target scan)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=25000)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    sb = gc.SupabaseClient(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    sb.ping()

    games = rest_get(sb, "games", {
        "select": "id,team_id,gc_event_id,home_away,game_date,opponent",
        "season": f"eq.{args.season}",
    })
    gby = {g["id"]: g for g in games}
    teams = {t["id"]: t for t in rest_get(sb, "teams", {"select": "id,gc_team_id,name"})}

    bat = rest_get(sb, "game_batting_stats", {
        "select": "game_id,team_id,player_name,player_num,positions,games!inner(season)",
        "games.season": f"eq.{args.season}",
    })
    cat = rest_get(sb, "game_catching_stats", {
        "select": "game_id,team_id,inn,games!inner(season)",
        "games.season": f"eq.{args.season}",
    })

    lineup = {}
    for r in bat:
        lineup.setdefault((r["game_id"], r["team_id"]), []).append(r)
    catch_rows = {}
    for r in cat:
        if (r.get("inn") or 0) > 0:
            catch_rows[(r["game_id"], r["team_id"])] = catch_rows.get((r["game_id"], r["team_id"]), 0) + 1

    targets = []
    for (gid, tid), rows in lineup.items():
        g = gby.get(gid)
        if not g:
            continue
        if args.event and g["gc_event_id"] != args.event:
            continue
        n_c = sum(1 for r in rows if has_c(r.get("positions")))
        if args.event or (n_c > 1 and catch_rows.get((gid, tid), 0) < n_c):
            targets.append((g, rows, n_c))
    targets.sort(key=lambda x: (x[0].get("game_date") or "", x[0]["id"]), reverse=True)
    if args.limit:
        targets = targets[: args.limit]
    print(f"[fix] {len(targets)} games to process (dry_run={args.dry_run})")

    changed = 0
    for i, (g, rows, n_c) in enumerate(targets, 1):
        team = teams.get(g["team_id"], {})
        gc_team_id = team.get("gc_team_id")
        label = f"{g.get('game_date')} {team.get('name')} vs {g.get('opponent')} [{g['gc_event_id'][:8]}]"
        print(f"\n[fix] ({i}/{len(targets)}) {label}  box-score catchers={n_c}")
        if not gc_team_id:
            print("[fix]   no gc_team_id — skipped")
            continue

        url = f"https://web.gc.com/teams/{gc_team_id}/schedule/{g['gc_event_id']}/plays"
        ready_js = ("document.body.innerText.includes('Top ') || "
                    "document.body.innerText.includes('Bottom ') || "
                    "document.body.innerText.includes(\"doesn't exist\")")
        try:
            raw = gc.fetch_page_text_via_real_chrome(url, ready_js, timeout=max(20, args.timeout // 1000))
        except gc.GCSignedOutError as e:
            print(f"[fix] SIGNED OUT — stopping: {e}")
            sys.exit(2)
        except Exception as e:
            print(f"[fix]   fetch failed: {e}")
            continue
        lines = gc._lines_from_text(raw)
        if not lines:
            print("[fix]   no plays text — skipped")
            continue

        scouted = [{
            "name": r["player_name"], "num": r.get("player_num") or "",
            "positions": [x.strip() for x in (r.get("positions") or "").split(",") if x.strip()],
        } for r in rows]
        catching, innings = gc.infer_fielding_from_lineup_and_plays(
            scouted, lines, g.get("home_away") == "home")
        if not catching and not innings:
            print("[fix]   inference empty — left unchanged")
            continue

        summary = ", ".join(f"{c['player_name']} {c['inn']:g}inn/{c['pb']}pb/{c['sb']}sb/{c['cs']}cs" for c in catching)
        print(f"[fix]   NEW: {summary}")
        if not args.dry_run:
            gc.insert_game_catching_stats(sb, g["team_id"], g["id"], g["gc_event_id"], catching)
            gc.insert_game_innings_played(sb, g["team_id"], g["id"], g["gc_event_id"], innings)
            changed += 1
        time.sleep(args.sleep)

    print(f"\n[fix] done — {changed} games rewritten" + (" (dry run, nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
