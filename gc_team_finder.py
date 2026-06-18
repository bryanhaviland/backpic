#!/usr/bin/env python3
"""
gc_team_finder.py

Given one or more GameChanger team IDs, scrapes each team's schedule and
extracts every opponent's name + GC team ID.  Results are printed to the
console and saved to gc_teams_found.csv in the same directory.

Usage:
  python3 gc_team_finder.py --team-id mDCClxcC1LQs --team-id 62gwwZMxWShR

  # Or from a plain text file (one ID per line):
  python3 gc_team_finder.py --file team_ids.txt

Output columns: gc_team_id, team_name, found_via (the seed team ID)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# ── Config ────────────────────────────────────────────────────────────────────

GC_BASE      = "https://web.gc.com"
PAGE_TIMEOUT = 30    # seconds for initial page load
REACT_WAIT   = 8     # seconds for React/fetch to settle
INTER_PAGE   = 1.5   # polite delay between team pages
OUTPUT_CSV   = os.path.join(os.path.dirname(__file__), "gc_teams_found.csv")

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Selenium helpers ──────────────────────────────────────────────────────────

def build_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    # Reuse saved Chrome profile so GC session cookies are available
    profile = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default"
    )
    if os.path.isdir(profile):
        opts.add_argument(f"--user-data-dir={os.path.expanduser('~/Library/Application Support/Google/Chrome')}")
        opts.add_argument("--profile-directory=Default")
        log("Using saved Chrome profile (cookies/session)")
    else:
        log("WARNING: Chrome profile not found — may need to log in manually")
    # Enable CDP performance log for network capture
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


def wait_for_react(driver, seconds: float = REACT_WAIT):
    """Wait for the React SPA to finish rendering."""
    time.sleep(seconds)

# ── React fiber extraction ────────────────────────────────────────────────────

# JS: walk the React fiber tree looking for schedule/event data that includes
# opponent team names and IDs.
FIBER_JS = r"""
(function() {
  function getFiber(el) {
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')) {
        return el[k];
      }
    }
    return null;
  }

  function walk(fiber, depth, results) {
    if (!fiber || depth > 80) return;
    try {
      const p = fiber.memoizedProps;
      if (p && typeof p === 'object') {
        // Look for objects with a teamId or team_id property
        const str = JSON.stringify(p);
        if (str && str.includes('teamId') && str.length < 50000) {
          results.push(p);
        }
      }
    } catch(e) {}
    walk(fiber.child, depth + 1, results);
    walk(fiber.sibling, depth + 1, results);
  }

  const root = document.getElementById('__NEXT_DATA__');
  if (root) {
    try {
      const data = JSON.parse(root.textContent);
      return { source: 'next_data', data: data };
    } catch(e) {}
  }

  return { source: 'none', data: null };
})();
"""

SCHEDULE_API_JS = r"""
(function() {
  // Pull schedule data directly from the Next.js page props
  try {
    const el = document.getElementById('__NEXT_DATA__');
    if (!el) return null;
    const d = JSON.parse(el.textContent);
    return d;
  } catch(e) {
    return null;
  }
})();
"""

# ── Network capture ───────────────────────────────────────────────────────────

def capture_schedule_from_network(driver) -> list:
    """
    Read CDP performance logs looking for the GC schedule API response.
    Returns a list of {gc_team_id, team_name} dicts.
    """
    teams = []
    try:
        logs = driver.get_log("performance")
    except Exception:
        return teams

    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            url = msg.get("params", {}).get("response", {}).get("url", "")
            if "schedule" not in url and "events" not in url and "games" not in url:
                continue
            req_id = msg["params"]["requestId"]
            try:
                body_msg = driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": req_id}
                )
                body = body_msg.get("body", "")
                if not body:
                    continue
                data = json.loads(body)
                found = extract_teams_from_json(data)
                teams.extend(found)
            except Exception:
                pass
        except Exception:
            pass
    return teams


def extract_teams_from_json(obj, found=None) -> list:
    """Recursively extract {gc_team_id, team_name} from arbitrary JSON."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        # Common GC patterns: {teamId: "...", teamName: "..."} or {id: "...", name: "..."}
        tid = (obj.get("teamId") or obj.get("team_id") or
               obj.get("opponentTeamId") or obj.get("opponent_team_id"))
        name = (obj.get("teamName") or obj.get("team_name") or
                obj.get("opponentTeamName") or obj.get("opponent_team_name") or
                obj.get("name"))
        if tid and name and len(str(tid)) > 4:
            found.append({"gc_team_id": str(tid), "team_name": str(name)})
        for v in obj.values():
            extract_teams_from_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            extract_teams_from_json(item, found)
    return found

# ── DOM scraping fallback ─────────────────────────────────────────────────────

