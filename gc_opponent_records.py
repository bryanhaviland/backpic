#!/usr/bin/env python3
"""
gc_opponent_records.py

Strength-of-schedule feeder. For each seed team, reads its GameChanger schedule,
finds every opponent's own GameChanger team page, and stores that opponent's real
season record in Supabase `opponent_records` (keyed by the opponent name exactly as
it appears on our schedules + season). BackPic's Scout a Tourney uses these records
for strength of schedule instead of only the games we happen to track.

Usage:
  # Specific seed teams (GC team IDs)
  python3 gc_opponent_records.py --team-id FutRbF9Dwpf4 --team-id ltfCe1Cg2xv4

  # Every tracked team with games in the given season
  python3 gc_opponent_records.py --all-teams --season "Fall 2026"

  # Only refresh records older than N hours (default 12)
  python3 gc_opponent_records.py --all-teams --max-age-hours 24

Needs .env (SUPABASE_URL, SUPABASE_KEY, GC_EMAIL, GC_PASSWORD, GMAIL_APP_PASSWORD).
Opens a visible Chrome window (GameChanger search requires a signed-in session and
blocks headless/bundled Chromium).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gc_scraper as g  # noqa: E402  (loads .env, login(), SupabaseClient)
from playwright.sync_api import sync_playwright  # noqa: E402

API = "https://api.team-manager.gc.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def public_get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tokens(name):
    return [w for w in re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split()
            if w and not re.fullmatch(r"\d{1,2}u", w) and w not in ("softball", "fastpitch", "the")]


def age(name):
    m = re.search(r"\b(\d{1,2})\s*u\b", str(name), re.I)
    if not m:
        return None
    a = int(m.group(1))
    return a + 1 if a % 2 else a


def same_team(a, b):
    ta, tb = tokens(a), set(tokens(b))
    if not ta or not tb:
        return False
    return all(w in tb for w in ta) or (len(tb) >= 2 and all(w in set(ta) for w in tb))


def season_label(season_obj):
    """{'name':'fall','year':2026} -> 'Fall 2026'"""
    if not season_obj:
        return None
    return f"{str(season_obj.get('name', '')).title()} {season_obj.get('year', '')}".strip()


class Searcher:
    """Runs GameChanger team searches through the signed-in web UI and captures the API hits."""

    def __init__(self, page):
        self.page = page
        self._hits = None
        page.on("response", self._on_resp)

    def _on_resp(self, r):
        if r.request.method == "POST" and "/search?" in r.url and "api.team-manager.gc.com" in r.url:
            try:
                self._hits = r.json().get("hits", [])
            except Exception:
                self._hits = []

    def search(self, name, timeout=20):
        self._hits = None
        self.page.goto("https://web.gc.com/search?search=" + urllib.parse.quote(name), timeout=60000)
        t0 = time.time()
        while self._hits is None and time.time() - t0 < timeout:
            self.page.wait_for_timeout(500)
        if self._hits is None:  # URL param didn't trigger — type into the box
            try:
                box = self.page.locator("input").first
                box.fill("")
                box.type(name, delay=40)
                self.page.keyboard.press("Enter")
                t0 = time.time()
                while self._hits is None and time.time() - t0 < timeout:
                    self.page.wait_for_timeout(500)
            except Exception as e:
                log(f"  search box error: {e}")
        return [h["result"] for h in (self._hits or []) if h.get("type") == "team"]


def resolve_opponent(searcher, opp_name, seed_name, game_dates, season, state_pref="FL"):
    """Find the opponent's GC team: same season, softball, name match; verify via its schedule."""
    want_season = season.lower()
    cands = []
    for r in searcher.search(opp_name):
        if str(r.get("sport", "")).lower() != "softball":
            continue
        if (season_label(r.get("season")) or "").lower() != want_season:
            continue
        if not same_team(r.get("name", ""), opp_name) and not same_team(opp_name, r.get("name", "")):
            continue
        oa, ra = age(opp_name), age(r.get("name", ""))
        if oa and ra and oa != ra:
            continue
        cands.append(r)
    if not cands:
        return None, "not_found"
    # Verify: the right team has our seed team on its schedule on one of the same dates
    for r in sorted(cands, key=lambda x: (x.get("location", {}).get("state") != state_pref,
                                          x.get("name", "").lower() != opp_name.lower())):
        try:
            games = public_get(f"/public/teams/{r['public_id']}/games")
        except Exception:
            continue
        for gm in games:
            d = str(gm.get("start_ts", ""))[:10]
            if d in game_dates and same_team(seed_name, gm.get("opponent_team", {}).get("name", "")):
                return r, "verified_schedule"
    fl = [c for c in cands if c.get("location", {}).get("state") == state_pref]
    if len(fl) == 1:
        return fl[0], "single_fl_match"
    if len(cands) == 1:
        return cands[0], "single_match"
    return None, f"ambiguous_{len(cands)}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team-id", action="append", dest="team_ids", default=[], help="Seed GC team ID (repeatable)")
    ap.add_argument("--all-teams", action="store_true", help="Seed with every tracked team that has games this season")
    ap.add_argument("--season", default="Fall 2026")
    ap.add_argument("--max-age-hours", type=float, default=12, help="Skip opponents refreshed more recently than this")
    ap.add_argument("--timeout", type=int, default=60000)
    args = ap.parse_args()

    sb = g.SupabaseClient(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    seeds = list(args.team_ids)
    if args.all_teams:
        rows = sb.select("games", {"season": args.season}, columns="team_id")
        tids = sorted({r["team_id"] for r in rows})
        teams = sb.select("teams", {}, columns="id,gc_team_id")
        seeds += [t["gc_team_id"] for t in teams if t["id"] in tids and t.get("gc_team_id")]
    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        ap.error("give --team-id or --all-teams")

    # Opponent name -> {seed names, dates}
    opps = {}
    for sid in seeds:
        try:
            team = public_get(f"/public/teams/{sid}")
            games = public_get(f"/public/teams/{sid}/games")
        except Exception as e:
            log(f"seed {sid}: public API error {e}")
            continue
        if season_label(team.get("team_season")) and season_label(team.get("team_season")).lower() != args.season.lower():
            log(f"seed {team.get('name')}: season {season_label(team.get('team_season'))} ≠ {args.season}, skipping")
            continue
        log(f"seed {team.get('name')}: {len(games)} games")
        for gm in games:
            name = (gm.get("opponent_team") or {}).get("name")
            if not name or re.match(r"(?i)^tbd", name):
                continue
            o = opps.setdefault(name, {"seed": team.get("name", ""), "dates": set()})
            o["dates"].add(str(gm.get("start_ts", ""))[:10])

    # Skip fresh ones
    fresh = set()
    if args.max_age_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.max_age_hours)
        for r in sb.select("opponent_records", {"season": args.season}, columns="opponent_name,scraped_at,gc_public_id"):
            try:
                ts = datetime.fromisoformat(str(r["scraped_at"]).replace("Z", "+00:00"))
                if ts > cutoff and r.get("gc_public_id"):
                    fresh.add(r["opponent_name"])
            except Exception:
                pass
    todo = [n for n in sorted(opps) if n not in fresh]
    log(f"{len(opps)} opponents found, {len(todo)} to look up ({len(fresh)} fresh)")

    out_rows, misses = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel="chrome",
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        g.login(page, args.timeout, os.getenv("GC_EMAIL"), os.getenv("GC_PASSWORD"))
        searcher = Searcher(page)
        for i, name in enumerate(todo, 1):
            info = opps[name]
            try:
                hit, how = resolve_opponent(searcher, name, info["seed"], info["dates"], args.season)
            except Exception as e:
                hit, how = None, f"error: {e}"
            if not hit:
                log(f"[{i}/{len(todo)}] ✗ {name} — {how}")
                misses.append(name)
                continue
            try:
                t = public_get(f"/public/teams/{hit['public_id']}")
                rec = (t.get("team_season") or {}).get("record") or {}
            except Exception as e:
                log(f"[{i}/{len(todo)}] ✗ {name} — record fetch error {e}")
                continue
            row = {
                "opponent_name": name, "season": args.season,
                "gc_public_id": hit["public_id"], "gc_name": t.get("name"),
                "city": (t.get("location") or {}).get("city"), "state": (t.get("location") or {}).get("state"),
                "age_group": t.get("age_group"),
                "wins": rec.get("win", 0), "losses": rec.get("loss", 0), "ties": rec.get("tie", 0),
                "match_method": how, "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            out_rows.append(row)
            log(f"[{i}/{len(todo)}] ✓ {name} → {row['gc_name']} ({row['city']}, {row['state']}) "
                f"{row['wins']}-{row['losses']}-{row['ties']} [{how}]")
            time.sleep(0.8)
        browser.close()

    if out_rows:
        # Normalize keys so every row in the batch has the same columns (PostgREST requirement)
        cols = ["opponent_name", "season", "gc_public_id", "gc_name", "city", "state", "age_group",
                "wins", "losses", "ties", "match_method", "scraped_at"]
        out_rows = [{c: r.get(c) for c in cols} for r in out_rows]
        for i in range(0, len(out_rows), 200):
            sb.upsert_many("opponent_records", out_rows[i:i + 200], on_conflict="opponent_name,season")
    log(f"DONE — {len(out_rows) - len(misses)} records saved, {len(misses)} not matched")
    if misses:
        log("Not matched: " + "; ".join(misses))


if __name__ == "__main__":
    main()
