"""
GameChanger Scraper — gc_scraper.py
====================================
Scrapes team data from web.gc.com and writes to a Supabase database.
Logs in once, then scrapes every team in one run.

Usage:
    # Credentials via environment variables (recommended)
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_KEY="your-service-role-key"
    export GC_EMAIL="you@example.com"
    export GC_PASSWORD="your-gc-password"

    # Single team by ID
    python3 gc_scraper.py --team-id ABC123 --team "Lady Canes 10U"

    # Multiple teams in one run (log in once)
    python3 gc_scraper.py \
        --team-id ABC123 --team "Lady Canes 10U" \
        --team-id DEF456 --team "Lady Bombers Berrios 10U" \
        --team-id GHI789 --team "STP Elite Navy 12U"

    # Search by name (no --team-id needed)
    python3 gc_scraper.py --team "Lady Canes" --division "10U" --state FL

    # Options
    --sport     softball (default) | baseball
    --headless  run browser without a visible window

Requirements:
    pip3 install playwright requests
    python3 -m playwright install chromium
"""

import argparse
import difflib
import email
import imaplib
import os
import re
import time
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Auto-load .env (credentials) if present
# ---------------------------------------------------------------------------
# Ensures SUPABASE_URL / SUPABASE_KEY / GC_EMAIL / GC_PASSWORD /
# GMAIL_APP_PASSWORD are available even when this script is invoked directly
# (e.g. by a scheduler) without first sourcing .env in the shell. Only fills
# in vars that aren't already set, so an explicit `source .env` or exported
# env still takes precedence.

def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