def scrape_schedule_dom(driver, seed_team_id: str) -> list:
    """
    Last-resort DOM scrape: look for links that match /teams/{id}/ and grab
    text nearby as the team name.
    """
    teams = []
    try:
        anchors = driver.find_elements(By.TAG_NAME, "a")
        pattern = re.compile(r"/teams/([A-Za-z0-9_-]{6,})")
        seen = set()
        for a in anchors:
            href = a.get_attribute("href") or ""
            m = pattern.search(href)
            if not m:
                continue
            tid = m.group(1)
            if tid == seed_team_id or tid in seen:
                continue
            seen.add(tid)
            name = a.text.strip() or a.get_attribute("aria-label") or ""
            if name:
                teams.append({"gc_team_id": tid, "team_name": name})
    except Exception as e:
        log(f"  DOM scrape error: {e}")
    return teams

# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_team(driver, team_id: str) -> list:
    """
    Navigate to a team's schedule page and return all opponent teams found.
    Returns list of {gc_team_id, team_name} dicts.
    """
    url = f"{GC_BASE}/teams/{team_id}/schedule"
    log(f"  → {url}")

    try:
        driver.get(url)
    except TimeoutException:
        log("  Page load timed out — continuing with what loaded")
    except WebDriverException as e:
        log(f"  WebDriver error: {e}")
        return []

    wait_for_react(driver, REACT_WAIT)

    teams = []

    # Strategy 1: Next.js page data
    try:
        next_data = driver.execute_script(SCHEDULE_API_JS)
        if next_data:
            found = extract_teams_from_json(next_data)
            if found:
                log(f"  Next.js data: {len(found)} raw team refs")
                teams.extend(found)
    except Exception as e:
        log(f"  Next.js extract error: {e}")

    # Strategy 2: CDP network log
    if not teams:
        net_teams = capture_schedule_from_network(driver)
        if net_teams:
            log(f"  Network capture: {len(net_teams)} raw team refs")
            teams.extend(net_teams)

    # Strategy 3: DOM link scrape
    if not teams:
        dom_teams = scrape_schedule_dom(driver, team_id)
        if dom_teams:
            log(f"  DOM scrape: {len(dom_teams)} teams")
            teams.extend(dom_teams)

    # Deduplicate by gc_team_id, exclude the seed itself
    seen = set()
    unique = []
    for t in teams:
        tid = t.get("gc_team_id", "").strip()
        if not tid or tid == team_id or tid in seen:
            continue
        seen.add(tid)
        unique.append({"gc_team_id": tid, "team_name": t.get("team_name", "").strip()})

    log(f"  Found {len(unique)} unique opponent teams")
    return unique

# ── CLI + main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Find GC team names + IDs from schedule pages")
    parser.add_argument("--team-id", action="append", dest="team_ids", default=[],
                        metavar="ID", help="GC team ID (repeat for multiple)")
    parser.add_argument("--file", metavar="FILE",
                        help="Text file with one GC team ID per line")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show the browser window (useful for debugging)")
    args = parser.parse_args()

    # Collect seed team IDs
    seed_ids = list(args.team_ids)
    if args.file:
        with open(args.file) as f:
            for line in f:
                tid = line.strip()
                if tid and not tid.startswith("#"):
                    seed_ids.append(tid)

    if not seed_ids:
        parser.error("Provide at least one --team-id or --file")

    log(f"Starting team finder — {len(seed_ids)} seed team(s)")
    headless = not args.no_headless

    all_results = []  # {gc_team_id, team_name, found_via}

    driver = build_driver(headless=headless)
    try:
        driver.set_page_load_timeout(PAGE_TIMEOUT)

        for seed in seed_ids:
            seed = seed.strip()
            log(f"\nScraping schedule for seed team: {seed}")
            opponents = scrape_team(driver, seed)
            for opp in opponents:
                all_results.append({
                    "gc_team_id":  opp["gc_team_id"],
                    "team_name":   opp["team_name"],
                    "found_via":   seed,
                })
            time.sleep(INTER_PAGE)

    finally:
        driver.quit()

    if not all_results:
        log("\nNo teams found. Try --no-headless to debug, or check that you're logged in.")
        sys.exit(1)

    # Deduplicate across all seeds
    seen_global = set()
    deduped = []
    for r in all_results:
        if r["gc_team_id"] not in seen_global:
            seen_global.add(r["gc_team_id"])
            deduped.append(r)

    # Print table
    print(f"\n{'─'*60}")
    print(f"{'GC Team ID':<22}  {'Team Name':<32}  Via")
    print(f"{'─'*60}")
    for r in sorted(deduped, key=lambda x: x["team_name"].lower()):
        print(f"{r['gc_team_id']:<22}  {r['team_name']:<32}  {r['found_via']}")
    print(f"{'─'*60}")
    print(f"Total: {len(deduped)} teams\n")

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["gc_team_id", "team_name", "found_via"])
        writer.writeheader()
        writer.writerows(deduped)
    log(f"Saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