_load_dotenv()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="GameChanger → Supabase scraper. Repeat --team-id / --team for multiple teams.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Repeatable per-team flags
    p.add_argument("--team-id", action="append", dest="team_ids", default=[],
                   metavar="ID",
                   help="GC team ID (repeat for each team, e.g. --team-id ABC --team-id DEF)")
    p.add_argument("--team", action="append", dest="team_names", default=[],
                   metavar="NAME",
                   help="Team display name / search term (repeat to match each --team-id)")
    # Global options
    p.add_argument("--sport", default="softball", choices=["softball", "baseball"])
    p.add_argument("--headless", action="store_true", help="Headless browser mode")
    p.add_argument("--timeout", type=int, default=15000, help="Nav timeout in ms")
    p.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    p.add_argument("--supabase-key", default=os.getenv("SUPABASE_KEY"))
    p.add_argument("--gc-email", default=os.getenv("GC_EMAIL"))
    p.add_argument("--gc-password", default=os.getenv("GC_PASSWORD"))
    p.add_argument("--gmail-app-password", default=os.getenv("GMAIL_APP_PASSWORD"),
                   help="Gmail App Password for auto-fetching GC OTP codes")
    # Search filters (applied when resolving a --team name without a --team-id)
    p.add_argument("--division", default=None, help="e.g. '10U', '12U'")
    p.add_argument("--state",    default=None, help="e.g. 'FL', 'TX'")
    p.add_argument("--season",   default=None, help="e.g. 'Spring 2026'")
    p.add_argument("--since-date", default=None, dest="since_date",
                   help="Only process games on or after this date (YYYY-MM-DD). "
                        "Skips box scores, plays, and stats for older games.")
    p.add_argument("--all-teams", action="store_true",
                   help="Scrape every team already in the Supabase teams table.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NBSP = "\xa0"

def normalize(text: str) -> str:
    return text.replace(NBSP, " ").replace(" ", " ").strip()


# ── Fuzzy team-name matching ──────────────────────────────────────────────────
# Used in parse_full_box_score to identify which section belongs to the scouted
# team. A plain substring check fails when names differ by abbreviation
# ("CF" vs "Central Florida"), hyphenation, or "10U" suffix.

_TEAM_NOISE = re.compile(
    r"\b(10u|12u|14u|16u|18u|softball|fastpitch|baseball|"
    r"gold|silver|blue|red|white|black|navy|the|and|of|at)\b",
    re.IGNORECASE,
)

# Regional abbreviations that show up in our --team names but get spelled out
# in full on GameChanger's actual box-score roster name. Without expanding
# these first, "CF Lady Canes" vs "Central Florida Lady Canes" scores well
# below the fuzzy threshold despite being the same team — difflib has no idea
# "CF" stands for "Central Florida".
_ABBREV_MAP = {
    r"\bcf\b": "central florida",
}

def _fuzzy_norm(name: str) -> str:
    """Lowercase, expand known abbreviations, strip punctuation/noise, collapse spaces."""
    n = name.lower()
    for pat, repl in _ABBREV_MAP.items():
        n = re.sub(pat, repl, n)
    n = re.sub(r"[^\w\s]", " ", n)
    n = _TEAM_NOISE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()

def _team_matches(scouted_name: str, gc_section_name: str,
                  threshold: float = 0.68) -> bool:
    """
    Return True if scouted_name refers to the same team as gc_section_name.
    Fast-path: substring check (original behaviour, zero cost).
    Fallback: fuzzy match via difflib.SequenceMatcher.

    Examples that now work:
      "CF Flamingos"           <-> "Central Florida Flamingos 10U"
      "LadyHawks Garcia"       <-> "10U LadyHawks-Garcia 10U"
      "St Pete Scorchers Wilson" <-> "St Pete Scorchers Wilson 10U"
    """
    sl = scouted_name.lower()
    gl = gc_section_name.lower()
    if sl in gl or gl in sl:
        return True
    sn = _fuzzy_norm(scouted_name)
    gn = _fuzzy_norm(gc_section_name)
    if not sn or not gn:
        return False
    base  = difflib.SequenceMatcher(None, sn, gn).ratio()
    bonus = 0.10 if (sn in gn or gn in sn) else 0.0
    score = min(1.0, base + bonus)
    if score >= threshold:
        print(f"[boxscore] fuzzy \'{scouted_name}\' <-> \'{gc_section_name}\' score={score:.2f} matched")
        return True
    return False

def page_lines(page: Page) -> list[str]:
    raw = page.inner_text("body")
    return [normalize(ln) for ln in raw.split("\n") if normalize(ln)]

def safe_float(val: str) -> Optional[float]:
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

def safe_int(val: str) -> Optional[int]:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

def classify_outcome(text: str) -> Optional[str]:
    t = text.lower()
    if any(x in t for x in ["home run", " hr", "homered", "homers"]):
        return "HomeRun"
    if "triple play" in t or "into a triple play" in t:
        return "TriplePlay"
    if "double play" in t or "into a double play" in t or "doubles off" in t:
        return "DoublePlay"
    if "triple" in t or " 3b" in t:
        return "Triple"
    if "double" in t or " 2b" in t:
        return "Double"
    if "single" in t or " 1b" in t:
        return "Single"
    if any(x in t for x in ["flyout", "fly out", "flied out", "flies out",
                              "popped out", "pops out", "pop out"]):
        return "FlyOut"
    if any(x in t for x in ["groundout", "ground out", "grounded out",
                              "grounds out", "hits a ground ball", "grounds into"]):
        return "GroundOut"
    if any(x in t for x in ["lineout", "line out", "lined out", "lines out"]):
        return "LineOut"
    if any(x in t for x in ["reached on error", "reaches on error", "reaches base on error",
                              "error"]) and any(x in t for x in ["reached", "reaches", "on error"]):
        return "Error"
    if "sacrifice fly" in t or "sac fly" in t:
        return "SacFly"
    if "sacrifice bunt" in t or "sac bunt" in t or "sacrifices" in t:
        return "SacBunt"
    if "bunts out" in t or "bunt out" in t or "bunted out" in t or "bunts into" in t:
        return "BuntOut"
    if "bunts" in t or "bunted" in t:
        return "SacBunt"
    if "fielder's choice" in t or " fc " in t:
        return "FC"
    if any(x in t for x in ["strikeout", "struck out", " k ", "strikes out"]):
        return "Strikeout"
    if any(x in t for x in ["intentional walk", " ibb", "walks", "walked", " bb "]):
        return "Walk"
    if any(x in t for x in ["hit by pitch", "is hit by pitch", " hbp"]):
        return "HBP"
    return None

def classify_zone(text: str) -> Optional[str]:
    t = text.lower()
    if any(x in t for x in ["left field", "left fielder", "to left", "left center"]):
        return "LF"
    if any(x in t for x in ["right field", "right fielder", "to right", "right center"]):
        return "RF"
    if any(x in t for x in ["center field", "center fielder", "centerfield"]):
        return "CF"
    if any(x in t for x in ["third base", "third baseman"]):
        return "3B"
    if any(x in t for x in ["shortstop", "to short"]):
        return "SS"
    if any(x in t for x in ["second base", "second baseman"]):
        return "2B"
    if any(x in t for x in ["first base", "first baseman"]):
        return "1B"
    if any(x in t for x in ["pitcher", "to the mound"]):
        return "P"
    return "Unknown"

ZONE_EXEMPT = {"Strikeout", "Walk", "HBP"}

PITCHER_RE = re.compile(
    r"(?:lineup changed|now pitching|in at pitcher)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)|"
    r"([A-Z][a-z]+ [A-Z][a-z]+)\s+(?:now pitching|pitching)",
    re.IGNORECASE,
)

def extract_pitcher_name(line: str) -> Optional[str]:
    m = PITCHER_RE.search(line)
    if m:
        return m.group(1) or m.group(2)
    return None


# ---------------------------------------------------------------------------
# Supabase REST API layer (uses requests directly — no supabase-py needed)
# ---------------------------------------------------------------------------

class SupabaseClient:
    """Thin wrapper around Supabase's PostgREST REST API."""

    def __init__(self, url: str, key: str):
        if not url or not key:
            raise ValueError(
                "Supabase credentials missing. Set SUPABASE_URL and SUPABASE_KEY "
                "env vars, or pass --supabase-url / --supabase-key."
            )
        url = url.rstrip("/")
        # Strip any existing /rest/v1 suffix to avoid doubling it
        if url.endswith("/rest/v1"):
            url = url[:-len("/rest/v1")]
        self.base = url + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def select(self, table: str, filters: dict, columns: str = "*") -> list[dict]:
        params = {"select": columns}
        for col, val in filters.items():
            # None must use PostgREST's "is.null" — "eq.None" is the literal
            # string "None" and never matches a real SQL NULL, which was
            # causing lookups for un-jersey-numbered players to always miss
            # and re-insert duplicate season rows instead of updating.
            params[col] = "is.null" if val is None else f"eq.{val}"
        r = requests.get(f"{self.base}/{table}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, data: dict) -> dict:
        r = requests.post(f"{self.base}/{table}", headers=self.headers, json=data)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else {}

    def insert_many(self, table: str, rows: list[dict]):
        if not rows:
            return
        r = requests.post(f"{self.base}/{table}", headers=self.headers, json=rows)
        r.raise_for_status()

    def upsert_many(self, table: str, rows: list[dict], on_conflict: str = None):
        """Insert rows, updating on conflict (merge-duplicates).

        on_conflict must list the columns of the table's actual UNIQUE/PK
        constraint — PostgREST defaults to the primary key otherwise, which
        almost never matches on a fresh insert (no `id` in the payload), so
        the real unique-constraint violation surfaces as a raw 409 no matter
        what the Prefer header says.
        """
        if not rows:
            return
        params = {"on_conflict": on_conflict} if on_conflict else {}
        hdrs = {**self.headers, "Prefer": "resolution=merge-duplicates"}
        r = requests.post(f"{self.base}/{table}", headers=hdrs, params=params, json=rows)
        if r.status_code == 409:
            hdrs2 = {**self.headers, "Prefer": "resolution=ignore-duplicates"}
            r = requests.post(f"{self.base}/{table}", headers=hdrs2, params=params, json=rows)
        if r.status_code >= 400:
            print(f"[supabase] upsert_many({table}) failed {r.status_code}: {r.text[:500]}")
        r.raise_for_status()

    def delete_where(self, table: str, filters: dict):
        """Delete rows matching all filter conditions (used to clear before re-insert)."""
        params = {col: f"eq.{val}" for col, val in filters.items()}
        r = requests.delete(f"{self.base}/{table}", headers=self.headers, params=params)
        r.raise_for_status()

    def update(self, table: str, row_id: int, data: dict):
        r = requests.patch(
            f"{self.base}/{table}",
            headers=self.headers,
            params={"id": f"eq.{row_id}"},
            json=data,
        )
        r.raise_for_status()

    def ping(self):
        """Quick connectivity check — raises on failure."""
        r = requests.get(f"{self.base}/teams", headers=self.headers, params={"select": "id", "limit": "1"})
        r.raise_for_status()
        print(f"[supabase] Connection OK — REST endpoint reachable")


def _select_one(sb: SupabaseClient, table: str, filters: dict) -> Optional[dict]:
    rows = sb.select(table, filters, columns="id")
    return rows[0] if rows else None


def upsert_team(sb: SupabaseClient, gc_team_id: str, name: str, sport: str) -> int:
    rows = sb.select("teams", {"gc_team_id": gc_team_id}, columns="id,name")
    if rows:
        return rows[0]["id"]
    row = sb.insert("teams", {"gc_team_id": gc_team_id, "name": name, "sport": sport})
    return row["id"]


def get_team_name_from_db(sb: SupabaseClient, gc_team_id: str) -> Optional[str]:
    """Look up stored team name for a gc_team_id. Returns None if not found."""
    rows = sb.select("teams", {"gc_team_id": gc_team_id}, columns="name")
    return rows[0]["name"] if rows else None


_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

def _infer_season(date_str: Optional[str]) -> Optional[str]:
    """Infer season label from 'May 2026', 'May 20 2026', or 'YYYY-MM-DD' date strings."""
    if not date_str:
        return None
    # Try "Month Year" or "Month Day Year" format
    m = re.match(r'([A-Za-z]{3})\w*\s+(?:\d{1,2}\s+)?(\d{4})', date_str)
    if m:
        month_num = _MONTH_MAP.get(m.group(1).lower())
        year = int(m.group(2))
    else:
        # Try ISO format YYYY-MM-DD
        try:
            from datetime import date as _date
            d = _date.fromisoformat(date_str)
            month_num, year = d.month, d.year
        except ValueError:
            return None
    if month_num is None:
        return None
    if 2 <= month_num <= 5:
        label = "Spring"
    elif 6 <= month_num <= 8:
        label = "Summer"
    elif 9 <= month_num <= 11:
        label = "Fall"
    else:
        label = "Winter"
    return f"{label} {year}"


def _parse_game_date(full_date_raw: str, month_year: str = "") -> Optional[str]:
    """
    Convert a GC date string to ISO format (YYYY-MM-DD).

    Tries (in order):
      1. full_date_raw — e.g. "May 30" / "May 30, 2026" / "Sat May 30, 2:00 PM"
      2. month_year    — e.g. "May 2026" (day defaults to 1, low precision)

    Returns None if neither can be parsed.
    """
    from datetime import datetime as _dt
    current_year = _dt.now().year

    def _try(s: str) -> Optional[str]:
        if not s:
            return None
        # Strip day-of-week prefix ("Sat ", "Saturday, ")
        s = re.sub(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*[\s,]+', '', s, flags=re.I).strip()
        # Strip time suffix ("2:00 PM …")
        s = re.sub(r',?\s*\d{1,2}:\d{2}\s*(AM|PM).*$', '', s, flags=re.I).strip()
        for fmt in ('%b %d %Y', '%B %d %Y', '%b %d, %Y', '%B %d, %Y',
                    '%b %d', '%B %d'):
            try:
                dt = _dt.strptime(s, fmt)
                if dt.year == 1900:
                    dt = dt.replace(year=current_year)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
        return None

    return _try(full_date_raw) or _try(month_year)


def upsert_game(sb: SupabaseClient, team_id: int, g: dict) -> int:
    game_date = _parse_game_date(
        g.get("full_date_raw", ""),
        g.get("date", ""),
    )
    data = {
        "team_id": team_id,
        "gc_event_id": g["gc_event_id"],
        "date": g.get("date") or None,
        "game_date": game_date,
        "opponent": g.get("opponent") or None,
        "home_away": g.get("home_away") or None,
        "result": g.get("result") or None,
        "runs_scored": g.get("runs_scored"),
        "runs_allowed": g.get("runs_allowed"),
        "starting_pitcher": g.get("starting_pitcher") or None,
        "season": _infer_season(game_date or g.get("date")),
        "scraped_at": "now()",
    }
    existing = _select_one(sb, "games", {"team_id": team_id, "gc_event_id": g["gc_event_id"]})
    if existing:
        sb.update("games", existing["id"], data)
        return existing["id"]
    row = sb.insert("games", data)
    return row["id"]


def insert_plays(sb: SupabaseClient, team_id: int, game_id: int, plays: list[dict]):
    if not plays:
        return
    # Clear any existing plays for this game before inserting to prevent duplicates
    sb.delete_where("plays", {"team_id": team_id, "game_id": game_id})
    rows = [
        {
            "team_id": team_id,
            "game_id": game_id,
            "gc_event_id": p["gc_event_id"],
            "inning": p.get("inning"),
            "half": p.get("half"),
            "player_name": p.get("player_name") or None,
            "side": p.get("side"),
            "outcome": p.get("outcome"),
            "zone": p.get("zone"),
            "error_fielder": p.get("error_fielder") or None,
        }
        for p in plays
    ]
    for i in range(0, len(rows), 500):
        sb.insert_many("plays", rows[i:i+500])


def upsert_batting(sb: SupabaseClient, team_id: int, rows: list[dict]):
    for r in rows:
        data = {
            "team_id": team_id,
            "player_name": r["player_name"],
            "player_num": r.get("player_num"),
            "gp": r.get("gp"), "pa": r.get("pa"), "ab": r.get("ab"),
            "avg": r.get("avg"), "obp": r.get("obp"), "ops": r.get("ops"),
            "slg": r.get("slg"), "h": r.get("h"), "singles": r.get("singles"),
            "doubles": r.get("doubles"), "triples": r.get("triples"),
            "hr": r.get("hr"), "rbi": r.get("rbi"), "r": r.get("r"),
            "bb": r.get("bb"), "k": r.get("k"), "k_looking": r.get("k_looking"),
            "hbp": r.get("hbp"), "sac": r.get("sac"), "sf": r.get("sf"),
            "roe": r.get("roe"), "fc": r.get("fc"),
            "sb": r.get("sb", 0), "cs": r.get("cs", 0),
        }
        existing = _select_one(sb, "batting_stats", {
            "team_id": team_id, "player_name": r["player_name"], "player_num": r.get("player_num")
        })
        if existing:
            sb.update("batting_stats", existing["id"], data)
        else:
            sb.insert("batting_stats", data)


def upsert_pitching(sb: SupabaseClient, team_id: int, rows: list[dict]):
    for r in rows:
        data = {
            "team_id": team_id,
            "player_name": r["player_name"],
            "player_num": r.get("player_num"),
            "ip": r.get("ip"), "gp": r.get("gp"), "gs": r.get("gs"),
            "bf": r.get("bf"), "num_pitches": r.get("num_pitches"),
            "w": r.get("w"), "l": r.get("l"), "sv": r.get("sv"),
            "h": r.get("h"), "r": r.get("r"), "er": r.get("er"),
            "bb": r.get("bb"), "k": r.get("k"), "k_looking": r.get("k_looking"),
            "hbp": r.get("hbp"), "era": r.get("era"), "whip": r.get("whip"),
            "wp": r.get("wp", 0),
        }
        existing = _select_one(sb, "pitching_stats", {
            "team_id": team_id, "player_name": r["player_name"], "player_num": r.get("player_num")
        })
        if existing:
            sb.update("pitching_stats", existing["id"], data)
        else:
            sb.insert("pitching_stats", data)


def insert_game_batting_stats(sb: SupabaseClient, team_id: int, game_id: int,
                               event_id: str, box_score: dict):
    """Write per-game batting rows for the scouted team only."""
    sb.delete_where("game_batting_stats", {"team_id": team_id, "game_id": game_id})
    section = box_score.get('scouted', {})
    rows_by_key = {}  # (player_name, player_num) -> row, merges duplicate batting-list entries
    for p in section.get('batting', []):
        if p.get('name') == 'TEAM':
            continue
        key = (p['name'], p.get('num'))
        if key in rows_by_key:
            # Same player appears twice in the parsed box score (e.g. re-entry,
            # split rows) — merge counting stats instead of emitting two rows,
            # since Postgres' ON CONFLICT DO UPDATE can't touch the same
            # conflict-target row twice within one INSERT batch.
            row = rows_by_key[key]
            for f in ("ab", "r", "h", "rbi", "bb", "so"):
                row[f] = row.get(f, 0) + p.get(f, 0)
            existing_pos = set(row["positions"].split(', ')) if row["positions"] else set()
            existing_pos.update(p.get('positions', []))
            row["positions"] = ', '.join(sorted(filter(None, existing_pos)))
            continue
        rows_by_key[key] = {
            "team_id": team_id,
            "game_id": game_id,
            "gc_event_id": event_id,
            "player_name": p['name'],
            "player_num": p.get('num'),
            "positions": ', '.join(p.get('positions', [])),
            "ab":  p.get('ab', 0),
            "r":   p.get('r',  0),
            "h":   p.get('h',  0),
            "rbi": p.get('rbi', 0),
            "bb":  p.get('bb', 0),
            "so":  p.get('so', 0),
            "doubles": 0, "triples": 0, "hr": 0, "hbp": 0, "sb": 0, "cs": 0,
        }
    rows = list(rows_by_key.values())

    # Distribute extras (2B/3B/HR/HBP/SB/CS) to players by name prefix
    extras = section.get('extras_bat', {})
    for xkey, field in [('2B','doubles'),('3B','triples'),('HR','hr'),
                         ('HBP','hbp'),('SB','sb'),('CS','cs')]:
        for part in extras.get(xkey, '').split(','):
            part = part.strip()
            if not part:
                continue
            cnt_m = re.search(r'\b(\d+)$', part)
            cnt = int(cnt_m.group(1)) if cnt_m else 1
            name_part = re.sub(r'\s*\d+$', '', part).strip().lower()
            for row in rows:
                if row['player_name'].lower().startswith(name_part):
                    row[field] = row.get(field, 0) + cnt
                    break

    if rows:
        for i in range(0, len(rows), 500):
            sb.upsert_many("game_batting_stats", rows[i:i+500],
                           on_conflict="team_id,game_id,player_name,player_num")
        print(f"[supabase] {len(rows)} game_batting_stats rows → {event_id}")


def insert_game_pitching_stats(sb: SupabaseClient, team_id: int, game_id: int,
                                event_id: str, box_score: dict):
    """Write per-game pitching rows for the scouted team only."""
    sb.delete_where("game_pitching_stats", {"team_id": team_id, "game_id": game_id})
    section = box_score.get('scouted', {})
    extras = section.get('extras_pit', {})
    rows_by_key = {}  # (player_name, player_num) -> row, merges duplicate pitching-list entries

    for p in section.get('pitching', []):
        if p.get('name') == 'TEAM':
            continue
        key = (p['name'], p.get('num'))
        if key in rows_by_key:
            row = rows_by_key[key]
            for f in ("ip", "h", "r", "er", "bb", "so"):
                row[f] = row.get(f, 0) + p.get(f, 0)
            pname_lower = p['name'].lower()
            for xkey, field in [('WP', 'wp'), ('HBP', 'hbp')]:
                for part in extras.get(xkey, '').split(','):
                    part = part.strip()
                    cnt_m = re.search(r'\b(\d+)$', part)
                    cnt = int(cnt_m.group(1)) if cnt_m else 1
                    if pname_lower.startswith(re.sub(r'\s*\d+$', '', part).strip().lower()):
                        row[field] += cnt
            continue
        row = {
            "team_id": team_id,
            "game_id": game_id,
            "gc_event_id": event_id,
            "player_name": p['name'],
            "player_num": p.get('num'),
            "ip":  p.get('ip',  0),
            "h":   p.get('h',   0),
            "r":   p.get('r',   0),
            "er":  p.get('er',  0),
            "bb":  p.get('bb',  0),
            "so":  p.get('so',  0),
            "wp":  0, "hbp": 0, "num_pitches": 0, "bf": 0,
        }
        pname_lower = p['name'].lower()
        # WP
        for part in extras.get('WP', '').split(','):
            part = part.strip()
            cnt_m = re.search(r'\b(\d+)$', part)
            cnt = int(cnt_m.group(1)) if cnt_m else 1
            if pname_lower.startswith(re.sub(r'\s*\d+$', '', part).strip().lower()):
                row['wp'] += cnt
        # HBP
        for part in extras.get('HBP', '').split(','):
            part = part.strip()
            cnt_m = re.search(r'\b(\d+)$', part)
            cnt = int(cnt_m.group(1)) if cnt_m else 1
            if pname_lower.startswith(re.sub(r'\s*\d+$', '', part).strip().lower()):
                row['hbp'] += cnt
        # Pitch count
        for ps_entry in extras.get('Pitches-Strikes', '').split(','):
            ps_m = re.search(r'(.+?)\s+(\d+)-\d+$', ps_entry.strip())
            if ps_m and pname_lower.startswith(ps_m.group(1).strip().lower()):
                row['num_pitches'] = int(ps_m.group(2))
        # BF
        for bf_entry in extras.get('Batters Faced', '').split(','):
            bf_m = re.search(r'(.+?)\s+(\d+)$', bf_entry.strip())
            if bf_m and pname_lower.startswith(bf_m.group(1).strip().lower()):
                row['bf'] = int(bf_m.group(2))

        rows_by_key[key] = row

    rows = list(rows_by_key.values())
    if rows:
        for i in range(0, len(rows), 500):
            sb.upsert_many("game_pitching_stats", rows[i:i+500],
                            on_conflict="team_id,game_id,player_name,player_num")
        print(f"[supabase] {len(rows)} game_pitching_stats rows → {event_id}")


def insert_game_catching_stats(sb: SupabaseClient, team_id: int, game_id: int,
                                event_id: str, rows: list[dict]):
    if not rows:
        return
    sb.delete_where("game_catching_stats", {"team_id": team_id, "game_id": game_id})
    for r in rows:
        sb.insert("game_catching_stats", {
            "team_id": team_id, "game_id": game_id, "gc_event_id": event_id,
            "player_name": r["player_name"], "player_num": r.get("player_num"),
            "inn": r.get("inn", 0), "pb": r.get("pb", 0),
            "sb": r.get("sb", 0), "sb_att": r.get("sb_att", 0),
            "cs": r.get("cs", 0), "cs_pct": r.get("cs_pct"),
            "pik": r.get("pik", 0), "ci": r.get("ci", 0),
        })
    print(f"[supabase] {len(rows)} game_catching_stats rows → {event_id}")


def insert_game_innings_played(sb: SupabaseClient, team_id: int, game_id: int,
                                event_id: str, rows: list[dict]):
    if not rows:
        return
    sb.delete_where("game_innings_played", {"team_id": team_id, "game_id": game_id})
    for r in rows:
        sb.insert("game_innings_played", {
            "team_id": team_id, "game_id": game_id, "gc_event_id": event_id,
            "player_name": r["player_name"], "player_num": r.get("player_num"),
            "inn_p":  r.get("inn_p",  0), "inn_c":  r.get("inn_c",  0),
            "inn_1b": r.get("inn_1b", 0), "inn_2b": r.get("inn_2b", 0),
            "inn_3b": r.get("inn_3b", 0), "inn_ss": r.get("inn_ss", 0),
            "inn_lf": r.get("inn_lf", 0), "inn_cf": r.get("inn_cf", 0),
            "inn_rf": r.get("inn_rf", 0), "inn_sf": r.get("inn_sf", 0),
            "inn_total": r.get("inn_total", 0),
        })
    print(f"[supabase] {len(rows)} game_innings_played rows → {event_id}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def fetch_gc_otp_from_gmail(gmail_user: str, gmail_app_password: str,
                             timeout_secs: int = 60) -> Optional[str]:
    """Poll Gmail via IMAP for the most recent GameChanger OTP code."""
    print("[auth] Polling Gmail for GC verification code ...")
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(gmail_user, gmail_app_password)
            mail.select("inbox")
            # Search for recent unseen mail from GameChanger
            for sender in ("noreply@gc.com", "no-reply@gc.com",
                           "notifications@gc.com", "gamechanger"):
                _, data = mail.search(None, f'(UNSEEN FROM "{sender}")')
                ids = (data[0] or b"").split()
                if ids:
                    _, msg_data = mail.fetch(ids[-1], "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")
                    m = re.search(r'\b(\d{6})\b', body)
                    if m:
                        print(f"[auth] ✓ Got OTP from Gmail: {m.group(1)}")
                        mail.logout()
                        return m.group(1)
            mail.logout()
        except Exception as e:
            print(f"[auth] Gmail poll error: {e}")
        time.sleep(5)
    print("[auth] Timed out waiting for OTP email")
    return None


def _has_email_field(page: Page) -> bool:
    """Return True if an email input is visible on the page."""
    try:
        return page.locator(
            "input[type='email'], input[name='email'], input[placeholder*='email' i]"
        ).first.is_visible(timeout=2000)
    except Exception:
        return False

def _has_password_field(page: Page) -> bool:
    try:
        return page.locator("input[type='password']").first.is_visible(timeout=2000)
    except Exception:
        return False

def login(page: Page, timeout: int, email: str = None, password: str = None):
    print("[auth] Navigating to https://web.gc.com …")
    page.goto("https://web.gc.com", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    time.sleep(3)

    print(f"[auth] Current URL: {page.url}")

    # Check for email field on current page; if not present, try direct sign-in URL
    if not _has_email_field(page):
        print("[auth] No login form on landing page — trying sign-in URL …")
        page.goto("https://web.gc.com/sign-in", timeout=timeout)
        page.wait_for_load_state("networkidle", timeout=timeout)
        time.sleep(3)
        print(f"[auth] URL after sign-in nav: {page.url}")

    if _has_email_field(page):
        if email:
            print("[auth] Login form found — entering email …")
            try:
                email_field = page.locator(
                    "input[type='email'], input[name='email'], input[placeholder*='email' i]"
                ).first
                email_field.click(timeout=5000)
                email_field.fill(email, timeout=5000)
                print(f"[auth] Email filled: {email}")
                time.sleep(0.5)

                # Click the first submit/continue button
                page.locator(
                    "button[type='submit'], button:has-text('Sign In'), "
                    "button:has-text('Log In'), button:has-text('Continue'), "
                    "button:has-text('Next'), button:has-text('Send')"
                ).first.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=timeout)
                time.sleep(2)
                print(f"[auth] Post-email URL: {page.url}")

                # If a password field appeared, fill it
                if _has_password_field(page) and password:
                    pw_field = page.locator("input[type='password']").first
                    pw_field.click(timeout=5000)
                    pw_field.fill(password, timeout=5000)
                    print("[auth] Password filled")
                    time.sleep(0.5)
                    page.locator(
                        "button[type='submit'], button:has-text('Sign In'), button:has-text('Continue')"
                    ).first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=timeout)
                    time.sleep(3)

                # GC uses OTP — try Gmail auto-fetch, fall back to manual prompt
                print("[auth] Check your email for a one-time code.")
                gmail_app_pw = os.getenv("GMAIL_APP_PASSWORD")
                if gmail_app_pw:
                    code = fetch_gc_otp_from_gmail(email, gmail_app_pw) or ""
                    if not code:
                        code = input("Enter the one-time code from your email: ").strip()
                else:
                    code = input("Enter the one-time code from your email: ").strip()
                if code:
                    # Find the code input — could be individual digit boxes or one field
                    code_field = page.locator(
                        "input[placeholder*='code' i], input[name*='code' i], "
                        "input[name*='otp' i], input[type='number'], input[inputmode='numeric']"
                    ).first
                    try:
                        code_field.fill(code, timeout=5000)
                    except Exception:
                        # Try typing digit by digit into split inputs
                        digit_fields = page.locator("input[maxlength='1']").all()
                        if digit_fields:
                            for i, ch in enumerate(code[:len(digit_fields)]):
                                digit_fields[i].fill(ch)
                    # GC may show code + password on the same screen
                    if _has_password_field(page) and password:
                        pw_field = page.locator("input[type='password']").first
                        pw_field.fill(password, timeout=5000)
                        print("[auth] Password filled on OTP screen")
                    time.sleep(0.5)
                    try:
                        page.locator(
                            "button[type='submit'], button:has-text('Verify'), "
                            "button:has-text('Continue'), button:has-text('Confirm')"
                        ).first.click(timeout=5000)
                    except Exception:
                        pass  # Some OTP forms auto-submit
                    page.wait_for_load_state("networkidle", timeout=timeout)
                    time.sleep(3)

            except Exception as e:
                print(f"[auth] Error during login: {e}")
                print("[auth] Pausing 30s — complete login manually in the browser …")
                time.sleep(30)
        else:
            print("[auth] No email provided — pausing 30s for manual login …")
            print("[auth] TIP: set GC_EMAIL env var to automate this step")
            time.sleep(30)
    else:
        print("[auth] No login form detected — assuming already authenticated")

    print(f"[auth] Done. Current URL: {page.url}")


# ---------------------------------------------------------------------------
# Step 1: Find team
# ---------------------------------------------------------------------------

def find_team(page: Page, team_name: str, sport: str, timeout: int,
              division: str = None, state: str = None, season: str = None) -> Optional[str]:
    filters = [f for f in [division, state, season] if f]
    filter_str = "  filters: " + ", ".join(filters) if filters else ""
    print(f"[search] Looking for '{team_name}'{filter_str} …")

    page.goto(f"https://web.gc.com/search?query={team_name.replace(' ', '+')}", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    time.sleep(3)

    # Collect ALL team links (not just name-matched) so we can show the full list
    all_candidates = []
    seen_ids = set()
    for link in page.query_selector_all("a[href*='/teams/']"):
        href = link.get_attribute("href") or ""
        text = normalize(link.inner_text())
        m = re.search(r"/teams/([^/\?]+)", href)
        if m and m.group(1) not in seen_ids:
            seen_ids.add(m.group(1))
            all_candidates.append({"id": m.group(1), "text": text})

    # Filter to candidates whose name matches the search term
    candidates = [c for c in all_candidates if team_name.lower() in c["text"].lower()]

    if not candidates:
        print("[search] No teams matched the search term. All results on page:")
        for c in all_candidates:
            print(f"         {c['id']}  —  {c['text'][:100]}")
        return None

    # Score each candidate by how many filters it matches
    def score(c: dict) -> int:
        t = c["text"].lower()
        s = 0
        if division and division.lower() in t:
            s += 10
        if season and season.lower() in t:
            s += 8
        if state and state.lower() in t:
            s += 5
        if sport and sport.lower() in t:
            s += 3
        return s

    candidates.sort(key=score, reverse=True)
    best = candidates[0]

    # Check which filters the best match satisfies
    missing = []
    if division and division.lower() not in best["text"].lower():
        missing.append(f"--division {division}")
    if season and season.lower() not in best["text"].lower():
        missing.append(f"--season {season}")
    if state and state.lower() not in best["text"].lower():
        missing.append(f"--state {state}")

    if missing:
        print(f"\n[search] ⚠️  No team matched: {', '.join(missing)}")
        print("[search] All matching teams found:")
        for c in candidates:
            print(f"         {c['id']}  —  {c['text'][:120]}")
        print("\n[search] To use a specific team, re-run with:")
        print(f"         --team-id <ID>   (copy the ID from the list above)")
        print("[search] Stopping — not proceeding with wrong team.")
        return None

    print(f"[search] ✓ Selected: {best['id']}  ({best['text'][:120]})")
    return best["id"]


# ---------------------------------------------------------------------------
# Step 2: Schedule
# ---------------------------------------------------------------------------

def scrape_schedule(page: Page, team_id: str, timeout: int) -> list[dict]:
    print("[schedule] Fetching schedule …")
    page.goto(f"https://web.gc.com/teams/{team_id}/schedule", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    time.sleep(4)

    # Use JS to walk each game link's DOM context and find:
    #   - event ID (from href)
    #   - nearest preceding month+year heading
    #   - full date with day from card text (e.g. "Sat May 30, 2:00 PM")
    #   - opponent, result, score (from link's own innerText)
    raw_games = page.evaluate("""
        () => {
            const MONTH_YEAR_RE = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\s+(\\d{4})\\b/i;
            const FULL_DATE_RE  = /\\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\s+(\\d{1,2})(?:,?\\s*(\\d{4}))?/i;
            const SCORE_RE = /(\\d+)\\s*[-\\u2013]\\s*(\\d+)/;
            const RESULT_RE = /\\b(W|L|T)\\b/;

            // Collect month+year header elements
            const dateEls = [];
            for (const el of document.querySelectorAll('div,span,p,h1,h2,h3,h4,li,section,header')) {
                if (el.children.length > 8) continue;
                const t = (el.innerText || '').trim();
                if (t.length < 5 || t.length > 150) continue;
                if (SCORE_RE.test(t)) continue;
                if (MONTH_YEAR_RE.test(t)) dateEls.push(el);
            }

            // Find the last month+year element that precedes link in DOM order
            function findMonthYear(link) {
                let best = '';
                for (const el of dateEls) {
                    if (el.compareDocumentPosition(link) & 4) {
                        const m = (el.innerText || '').match(MONTH_YEAR_RE);
                        if (m) best = m[0];
                    }
                }
                return best;
            }

            // Find smallest ancestor with exactly 1 score (= individual game card).
            // If link itself has a score, use the link.
            function findCard(link) {
                const linkText = (link.innerText || '').trim();
                if (SCORE_RE.test(linkText)) return link;
                let cur = link;
                for (let i = 0; i < 12; i++) {
                    cur = cur.parentElement;
                    if (!cur || cur === document.body) break;
                    const t = (cur.innerText || '').trim();
                    const scoreCount = (t.match(/(\\d+)\\s*[-\\u2013]\\s*(\\d+)/g) || []).length;
                    if (scoreCount === 1) return cur;
                    if (scoreCount > 3) break;
                }
                return link.parentElement || link;
            }

            const seen = new Set();
            const results = [];
            for (const link of document.querySelectorAll('a[href*="/schedule/"]')) {
                const href = link.href || '';
                const idMatch = href.match(/\\/schedule\\/([^/?#]+)/);
                if (!idMatch) continue;
                const eventId = idMatch[1];
                if (seen.has(eventId)) continue;
                seen.add(eventId);

                const date     = findMonthYear(link);
                const card     = findCard(link);
                const cardText = (card.innerText || '').trim();

                const score    = cardText.match(SCORE_RE);
                const res      = cardText.match(RESULT_RE);
                const vsM      = cardText.match(/(?:^|\\n)\\s*vs\\.?\\s+([^\\n]+)/i);
                const atM      = cardText.match(/(?:^|\\n)\\s*@\\s+([^\\n]+)/i);
                const fullDateM = cardText.match(FULL_DATE_RE);
                const fullDate  = fullDateM ? fullDateM[0] : '';

                results.push({
                    eventId,
                    date,
                    fullDate,
                    runsScored:  score ? parseInt(score[1]) : null,
                    runsAllowed: score ? parseInt(score[2]) : null,
                    result:      res   ? res[1] : '',
                    opponent:    vsM   ? vsM[1].trim() : (atM ? atM[1].trim() : ''),
                    homeAway:    vsM   ? 'home' : (atM ? 'away' : ''),
                });
            }
            return results;
        }
    """)

    # Month+year RE: matches "February 2026", "Mar 2026", etc.
    month_year_re = re.compile(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})",
        re.I,
    )

    games = []
    seen_py: set = set()
    for rg in (raw_games or []):
        eid = rg.get("eventId", "")
        if not eid or eid in seen_py:
            continue
        seen_py.add(eid)

        raw_date = rg.get("date", "")
        dm = month_year_re.search(raw_date) if raw_date else None
        clean_date = dm.group(0).strip() if dm else ""

        # Prefer the full date (with day) extracted from the card text
        full_date_raw = rg.get("fullDate", "").strip()

        games.append({
            "gc_event_id":    eid,
            "date":           clean_date,
            "full_date_raw":  full_date_raw,   # "May 30" or "May 30, 2026"
            "opponent":       rg.get("opponent", ""),
            "home_away":      rg.get("homeAway", ""),
            "result":         rg.get("result", ""),
            "runs_scored":    rg.get("runsScored"),
            "runs_allowed":   rg.get("runsAllowed"),
            "starting_pitcher": "",
        })

    dated  = sum(1 for g in games if g.get("date"))
    print(f"[schedule] Found {len(games)} events, {dated} with dates.")
    for i, rg in enumerate((raw_games or [])[:5]):
        print(f"[schedule] RAW[{i}] id={rg.get('eventId','')} date={rg.get('date','')} score={rg.get('runsScored')}-{rg.get('runsAllowed')} opp={rg.get('opponent','')}")
    for g in games[:3]:
        print(f"[schedule]   {g['date']} | {g['home_away']} | {g['opponent']} | {g['result']} {g['runs_scored']}-{g['runs_allowed']}")
    return games


def _parse_schedule_lines(lines: list[str], games: list[dict]):
    # Extract all fields directly from each game's raw_text (parent element text),
    # which already contains date, score, result, and opponent.
    date_re = re.compile(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(?:[,\s]+(\d{4}))?",
        re.I,
    )
    current_year = str(__import__('datetime').date.today().year)
    score_re  = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
    result_re = re.compile(r"\b(W|L|T)\b")

    for g in games:
        raw = g.get("raw_text", "")
        if not raw:
            continue

        # Date — search the raw_text directly (captures full date with day)
        dm = date_re.search(raw)
        if dm:
            year = dm.group(2) or current_year
            g["date"] = f"{dm.group(0).split(',')[0].strip()} {year}"
            # Also set full_date_raw so _parse_game_date gets the day
            g["full_date_raw"] = g["date"]

        # Score
        sm = score_re.search(raw)
        if sm:
            g["runs_scored"]  = int(sm.group(1))
            g["runs_allowed"] = int(sm.group(2))

        # Result (W/L/T)
        rm = result_re.search(raw)
        if rm:
            g["result"] = rm.group(1)

        # Opponent + home/away — scan each sub-line of raw_text
        for part in raw.split("\n"):
            part = part.strip()
            if part.lstrip().startswith("@"):
                g["home_away"] = "away"
                g["opponent"]  = part.lstrip().lstrip("@").strip()
            elif re.match(r"^vs\.?\s+", part, re.I):
                g["home_away"] = "home"
                g["opponent"]  = re.sub(r"^vs\.?\s*", "", part, flags=re.I).strip()


# ---------------------------------------------------------------------------
# Step 3: Play-by-play
# ---------------------------------------------------------------------------

# GC renders each play as TWO lines:
#   Line 1 (category header): "Ground Out"  ← no player name, no zone
#   Line 2 (full description): "Piper H grounded out to shortstop"  ← has both
# We skip category-only header lines to avoid storing duplicates with null names.
_PLAY_HEADERS = {
    'single','double','triple','home run','ground out','groundout',
    'fly out','flyout','line out','lineout','pop out','popout',
    'strikeout','struck out','walk','intentional walk','hit by pitch','hbp',
    'sacrifice fly','sac fly','fielder\'s choice','reached on error',
    'error','stolen base','caught stealing','wild pitch','passed ball',
    'balk','out','safe',
    'double play','triple play',
    'sacrifice bunt','sacrifice fly','sac bunt','sac fly','sacrifice',
}

# Known action verbs that indicate the LINE has player context (not a bare header)
# Includes both past tense and GC's present-tense recap format.
_ACTION_VERBS = (
    # Past tense
    'singled','doubled','tripled','homered','home run','struck out','strikeout',
    'grounded out','grounded into','flied out','lined out','popped out','popped up',
    'walked','hit by pitch','was hit by','reached on error','fielder\'s choice',
    'sacrifice fly','sacrifice bunt','sac fly','sac bunt','sacrificed','sacrifices',
    'bunts','bunted',
    # Present tense (GC live/recap format: "H Haviland singles", "E Jocys strikes out")
    'singles','doubles','triples','homers',
    'strikes out',
    'grounds out','grounds into a double play','grounds into',
    'flies into a triple play','flies into a double play','flies into',
    'lines into a triple play','lines into a double play','lines into',
    'grounds into a triple play',
    'hits an inside the park home run','hits an inside-the-park home run',
    'hits an rbi','hits an',
    'hits a hard ground ball','hits a ground ball','hits a line drive',
    'hits a fly ball','hits a deep fly ball','hits a pop',
    'hits into',
    'flies out','lines out',
    'pops out','pops up','pops into',
    'walks',
    'reaches on an error','reaches on error','reaches base on',
    'is hit by',
    'sacrifices','sacrifice',
)

def _extract_batter(line: str) -> str:
    """
    Extract batter name from a full play line like:
      "Piper H grounded out to shortstop"
      "Eli C singled to center field"
    Returns empty string if line is a bare category header.
    """
    ll = line.lower()
    # Must contain an action verb to be a real play line
    if not any(v in ll for v in _ACTION_VERBS):
        return ""
    # Name is text before the first action verb, trimmed
    for verb in sorted(_ACTION_VERBS, key=len, reverse=True):
        idx = ll.find(verb)
        if idx > 0:
            candidate = line[:idx].strip()
            words = candidate.split()
            # Valid name: 1-4 words, each starting with a capital letter.
            # Allow nickname tokens like "(Zo)" or "(Addie)" — strip leading "(" before checking.
            def _word_caps(w: str) -> bool:
                core = w.lstrip("(").rstrip(")")
                return bool(core) and core[0].isupper()
            if 1 <= len(words) <= 4 and all(_word_caps(w) for w in words if w):
                return candidate
    return ""


# Regex to extract the fielder who committed an error from play descriptions like:
#   "reaches on an error by left fielder Skylar E"
#   "reaches on an error by first baseman Addie B"
_ERROR_FIELDER_RE = re.compile(
    r'error\s+by\s+'
    r'(?:left\s+field(?:er)?|right\s+field(?:er)?|center\s+field(?:er)?|'
    r'first\s+base(?:man)?|second\s+base(?:man)?|third\s+base(?:man)?|'
    r'shortstop|short\s+stop|pitcher|catcher)\s+'
    r'([A-Z][A-Za-z]+(?:\s+[A-Z](?:[A-Za-z]*)?)?)',
    re.IGNORECASE,
)


def extract_error_fielder(line: str) -> Optional[str]:
    """Extract the name of the fielder who committed an error from a play line."""
    m = _ERROR_FIELDER_RE.search(line)
    if m:
        return m.group(1).strip()
    return None


def scrape_plays(page: Page, team_id: str, event_id: str,
                 scouted_home: bool, timeout: int):
    url = f"https://web.gc.com/teams/{team_id}/schedule/{event_id}/plays"
    print(f"[plays] {url}")

    try:
        page.goto(url, timeout=timeout)
        page.wait_for_load_state("networkidle", timeout=timeout)
        time.sleep(3)
    except PWTimeout:
        print(f"[plays] Timeout: {event_id}")
        return [], {}, {}, []

    lines = page_lines(page)

    plays = []
    catcher_stats = {"sb": 0, "cs": 0, "wp": 0, "pb": 0}
    pitcher_info = {"starting_pitcher": ""}

    current_inning = None
    current_half = None
    sp_set = False
    our_half = "bottom" if scouted_home else "top"

    for line in lines:
        inning_m = re.match(r"(top|bottom)\s+(?:of\s+)?(\d+)(?:st|nd|rd|th)?", line, re.I)
        if inning_m:
            current_half = inning_m.group(1).lower()
            current_inning = int(inning_m.group(2))
            continue

        if line.lower().strip() in _PLAY_HEADERS:
            continue

        pitcher = extract_pitcher_name(line)
        if pitcher:
            if not sp_set and current_inning == 1 and current_half == "top":
                pitcher_info["starting_pitcher"] = pitcher
                sp_set = True
            continue

        # Catcher / baserunning events on defense
        if current_half and current_half != our_half:
            tl = line.lower()
            if "wild pitch" in tl:
                catcher_stats["wp"] += 1
            elif "passed ball" in tl:
                catcher_stats["pb"] += 1
            elif "caught stealing" in tl or "picked off" in tl:
                catcher_stats["cs"] += 1
            elif "steals" in tl or "stolen base" in tl:
                catcher_stats["sb"] += 1

        outcome = classify_outcome(line)
        if not outcome:
            continue

        side = "offense" if current_half == our_half else "defense"
        zone = None if outcome in ZONE_EXEMPT else classify_zone(line)
        batter = _extract_batter(line)
        error_fielder = extract_error_fielder(line) if outcome == "Error" else None

        plays.append({
            "gc_event_id": event_id,
            "inning": current_inning,
            "half": current_half,
            "player_name": batter or None,
            "side": side,
            "outcome": outcome,
            "zone": zone,
            "error_fielder": error_fielder,
        })

    return plays, catcher_stats, pitcher_info, lines


# ---------------------------------------------------------------------------
# Step 4a: Infer fielding stats from lineup positions + play-by-play
# ---------------------------------------------------------------------------

# Map position full names/phrases to abbreviations
_POS_ABBR_MAP = {
    'pitcher': 'P',   'pitching': 'P',
    'catcher': 'C',   'catching': 'C',
    'first base': '1B', 'first baseman': '1B',
    'second base': '2B', 'second baseman': '2B',
    'third base': '3B', 'third baseman': '3B',
    'shortstop': 'SS', 'short stop': 'SS',
    'short field': 'SF', 'short fielder': 'SF',
    'left field': 'LF',   'left fielder': 'LF',
    'center field': 'CF', 'center fielder': 'CF',
    'right field': 'RF',  'right fielder': 'RF',
}

_POS_TO_INN_FIELD = {
    'P': 'inn_p', 'C': 'inn_c', '1B': 'inn_1b', '2B': 'inn_2b',
    '3B': 'inn_3b', 'SS': 'inn_ss', 'LF': 'inn_lf',
    'CF': 'inn_cf', 'RF': 'inn_rf', 'SF': 'inn_sf',
}

# Detects substitution / position-change lines:
# "lineup changed: Laila J in at catcher"
# "Laila J moves to shortstop"
# "Mia S now pitching"
_SUB_RE = re.compile(
    r'(?:lineup\s+changed?[:\s]+)?'
    r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\s+'
    r'(?:in\s+at|now\s+(?:at|playing|pitching)|moves?\s+to|entered?\s+(?:as|at))\s+'
    r'(pitcher|catcher|first\s+base(?:man)?|second\s+base(?:man)?|third\s+base(?:man)?|'
    r'shortstop|short\s+field(?:er)?|left\s+field(?:er)?|center\s+field(?:er)?|right\s+field(?:er)?)',
    re.IGNORECASE,
)

def _parse_pos_abbr(s: str) -> Optional[str]:
    sl = s.strip().lower()
    for phrase, abbr in _POS_ABBR_MAP.items():
        if phrase in sl:
            return abbr
    return None


def infer_fielding_from_lineup_and_plays(
    scouted_players: list[dict],
    play_lines: list[str],
    scouted_home: bool,
) -> tuple[list[dict], list[dict]]:
    """
    Infer per-player catching stats and innings-played from:
      - Box score lineup (player positions in order played)
      - Raw play-by-play lines (substitution events + catching events)

    Each player's positions list (e.g. ['C','SS']) records the order they played
    those positions. Play-by-play sub events tell us WHEN the switch happened.

    Returns (catching_stats, innings_played_stats).
    """
    our_half = 'bottom' if scouted_home else 'top'
    def_half = 'top'   if scouted_home else 'bottom'   # when WE are in the field

    # ---- Build initial position assignments from box score ----
    # player_name -> starting position
    player_to_pos: dict[str, str] = {}
    # position abbr -> player currently at that position
    pos_to_player: dict[str, str] = {}
    # player -> jersey number
    player_num: dict[str, str] = {}

    for p in scouted_players:
        name = p.get('name', '')
        if not name or name == 'TEAM':
            continue
        positions = [x.strip().upper() for x in p.get('positions', []) if x.strip()]
        if not positions:
            continue
        player_num[name] = p.get('num', '')
        starting = positions[0]
        player_to_pos[name] = starting
        pos_to_player[starting] = name

    if not player_to_pos:
        print(f"[infer] ✗ No players with positions found in box score — cannot infer innings")
        return [], []

    print(f"[infer] Starting positions: { {n: p for n, p in player_to_pos.items()} }")

    # ---- Accumulators ----
    # innings played: player -> {inn_p, inn_c, ...}
    inning_acc: dict[str, dict] = {
        name: {
            'player_name': name, 'player_num': player_num.get(name, ''),
            'inn_p': 0.0, 'inn_c': 0.0, 'inn_1b': 0.0, 'inn_2b': 0.0,
            'inn_3b': 0.0, 'inn_ss': 0.0, 'inn_lf': 0.0,
            'inn_cf': 0.0, 'inn_rf': 0.0, 'inn_sf': 0.0, 'inn_total': 0.0,
        }
        for name in player_to_pos
    }
    # catching stats: only for players who play C
    catching_acc: dict[str, dict] = {}
    for p in scouted_players:
        name = p.get('name', '')
        if not name or name == 'TEAM':
            continue
        positions = [x.strip().upper() for x in p.get('positions', []) if x.strip()]
        if 'C' in positions:
            catching_acc[name] = {
                'player_name': name, 'player_num': player_num.get(name, ''),
                'inn': 0.0, 'pb': 0, 'sb': 0, 'sb_att': 0,
                'cs': 0, 'cs_pct': None, 'pik': 0, 'ci': 0,
            }

    inning_re = re.compile(r'(top|bottom)\s+(?:of\s+)?(\d+)(?:st|nd|rd|th)?', re.I)

    current_inning: Optional[int] = None
    current_half:   Optional[str] = None
    # Snapshot of positions at the start of each defensive half-inning
    # player -> set of positions played THIS defensive half-inning
    this_half_at_pos: dict[str, set] = {}

    def _close_half_inning():
        """Credit each player for the defensive half-inning just completed."""
        for pname, pos_set in this_half_at_pos.items():
            if pname not in inning_acc:
                continue
            for pos in pos_set:
                fld = _POS_TO_INN_FIELD.get(pos)
                if fld:
                    inning_acc[pname][fld] += 1.0
                    inning_acc[pname]['inn_total'] += 1.0

    def _open_half_inning():
        """Snapshot current defensive positions at the start of a new defensive half."""
        this_half_at_pos.clear()
        for pname, pos in player_to_pos.items():
            this_half_at_pos.setdefault(pname, set()).add(pos)

    for line in play_lines:
        # ---- Inning header ----
        inning_m = inning_re.match(line)
        if inning_m:
            new_half   = inning_m.group(1).lower()
            new_inning = int(inning_m.group(2))

            # Close out the previous half if it was a defensive half
            if current_half == def_half and current_inning is not None:
                _close_half_inning()

            current_inning = new_inning
            current_half   = new_half

            if current_half == def_half:
                _open_half_inning()
            continue

        # ---- Substitution / position change ----
        sub_m = _SUB_RE.search(line)
        if sub_m:
            entering_raw = sub_m.group(1).strip()
            new_pos = _parse_pos_abbr(sub_m.group(2))
            if new_pos:
                # Match entering player by name prefix (handles abbreviated names)
                matched = None
                for pname in player_to_pos:
                    if pname.lower().startswith(entering_raw.lower()[:6]):
                        matched = pname
                        break
                if matched:
                    old_pos = player_to_pos.get(matched)
                    print(f"[infer] Sub: {matched} {old_pos} → {new_pos}  (line: '{line[:60]}')")
                    player_to_pos[matched] = new_pos
                    pos_to_player[new_pos] = matched
                    if old_pos and old_pos != new_pos:
                        if pos_to_player.get(old_pos) == matched:
                            del pos_to_player[old_pos]
                    # Track new position in current defensive half-inning
                    if current_half == def_half:
                        this_half_at_pos.setdefault(matched, set()).add(new_pos)
            continue

        # ---- Catching events (when we are in the field) ----
        if current_half == def_half and current_inning is not None:
            current_catcher = pos_to_player.get('C')
            if current_catcher and current_catcher in catching_acc:
                tl = line.lower()
                if 'stolen base' in tl or 'steals' in tl:
                    catching_acc[current_catcher]['sb']     += 1
                    catching_acc[current_catcher]['sb_att'] += 1
                elif 'caught stealing' in tl or 'picked off' in tl:
                    catching_acc[current_catcher]['cs']     += 1
                    catching_acc[current_catcher]['sb_att'] += 1
                elif 'passed ball' in tl:
                    catching_acc[current_catcher]['pb'] += 1

    # Close the final half-inning
    if current_half == def_half and current_inning is not None:
        _close_half_inning()

    # ---- Finalize catching stats ----
    for name, stats in catching_acc.items():
        stats['inn'] = inning_acc.get(name, {}).get('inn_c', 0.0)
        total_att = stats['sb_att']
        stats['cs_pct'] = (
            round(stats['cs'] / total_att * 100, 2) if total_att > 0 else None
        )

    catching_list = [
        v for v in catching_acc.values()
        if v['inn'] > 0 or v['sb'] > 0 or v['pb'] > 0 or v['cs'] > 0
    ]
    innings_list = [v for v in inning_acc.values() if v['inn_total'] > 0]

    print(f"[infer] Fielding: {len(innings_list)} players, "
          f"{len(catching_list)} catchers with stats")
    return catching_list, innings_list


# ---------------------------------------------------------------------------
# Step 4: Box scores — catchers + full per-game stats
# ---------------------------------------------------------------------------

# Keywords that are definitely not player/team names in box score context
_BOX_SKIP = {
    'LINEUP','PITCHING','AB','R','H','RBI','BB','SO','IP','ER','TEAM',
    'HOME','SCHEDULE','STATS','Back to Schedule','Get the App','Status',
    'Privacy','Terms','CA Disclosures','Your Privacy Choices',
}
_EXTRA_KEYS = {'2B','3B','HR','HBP','SB','CS','E','TB','WP','SF'}
_NUM_RE = re.compile(r'^#(\d+)(?:\s*\(([^)]+)\))?$')
_VAL_RE = re.compile(r'^-?\d+\.?\d*$')


def _parse_extras(lines: list[str]) -> dict:
    """Parse extra-stat lines like '2B: Mila R' or 'SB: Eli C 2, Karsyn H'."""
    extras = {}
    for line in lines:
        m = re.match(r'^(2B|3B|HR|HBP|SB|CS|E|TB|WP|SF|Pitches-Strikes|Batters Faced):\s*(.+)$', line)
        if m:
            extras[m.group(1)] = m.group(2)
    return extras


def _count_from_extras(extras: dict, key: str) -> int:
    """Count total occurrences from an extras line like 'Eli C 2, Jesalynn Q, Karsyn H'."""
    raw = extras.get(key, "")
    if not raw:
        return 0
    total = 0
    # Split on comma, each entry is "Name [count]"
    for part in raw.split(","):
        part = part.strip()
        m = re.search(r'\b(\d+)$', part)
        total += int(m.group(1)) if m else 1
    return total


def _parse_lineup_section(lines: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Parse a LINEUP section. Returns (players, catchers).
    Each player dict has: name, num, positions, ab, r, h, rbi, bb, so
    """
    BAT_COLS = ['ab', 'r', 'h', 'rbi', 'bb', 'so']
    players = []
    catchers = []
    i = 0
    # Skip column header lines
    while i < len(lines) and lines[i] in ('AB','R','H','RBI','BB','SO'):
        i += 1

    while i < len(lines):
        line = lines[i]
        if line in _BOX_SKIP or line in _EXTRA_KEYS or re.match(r'^(2B|3B|HR|WP|SF|HBP|SB|CS|E|TB):', line):
            i += 1
            continue
        if line == 'TEAM':
            i += 1
            team_vals = []
            while i < len(lines) and _VAL_RE.match(lines[i]) and len(team_vals) < 6:
                team_vals.append(lines[i])
                i += 1
            continue

        # Try combined format first: "Mia S #2 (SS, P, CF)" all on one line
        _COMBINED_RE = re.compile(r'^(.+?)\s+#(\d+)(?:\s*\(([^)]+)\))?$')
        combined_m = _COMBINED_RE.match(line)
        # Split format: name on this line, "#num (pos)" on next line
        split_nm = _NUM_RE.match(lines[i + 1]) if i + 1 < len(lines) else None

        if combined_m and not _NUM_RE.match(line):
            player_name = combined_m.group(1).strip()
            player_num  = combined_m.group(2)
            positions   = [p.strip() for p in (combined_m.group(3) or '').split(',') if p.strip()]
            i += 1
        elif split_nm:
            player_name = line
            player_num  = split_nm.group(1)
            positions   = [p.strip() for p in (split_nm.group(2) or '').split(',') if p.strip()]
            i += 2
        else:
            i += 1
            continue

        # Read stat columns (shared by both formats)
        vals = {}
        for col in BAT_COLS:
            if i < len(lines) and _VAL_RE.match(lines[i]):
                vals[col] = safe_int(lines[i])
                i += 1
            else:
                vals[col] = 0
        row = {'name': player_name, 'num': player_num, 'positions': positions, **vals}
        players.append(row)
        if 'C' in positions:
            catchers.append({'name': player_name, 'num': player_num})

    return players, catchers


def _parse_pitching_section(lines: list[str]) -> list[dict]:
    """Parse a PITCHING section. Returns list of pitcher dicts."""
    PIT_COLS = ['ip', 'h', 'r', 'er', 'bb', 'so']
    pitchers = []
    i = 0
    while i < len(lines) and lines[i] in ('IP','H','R','ER','BB','SO'):
        i += 1

    while i < len(lines):
        line = lines[i]
        if line in _BOX_SKIP or re.match(r'^(WP|HBP|Pitches-Strikes|Batters Faced):', line):
            i += 1
            continue
        if line == 'TEAM':
            i += 1
            while i < len(lines) and _VAL_RE.match(lines[i]):
                i += 1
            continue
        if i + 1 < len(lines) and _NUM_RE.match(lines[i + 1]):
            nm = _NUM_RE.match(lines[i + 1])
            pitcher_name = line
            pitcher_num = nm.group(1)
            i += 2
            vals = {}
            for col in PIT_COLS:
                if i < len(lines) and _VAL_RE.match(lines[i]):
                    vals[col] = safe_float(lines[i]) if col == 'ip' else safe_int(lines[i])
                    i += 1
                else:
                    vals[col] = 0
            pitchers.append({'name': pitcher_name, 'num': pitcher_num, **vals})
        else:
            i += 1

    return pitchers


def _load_box_score_page(page: Page, team_id: str, team_slug: Optional[str],
                          event_id: str, timeout: int) -> list[str]:
    """Navigate to the box score page and return rendered lines."""
    if team_slug:
        url = f"https://web.gc.com/teams/{team_id}/{team_slug}/schedule/{event_id}/box-score"
    else:
        url = f"https://web.gc.com/teams/{team_id}/schedule/{event_id}/box-score"
    try:
        page.goto(url, timeout=timeout)
        # Wait for actual box score content rather than networkidle —
        # GC's React SPA has background polling that prevents networkidle.
        # "BATTING" appears once the game data is rendered.
        try:
            page.wait_for_selector("text=BATTING", timeout=timeout)
        except PWTimeout:
            # Fall back to a fixed sleep if the selector never appears
            # (e.g. game with no batting stats, or slow load)
            time.sleep(10)
    except PWTimeout:
        print(f"[boxscore] Timeout navigating: {event_id}")
        return []
    return page_lines(page)


def get_team_slug(page: Page, team_id: str, timeout: int) -> Optional[str]:
    """
    Navigate to the team schedule and capture the URL slug.
    Tries three methods in order:
      1. Slug in the redirected page URL
      2. Slug extracted from a game event link on the page
      3. Returns None (falls back to short URL — may load wrong team)
    """
    page.goto(f"https://web.gc.com/teams/{team_id}/schedule", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    time.sleep(3)

    print(f"[slug] Page URL after redirect: {page.url}")

    # Method 1: slug in the redirected URL
    m = re.search(rf'/teams/{re.escape(team_id)}/([^/]+)/schedule', page.url)
    if m:
        slug = m.group(1)
        print(f"[slug] ✓ Found via URL redirect: '{slug}'")
        return slug

    # Method 2: extract from any team link containing a slug
    # Links look like /teams/{team_id}/{slug}/schedule or /teams/{team_id}/{slug}/schedule/{event_id}
    tid_esc = re.escape(team_id)
    all_hrefs = []
    for link in page.query_selector_all(f"a[href*='/teams/{team_id}/']"):
        href = link.get_attribute("href") or ""
        all_hrefs.append(href)
        m2 = re.search(rf'/teams/{tid_esc}/([^/]+)/(?:schedule|team|season-stats|roster)(?:/|$)', href)
        if m2 and m2.group(1) not in ('schedule', 'stats', 'roster', 'team', 'season-stats'):
            slug = m2.group(1)
            print(f"[slug] ✓ Found via event link: '{slug}'  (from {href[:80]})")
            return slug

    print(f"[slug] ✗ Not found. Scanned {len(all_hrefs)} links:")
    for h in all_hrefs[:10]:
        print(f"         {h}")
    print(f"[slug] Falling back to short URL — box score may show wrong team's stats")
    return None


def parse_full_box_score(lines: list[str], scouted_name: str) -> dict:
    """
    Parse a full box score page into scouted and opponent sections.
    Returns:
      { 'scouted': {batting, pitching, catchers, extras_bat, extras_pit},
        'opponent': {batting, pitching, catchers, extras_bat, extras_pit} }
    """
    # Find LINEUP positions — GC renders "LINEUP" as its own line in the box score
    team_sections = []  # (lineup_idx, team_name)
    for i, line in enumerate(lines):
        # Handle both exact match and "LINEUP" embedded in a longer line
        if line == 'LINEUP' or line.startswith('LINEUP\t') or '\tLINEUP\t' in line:
            # Look back up to 10 lines for a team name
            for j in range(i - 1, max(-1, i - 11), -1):
                candidate = lines[j]
                if (candidate and len(candidate) > 4
                        and candidate not in _BOX_SKIP
                        and not _VAL_RE.match(candidate)
                        and not candidate.startswith('#')
                        and not re.match(r'^[A-Z]{1,3}(\s+[A-Z]{1,3})*$', candidate)):  # skip all-caps column headers
                    team_sections.append((i, candidate))
                    break

    print(f"[boxscore] parse: {len(team_sections)} team sections found: "
          f"{[s[1][:40] for s in team_sections]}")

    if not team_sections:
        # Debug: show first 40 lines of the page to help diagnose
        print(f"[boxscore] ✗ No LINEUP anchors found. First 40 page lines:")
        for ln in lines[:40]:
            print(f"           '{ln}'")
        return {}

    result = {}
    for sec_idx, (lineup_idx, team_name) in enumerate(team_sections):
        # Determine section boundaries
        next_lineup = team_sections[sec_idx + 1][0] if sec_idx + 1 < len(team_sections) else len(lines)

        # Find PITCHING within this section
        pitching_idx = next(
            (i for i in range(lineup_idx, next_lineup) if lines[i] == 'PITCHING'), -1
        )

        bat_end = pitching_idx if pitching_idx > lineup_idx else next_lineup
        bat_lines = lines[lineup_idx + 1 : bat_end]
        pit_lines = lines[pitching_idx + 1 : next_lineup] if pitching_idx >= 0 else []

        players, catchers = _parse_lineup_section(bat_lines)
        pitchers = _parse_pitching_section(pit_lines)
        extras_bat = _parse_extras(bat_lines)
        extras_pit = _parse_extras(pit_lines)

        key = 'scouted' if _team_matches(scouted_name, team_name) else 'opponent'
        print(f"[boxscore] section '{team_name[:40]}' → '{key}' "
              f"({len(players)} batters, {len(pitchers)} pitchers)")
        result[key] = {
            'team_name': team_name,
            'batting': players,
            'pitching': pitchers,
            'catchers': catchers,
            'extras_bat': extras_bat,
            'extras_pit': extras_pit,
        }

    if 'scouted' not in result:
        print(f"[boxscore] ✗ scouted_name='{scouted_name}' did not match any section. "
              f"Sections: {[s[1] for s in team_sections]}")

    return result


_BOX_DATE_RE = re.compile(
    r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*[\s,]+'     # "Sat "
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+'  # "May "
    r'(\d{1,2})'                                      # "30"
    r'(?:[,\s]+(\d{4}))?',                            # optional ", 2026"
    re.IGNORECASE,
)

def _extract_date_from_lines(lines: list[str]) -> str:
    """
    Scan the first 20 lines of box score text for a date header like
    'Sat May 30, 2:00 PM - 3:00 PM ET FINAL' and return 'May 30 YYYY'.
    Returns '' if not found.
    """
    from datetime import date as _date
    current_year = str(_date.today().year)
    for line in lines[:20]:
        m = _BOX_DATE_RE.search(line)
        if m:
            month = m.group(2)
            day   = m.group(3)
            year  = m.group(4) or current_year
            return f"{month} {day} {year}"
    return ''


def scrape_box_score(page: Page, team_id: str, team_slug: Optional[str],
                     event_id: str, team_name: str, timeout: int) -> dict:
    """
    Load and parse a game box score.
    Returns parse_full_box_score result dict with an extra 'game_date_raw' key,
    or {} on failure.
    """
    lines = _load_box_score_page(page, team_id, team_slug, event_id, timeout)
    if not lines:
        return {}
    result = parse_full_box_score(lines, team_name)
    # Extract the full game date (day + month + year) from the page header
    result['game_date_raw'] = _extract_date_from_lines(lines)
    scouted = result.get('scouted', {})
    catchers = scouted.get('catchers', [])
    print(f"[boxscore] {event_id}: {len(scouted.get('batting',[]))} batters, "
          f"{len(scouted.get('pitching',[]))} pitchers, "
          f"catchers={[c['name'] for c in catchers]}"
          f"  date={result['game_date_raw'] or '?'}")
    return result


# ---------------------------------------------------------------------------
# Step 4b: Game Stats page — Catching + Innings Played (admin teams only)
# ---------------------------------------------------------------------------

_GS_PLAYER_RE = re.compile(r'^(.+?),\s*#(\d+)$')

def _gs_player(line: str) -> Optional[tuple]:
    """Parse 'First Last, #87' → (name, num). Returns None if not a player line."""
    m = _GS_PLAYER_RE.match(line.strip())
    return (m.group(1).strip(), m.group(2)) if m else None

def _click_gs_tab(page: Page, label: str):
    """Click a tab by label. Tries button, <a>, and role=tab in order."""
    print(f"[gamestats] Clicking tab '{label}' …")
    for selector in [
        f"button:has-text('{label}')",
        f"a:has-text('{label}')",
        f"[role='tab']:has-text('{label}')",
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1000):
                el.click(timeout=3000)
                time.sleep(2)
                print(f"[gamestats] ✓ Clicked '{label}' via {selector.split(':')[0]}")
                return
        except Exception:
            continue
    # Nothing matched — dump visible tab elements to help diagnose
    print(f"[gamestats] ✗ Tab '{label}' not found. Visible tab-like elements:")
    for sel in ["button", "a[role='tab']", "[role='tab']", "nav a"]:
        try:
            els = page.locator(sel).all()
            for el in els[:5]:
                try:
                    txt = el.inner_text(timeout=500).strip()
                    if txt:
                        print(f"           {sel}: '{txt}'")
                except Exception:
                    pass
        except Exception:
            pass

def _load_game_stats_page(page: Page, team_id: str, event_id: str, timeout: int,
                           team_slug: Optional[str] = None) -> bool:
    """Navigate to game-stats page. Returns True if loaded successfully."""
    if team_slug:
        url = f"https://web.gc.com/teams/{team_id}/{team_slug}/schedule/{event_id}/game-stats"
    else:
        url = f"https://web.gc.com/teams/{team_id}/schedule/{event_id}/game-stats"
    print(f"[gamestats] Loading: {url}")
    try:
        page.goto(url, timeout=timeout)
        page.wait_for_load_state("networkidle", timeout=timeout)
        time.sleep(5)
    except PWTimeout:
        print(f"[gamestats] Timeout loading game-stats page")
        return False
    body = page.inner_text("body")
    ok = "doesn't exist" not in body and "Want live stats" not in body and "Download the app" not in body
    print(f"[gamestats] Page loaded OK: {ok}  (final URL: {page.url})")
    return ok


def scrape_game_catching(page: Page, team_id: str, event_id: str,
                          loaded: bool, timeout: int,
                          team_slug: Optional[str] = None) -> list[dict]:
    """
    Parse Catching tab from a game-stats page already loaded.
    `loaded` = True if _load_game_stats_page was already called for this event.
    """
    if not loaded:
        if not _load_game_stats_page(page, team_id, event_id, timeout, team_slug):
            return []
    _click_gs_tab(page, "Fielding")
    _click_gs_tab(page, "Catching")

    # Wait for catching table content to appear (React re-render may lag behind click)
    try:
        page.wait_for_selector("text=SB-ATT", timeout=6000)
    except Exception:
        print(f"[gamestats] WARNING: 'SB-ATT' column never appeared — catching tab may not have rendered")
    time.sleep(1)

    lines = page_lines(page)
    CATCH_HEADERS = {'INN','PB','SB','SB-ATT','CS','CS%','PIK','CI','Player'}
    CATCH_COLS    = ['inn','pb','sb','sb_att_raw','cs','cs_pct','pik','ci']

    # Anchor to the catching table: find 'Player' immediately followed by 'INN'
    table_start = None
    for idx in range(len(lines) - 1):
        if lines[idx] == 'Player' and lines[idx + 1] == 'INN':
            table_start = idx
            break

    if table_start is None:
        print(f"[gamestats] ✗ Catching table not found in page")
        return []

    # Skip past all column headers
    i = table_start
    while i < len(lines) and lines[i] in CATCH_HEADERS:
        i += 1

    # Parse player rows until the TEAM totals row
    results = []
    while i < len(lines):
        line = lines[i]
        if line == 'Team':
            break  # hit the totals row — done
        parsed = _gs_player(line)
        if parsed:
            name, num = parsed
            i += 1
            vals = []
            while len(vals) < len(CATCH_COLS) and i < len(lines):
                v = lines[i]
                if _gs_player(v) or v == 'Team':
                    break
                vals.append(v)
                i += 1
            # Pad if fewer values than expected
            while len(vals) < len(CATCH_COLS):
                vals.append('0')

            # SB-ATT format: "2-2" → attempts = second number
            sb_att_raw = vals[3]
            sb_att_parts = sb_att_raw.split('-')
            sb_att = safe_int(sb_att_parts[1]) if len(sb_att_parts) > 1 else 0

            results.append({
                'player_name': name, 'player_num': num,
                'inn':    safe_float(vals[0]),
                'pb':     safe_int(vals[1]),
                'sb':     safe_int(vals[2]),
                'sb_att': sb_att,
                'cs':     safe_int(vals[4]),
                'cs_pct': safe_float(vals[5]),
                'pik':    safe_int(vals[6]),
                'ci':     safe_int(vals[7]),
            })
        else:
            i += 1

    print(f"[gamestats] Catching {event_id}: {len(results)} catchers")
    return results


def scrape_game_innings(page: Page, team_id: str, event_id: str,
                         loaded: bool, timeout: int,
                         team_slug: Optional[str] = None) -> list[dict]:
    """Parse Innings Played tab from a game-stats page."""
    if not loaded:
        if not _load_game_stats_page(page, team_id, event_id, timeout, team_slug):
            return []
    _click_gs_tab(page, "Fielding")
    _click_gs_tab(page, "Innings Played")

    # Wait for innings content to appear
    try:
        page.wait_for_selector("text=Total", timeout=6000)
    except Exception:
        print(f"[gamestats] WARNING: Innings Played tab may not have rendered")
    time.sleep(1)

    lines = page_lines(page)

    INN_COLS = ['inn_p','inn_c','inn_1b','inn_2b','inn_3b','inn_ss',
                'inn_lf','inn_cf','inn_rf','inn_sf','inn_total']
    INN_HEADERS = {'P','C','1B','2B','3B','SS','LF','CF','RF','SF','Total','Player',
                   'Innings played at pitcher','Innings played at catcher',
                   'Innings played at first base','Innings played at second base',
                   'Innings played at third base','Innings played at shortstop',
                   'Innings played at left field','Innings played at center field',
                   'Innings played at right field','Innings played at short field',
                   'Total innings played'}
    n_cols = len(INN_COLS)

    # Collect player names and all numeric tokens
    players = []
    num_tokens = []
    for line in lines:
        if _gs_player(line):
            players.append(_gs_player(line))
        elif line in INN_HEADERS or line in _BOX_SKIP or 'Get the App' in line:
            continue
        elif _VAL_RE.match(line):
            num_tokens.append(line)

    n_players = len(players)
    if n_players == 0:
        return []

    # GC uses column-major layout (all P innings for all players, then all C, etc.)
    results = []
    for idx, (name, num) in enumerate(players):
        row = {'player_name': name, 'player_num': num}
        for col_i, col in enumerate(INN_COLS):
            token_idx = col_i * n_players + idx
            row[col] = safe_float(num_tokens[token_idx]) if token_idx < len(num_tokens) else 0.0
        results.append(row)

    print(f"[gamestats] Innings {event_id}: {len(results)} players")
    return results


# ---------------------------------------------------------------------------
# Step 5: Season stats (admin) or aggregated box score stats (non-admin)
# ---------------------------------------------------------------------------

def is_stats_locked(page: Page, team_id: str, timeout: int) -> bool:
    """Return True if the season-stats page requires admin access."""
    try:
        page.goto(f"https://web.gc.com/teams/{team_id}/season-stats", timeout=timeout)
        page.wait_for_load_state("networkidle", timeout=timeout)
        time.sleep(4)
    except PWTimeout:
        return True
    text = page.inner_text("body")
    return "Want live stats" in text or "Download the app" in text


def scrape_batting_stats(page: Page, team_id: str, timeout: int) -> list[dict]:
    print("[stats] Batting (season-stats page) …")
    # Already on season-stats from is_stats_locked check — try clicking Batting tab
    try:
        page.locator("button:has-text('Batting'), a:has-text('Batting')").first.click(timeout=3000)
        time.sleep(2)
    except Exception:
        pass
    return _parse_stat_table(page_lines(page), "batting")


def scrape_pitching_stats(page: Page, timeout: int) -> list[dict]:
    print("[stats] Pitching (season-stats page) …")
    try:
        page.locator("button:has-text('Pitching'), a:has-text('Pitching')").first.click(timeout=3000)
        time.sleep(3)
    except Exception:
        pass
    return _parse_stat_table(page_lines(page), "pitching")


def aggregate_stats_from_box_scores(box_scores: list[dict], team_name: str) -> tuple[list, list]:
    """
    Aggregate per-game box score data into season batting and pitching totals.
    Returns (batting_rows, pitching_rows) in the same format as scrape_*_stats().
    """
    bat_acc: dict[tuple, dict] = {}  # (name, num) → accumulated stats
    pit_acc: dict[tuple, dict] = {}

    for bs in box_scores:
        scouted = bs.get('scouted', {})

        # --- Batting ---
        for p in scouted.get('batting', []):
            if p['name'] == 'TEAM':
                continue
            key = (p['name'], p['num'])
            if key not in bat_acc:
                bat_acc[key] = {'player_name': p['name'], 'player_num': p['num'],
                                'gp': 0, 'pa': 0, 'ab': 0, 'h': 0, 'r': 0,
                                'rbi': 0, 'bb': 0, 'k': 0, 'hbp': 0,
                                'doubles': 0, 'triples': 0, 'hr': 0, 'sb': 0, 'cs': 0}
            a = bat_acc[key]
            a['gp'] += 1
            a['ab'] += p.get('ab') or 0
            a['h']  += p.get('h')  or 0
            a['r']  += p.get('r')  or 0
            a['rbi']+= p.get('rbi')or 0
            a['bb'] += p.get('bb') or 0
            a['k']  += p.get('so') or 0

        # Apply extras (per-game doubles/triples/hr/hbp/sb/cs)
        extras = scouted.get('extras_bat', {})
        if extras:
            # Attribute extras back to players by name matching
            for xkey, acc_field in [('2B','doubles'),('3B','triples'),('HR','hr'),
                                     ('HBP','hbp'),('SB','sb'),('CS','cs')]:
                for part in extras.get(xkey, '').split(','):
                    part = part.strip()
                    if not part:
                        continue
                    cnt_m = re.search(r'\b(\d+)$', part)
                    cnt = int(cnt_m.group(1)) if cnt_m else 1
                    name_part = re.sub(r'\s*\d+$', '', part).strip()
                    # Find matching player key (name starts with name_part)
                    for (pname, pnum) in bat_acc:
                        if pname.lower().startswith(name_part.lower()):
                            bat_acc[(pname, pnum)][acc_field] += cnt
                            break

        # --- Pitching ---
        for p in scouted.get('pitching', []):
            if p['name'] == 'TEAM':
                continue
            key = (p['name'], p['num'])
            if key not in pit_acc:
                pit_acc[key] = {'player_name': p['name'], 'player_num': p['num'],
                                'gp': 0, 'gs': 0, 'ip': 0.0, 'bf': 0,
                                'h': 0, 'r': 0, 'er': 0, 'bb': 0, 'k': 0,
                                'wp': 0, 'hbp': 0, 'num_pitches': 0}
            a = pit_acc[key]
            a['gp'] += 1
            a['ip'] += p.get('ip') or 0.0
            a['h']  += p.get('h')  or 0
            a['r']  += p.get('r')  or 0
            a['er'] += p.get('er') or 0
            a['bb'] += p.get('bb') or 0
            a['k']  += p.get('so') or 0

        pit_extras = scouted.get('extras_pit', {})
        # WP
        for part in pit_extras.get('WP', '').split(','):
            part = part.strip()
            if not part:
                continue
            cnt_m = re.search(r'\b(\d+)$', part)
            cnt = int(cnt_m.group(1)) if cnt_m else 1
            name_part = re.sub(r'\s*\d+$', '', part).strip()
            for (pname, pnum) in pit_acc:
                if pname.lower().startswith(name_part.lower()):
                    pit_acc[(pname, pnum)]['wp'] += cnt
                    break
        # HBP
        for part in pit_extras.get('HBP', '').split(','):
            part = part.strip()
            if not part:
                continue
            cnt_m = re.search(r'\b(\d+)$', part)
            cnt = int(cnt_m.group(1)) if cnt_m else 1
            name_part = re.sub(r'\s*\d+$', '', part).strip()
            for (pname, pnum) in pit_acc:
                if pname.lower().startswith(name_part.lower()):
                    pit_acc[(pname, pnum)]['hbp'] += cnt
                    break
        # Pitch counts from "Pitches-Strikes: Name p-s"
        for ps_entry in pit_extras.get('Pitches-Strikes', '').split(','):
            ps_m = re.search(r'(.+?)\s+(\d+)-\d+$', ps_entry.strip())
            if ps_m:
                name_part = ps_m.group(1).strip()
                pitches = int(ps_m.group(2))
                for (pname, pnum) in pit_acc:
                    if pname.lower().startswith(name_part.lower()):
                        pit_acc[(pname, pnum)]['num_pitches'] += pitches
                        break

    # Convert to list format; compute pa, avg, era, whip
    batting_rows = []
    for (name, num), a in bat_acc.items():
        ab = a['ab']
        h  = a['h']
        bb = a['bb']
        hbp= a['hbp']
        pa = ab + bb + hbp
        avg = round(h / ab, 3) if ab > 0 else None
        singles = h - a['doubles'] - a['triples'] - a['hr']
        tb = singles + 2*a['doubles'] + 3*a['triples'] + 4*a['hr']
        slg = round(tb / ab, 3) if ab > 0 else None
        batting_rows.append({
            'player_name': name, 'player_num': num,
            'gp': a['gp'], 'pa': pa, 'ab': ab, 'avg': avg, 'slg': slg,
            'h': h, 'singles': singles, 'doubles': a['doubles'],
            'triples': a['triples'], 'hr': a['hr'],
            'rbi': a['rbi'], 'r': a['r'], 'bb': bb, 'k': a['k'],
            'hbp': hbp, 'sb': a['sb'], 'cs': a['cs'],
        })

    pitching_rows = []
    for (name, num), a in pit_acc.items():
        ip = a['ip']
        er = a['er']
        # IP stored as decimal innings (e.g. 6.1 = 6 full + 1 out)
        outs = int(ip) * 3 + round((ip % 1) * 10)
        innings = outs / 3
        era   = round(er / innings * 7, 2) if innings > 0 else None  # per 7 innings (softball)
        whip_val = round((a['bb'] + a['h']) / innings, 2) if innings > 0 else None
        pitching_rows.append({
            'player_name': name, 'player_num': num,
            'gp': a['gp'], 'ip': ip, 'h': a['h'], 'r': a['r'], 'er': er,
            'bb': a['bb'], 'k': a['k'], 'wp': a['wp'], 'hbp': a['hbp'],
            'num_pitches': a['num_pitches'], 'era': era, 'whip': whip_val,
        })

    print(f"[stats] Aggregated from box scores: {len(batting_rows)} batters, {len(pitching_rows)} pitchers")
    return batting_rows, pitching_rows


def _parse_stat_table(lines: list[str], stat_type: str) -> list[dict]:
    BATTING_COLS = [
        "gp","pa","ab","avg","obp","ops","slg","h","singles","doubles",
        "triples","hr","rbi","r","bb","k","k_looking","hbp","sac","sf","roe","fc"
    ]
    PITCHING_COLS = [
        "ip","gp","gs","bf","num_pitches","w","l","sv","svo","bs","sv_pct",
        "h","r","er","bb","k","k_looking","hbp","era","whip","lob","bk"
    ]
    cols = BATTING_COLS if stat_type == "batting" else PITCHING_COLS

    name_re = re.compile(r"^([A-Za-z][A-Za-z\s'\-\.]+),?\s*#?(\d+)$")
    alt_name_re = re.compile(r"^([A-Za-z][A-Za-z\s'\-\.]+)$")
    num_re = re.compile(r"^#?(\d+)$")

    players = []
    i = 0
    while i < len(lines):
        m = name_re.match(lines[i])
        if m:
            players.append({"name": m.group(1).strip(), "num": m.group(2)})
            i += 1
            continue
        if alt_name_re.match(lines[i]) and i + 1 < len(lines) and num_re.match(lines[i + 1]):
            players.append({"name": lines[i].strip(), "num": num_re.match(lines[i + 1]).group(1)})
            i += 2
            continue
        i += 1

    num_tokens = []
    for line in lines:
        for tok in line.split():
            if re.match(r"^-?\d+\.?\d*$", tok):
                num_tokens.append(tok)

    n_players = len(players)
    n_cols = len(cols)
    if n_players == 0:
        return []

    chunk = num_tokens[:n_players * n_cols]
    stats = []
    for idx, p in enumerate(players):
        row = {"player_name": p["name"], "player_num": p["num"]}
        for col_i, col_name in enumerate(cols):
            token_idx = col_i * n_players + idx  # column-major layout
            if token_idx < len(chunk):
                val = chunk[token_idx]
                if col_name in ("avg","obp","ops","slg","era","whip","ip","sv_pct","lob"):
                    row[col_name] = safe_float(val)
                else:
                    row[col_name] = safe_int(val)
            else:
                row[col_name] = None
        stats.append(row)
    return stats


# ---------------------------------------------------------------------------
# Per-team scrape (called once per team inside a shared browser session)
# ---------------------------------------------------------------------------

def scrape_one_team(sb: SupabaseClient, page: Page, gc_team_id: str,
                    team_name: str, args) -> dict:
    """Scrape a single team and write everything to Supabase. Returns summary dict."""
    print(f"\n{'='*60}")
    print(f"TEAM: {team_name}  ({gc_team_id})")
    print(f"{'='*60}")

    # If team_name is just the raw ID (no --team NAME provided), look up the real name
    # from Supabase so box score matching works correctly.
    if team_name == gc_team_id:
        stored = get_team_name_from_db(sb, gc_team_id)
        if stored:
            team_name = stored
            print(f"[team] Resolved name from DB: '{team_name}'")

    db_team_id = upsert_team(sb, gc_team_id, team_name, args.sport)
    print(f"[supabase] Team row ID: {db_team_id}")

    team_slug   = get_team_slug(page, gc_team_id, args.timeout)
    stats_locked = is_stats_locked(page, gc_team_id, args.timeout)
    print(f"[stats] {'LOCKED — using box scores' if stats_locked else 'Admin access — using season-stats page'}")

    games = scrape_schedule(page, gc_team_id, args.timeout)

    # Filter to games on or after --since-date, if provided
    if args.since_date:
        orig_count = len(games)
        since_ym = args.since_date[:7]  # "YYYY-MM" for month-level fallback

        def _should_include(g):
            # Best case: precise ISO date from full_date_raw
            iso = _parse_game_date(g.get("full_date_raw", ""), g.get("date", ""))
            if iso:
                return iso >= args.since_date
            # Fallback: compare year-month only (include entire month if it overlaps)
            my_raw = g.get("date", "")
            m = re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})',
                         my_raw, re.I)
            if m:
                month_num = _MONTH_MAP.get(m.group(1).lower()[:3])
                year = int(m.group(2))
                if month_num:
                    return f"{year:04d}-{month_num:02d}" >= since_ym
            # Can't determine date — include to avoid missing data
            return True

        games = [g for g in games if _should_include(g)]
        print(f"[filter] --since-date {args.since_date}: {orig_count} → {len(games)} games")

    total_plays = 0
    skipped_games = 0
    all_box_scores = []

    # Pre-fetch event IDs already in DB for this team (one REST call vs. per-game browser loads)
    _existing_rows = sb.select("games", {"team_id": db_team_id}, columns="id,gc_event_id")
    _existing_games = {r["gc_event_id"]: r["id"] for r in _existing_rows}
    print(f"[db] {len(_existing_games)} game(s) already in DB for this team")

    for g in games:
        event_id     = g["gc_event_id"]
        scouted_home = g.get("home_away") == "home"
        print(f"\n[game] {event_id}")

        # Skip if already fully scraped — check for existing plays in DB
        if event_id in _existing_games:
            _play_check = sb.select(
                "plays",
                {"team_id": db_team_id, "game_id": _existing_games[event_id]},
                columns="id",
            )
            if _play_check:
                print(f"[game] {event_id} — already scraped ({len(_play_check)} plays), skipping")
                skipped_games += 1
                continue

        # Box score — batting/pitching stats + spray chart data
        bs = scrape_box_score(page, gc_team_id, team_slug, event_id, team_name, args.timeout)
        if bs:
            all_box_scores.append(bs)

        # Play-by-play — spray chart zones + starting pitcher + raw lines for inference
        plays, _, pitcher_info, play_lines = scrape_plays(
            page, gc_team_id, event_id, scouted_home, args.timeout
        )
        if pitcher_info.get("starting_pitcher"):
            g["starting_pitcher"] = pitcher_info["starting_pitcher"]

        # Use the precise date from the box score header if available
        if bs and bs.get("game_date_raw"):
            g["full_date_raw"] = bs["game_date_raw"]

        db_game_id = upsert_game(sb, db_team_id, g)

        if plays:
            insert_plays(sb, db_team_id, db_game_id, plays)
            total_plays += len(plays)
            print(f"[supabase] {len(plays)} plays")

        if bs:
            insert_game_batting_stats(sb, db_team_id, db_game_id, event_id, bs)
            insert_game_pitching_stats(sb, db_team_id, db_game_id, event_id, bs)

        # Catching + innings played:
        # Primary: infer from box score lineup positions + play-by-play subs/events.
        # Fallback: game-stats page (admin teams only) if inference yields nothing.
        catching, innings = [], []
        if bs and play_lines:
            scouted_players = bs.get('scouted', {}).get('batting', [])
            catching, innings = infer_fielding_from_lineup_and_plays(
                scouted_players, play_lines, scouted_home
            )

        if not catching and not stats_locked:
            # Fallback to game-stats page for admin teams
            gs_loaded = _load_game_stats_page(page, gc_team_id, event_id, args.timeout, team_slug)
            if gs_loaded:
                catching = scrape_game_catching(page, gc_team_id, event_id, True, args.timeout, team_slug)
                innings  = scrape_game_innings(page, gc_team_id, event_id, True, args.timeout, team_slug)

        if catching:
            insert_game_catching_stats(sb, db_team_id, db_game_id, event_id, catching)
        if innings:
            insert_game_innings_played(sb, db_team_id, db_game_id, event_id, innings)

        time.sleep(1)

    # Season stats
    if not stats_locked:
        batting  = scrape_batting_stats(page, gc_team_id, args.timeout)
        pitching = scrape_pitching_stats(page, args.timeout)
    else:
        batting, pitching = aggregate_stats_from_box_scores(all_box_scores, team_name)

    if batting:
        upsert_batting(sb, db_team_id, batting)
    if pitching:
        upsert_pitching(sb, db_team_id, pitching)

    new_games = len(games) - skipped_games
    return {"team": team_name, "gc_id": gc_team_id,
            "games": len(games), "new": new_games, "skipped": skipped_games,
            "plays": total_plays, "batting": len(batting), "pitching": len(pitching)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    sb = SupabaseClient(args.supabase_url, args.supabase_key)
    sb.ping()

    # Build the list of (gc_team_id, team_name) pairs to process
    team_list: list[tuple[str, str]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page    = context.new_page()

        # Single login for the entire run
        login(page, args.timeout, email=args.gc_email, password=args.gc_password)

        # Resolve team IDs
        if args.all_teams:
            # Pull every team from the database
            rows = sb.select("teams", {}, columns="gc_team_id,name")
            if not rows:
                print("ERROR: --all-teams specified but no teams found in the database.")
                browser.close()
                return
            team_list = [(r["gc_team_id"], r["name"]) for r in rows]
            print(f"[db] Loaded {len(team_list)} teams from Supabase")
        elif args.team_ids:
            # IDs provided directly — pair with names (or use ID as label if name missing)
            for i, tid in enumerate(args.team_ids):
                name = args.team_names[i] if i < len(args.team_names) else tid
                team_list.append((tid, name))
        elif args.team_names:
            # Search by name
            for name in args.team_names:
                tid = find_team(page, name, args.sport, args.timeout,
                                division=args.division, state=args.state,
                                season=args.season)
                if tid:
                    team_list.append((tid, name))
                else:
                    print(f"[search] Skipping '{name}' — not found")
        else:
            print("ERROR: Provide --all-teams, --team-id, or --team.")
            browser.close()
            return

        if not team_list:
            print("ERROR: No teams resolved. Exiting.")
            browser.close()
            return

        print(f"\n[run] {len(team_list)} team(s) queued: {[t[1] for t in team_list]}")

        # Scrape each team in turn within the same session
        summaries = []
        for gc_team_id, team_name in team_list:
            try:
                summary = scrape_one_team(sb, page, gc_team_id, team_name, args)
                summaries.append(summary)
            except Exception as e:
                print(f"[ERROR] {team_name} ({gc_team_id}): {e}")
                summaries.append({"team": team_name, "gc_id": gc_team_id, "error": str(e)})

        browser.close()

    # Final summary
    print("\n" + "=" * 60)
    print("ALL DONE")
    print("=" * 60)
    for s in summaries:
        if "error" in s:
            print(f"  ✗ {s['team']}  → ERROR: {s['error']}")
        else:
            skip_note = f"  (skipped {s['skipped']} already scraped)" if s.get('skipped') else ""
            print(f"  ✓ {s['team']}  games={s['games']}  new={s['new']}  new_plays={s['plays']}"
                  f"  batters={s['batting']}  pitchers={s['pitching']}{skip_note}")
    print("=" * 60)


if __name__ == "__main__":
    args = parse_args()
    run(args)
