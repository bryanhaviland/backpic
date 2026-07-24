#!/usr/bin/env python3
"""
gc_boxscore_patch.py

Patches game_batting_stats and game_pitching_stats for teams that already have
games in Supabase but are missing box score data.

KEY DIFFERENCE vs gc_scraper.py: stores player names DIRECTLY from the GC box
score JSON — no roster name-matching step. This fixes the issue where matching
failures resulted in 0 batters / 0 pitchers for many teams.

Usage:
  python3 gc_boxscore_patch.py --headless \\
    --team-id mDCClxcC1LQs --team "LadyHawks Garcia" \\
    --team-id 62gwwZMxWShR --team "CF Flamingos" \\
    2>&1 | tee ~/backpic-scouting-v2/boxscore_patch.log

Required env vars (same as gc_scraper.py):
  SUPABASE_URL   https://fzndpqbjmhwwouxktmol.supabase.co
  SUPABASE_KEY   your service-role or anon key

Optional flags:
  --limit N      max games to patch per team (default: 0 = all)
  --dry-run      scrape but do NOT write to DB
  --force        re-patch games that already have stats (re-scrape + insert)
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print('ERROR: playwright not installed. Run: pip3 install playwright && playwright install chromium')
    sys.exit(1)

try:
    from supabase import create_client
except ImportError:
    print("ERROR: supabase-py not installed. Run: pip3 install supabase")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GC_BASE      = "https://web.gc.com"
PAGE_TIMEOUT = 30       # seconds for Selenium page load
REACT_WAIT   = 8        # seconds to let React/fetch settle after navigation
INTER_GAME   = 1.5      # polite delay between games
RETRY_LIMIT  = 2        # retry attempts per game

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Fuzzy name matching ───────────────────────────────────────────────────────

TEAM_NOISE = re.compile(
    r'\b(10u|12u|14u|16u|18u|softball|fastpitch|baseball|'
    r'gold|silver|blue|red|white|black|navy|'
    r'the|and|of|at)\b',
    re.IGNORECASE,
)

def normalize(name: str) -> str:
    """Lowercase, strip punctuation/noise words, collapse spaces."""
    n = name.lower()
    n = re.sub(r"[^\w\s]", " ", n)   # punctuation → space
    n = TEAM_NOISE.sub(" ", n)        # remove noise words
    n = re.sub(r"\s+", " ", n).strip()
    return n

def fuzzy_score(a: str, b: str) -> float:
    """
    Return 0.0–1.0 similarity between two team/player names.
    Uses SequenceMatcher on normalized strings; also gives a bonus when
    one string is a prefix or substring of the other (handles '10U' suffix gap).
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    base = difflib.SequenceMatcher(None, na, nb).ratio()
    # substring bonus: "st pete scorchers wilson" ⊂ "st pete scorchers wilson 10u"
    bonus = 0.1 if (na in nb or nb in na) else 0.0
    return min(1.0, base + bonus)

def best_match(query: str, candidates: list, threshold: float = 0.70) -> tuple:
    """
    Return (best_candidate, score) from candidates, or (None, 0) if below threshold.
    candidates is a list of strings.
    """
    best, best_s = None, 0.0
    for c in candidates:
        s = fuzzy_score(query, c)
        if s > best_s:
            best, best_s = c, s
    if best_s >= threshold:
        return best, best_s
    return None, best_s

# ── Text-based box score parser ──────────────────────────────────────────────
# GC renders batting/pitching as plain text lines — not HTML tables.
# Structure after clicking Box Score tab:
#   [Team Name]
#   LINEUP
#   AB\nR\nH\nRBI\nBB\nSO          ← column headers, one per line
#   [Player Name]
#   #N (pos, pos)                   ← position line starts with #
#   1\n1\n0\n1\n2\n0                ← stat values, one per line
#   ...
#   TEAM\n[totals]
#   (notes like "2B: ...", "TB: ...")
#   [Opponent Team Name]
#   LINEUP ...                      ← repeat for opponent
#   PITCHERS                        ← pitching section
#   IP\nH\nR\nER\nBB\nSO
#   [Pitcher Name]\n#N (P)\nstats...

BAT_COLS = ['AB', 'R', 'H', 'RBI', 'BB', 'SO']
PIT_COLS = ['IP', 'H', 'R', 'ER', 'BB', 'SO']

def _is_num(s: str) -> bool:
    try: float(s); return True
    except: return False

def _cols_match(lines: list, idx: int, cols: list) -> bool:
    if idx + len(cols) > len(lines):
        return False
    return all(lines[idx + k] == cols[k] for k in range(len(cols)))

def _count_extras(section_lines: list, key: str) -> int:
    """Count total occurrences of an extras line like 'SB: Name1, Name2 2'."""
    for l in section_lines:
        if l.startswith(f'{key}:'):
            total = 0
            for part in l[len(key)+1:].split(','):
                part = part.strip()
                m = re.search(r'\b(\d+)$', part)
                total += int(m.group(1)) if m else (1 if part else 0)
            return total
    return 0


def parse_boxscore_text(body_text: str, scouted_name: str) -> tuple:
    """
    Parse GC box score from page innerText.
    Returns (batters, pitchers) lists compatible with parse_batter/parse_pitcher.
    """
    raw = [l.strip() for l in body_text.replace('\xa0', ' ').replace('\xad', '').split('\n')]
    lines = [l for l in raw if l]
    n = len(lines)
    batters, pitchers, catchers = [], [], []

    # ── locate all LINEUP and PITCHERS sections ───────────────────────────────
    bat_sections  = []   # (team_hint, data_start_idx)
    pit_sections  = []   # (team_hint, data_start_idx)
    bat_kw_lines  = []   # keyword line index for each batting section (parallel)
    pit_kw_lines  = []   # keyword line index for each pitching section (parallel)

    for i, line in enumerate(lines):
        if line == 'LINEUP' and _cols_match(lines, i + 1, BAT_COLS):
            team_hint = lines[i - 1] if i > 0 else ''
            bat_sections.append((team_hint, i + 1 + len(BAT_COLS)))
            bat_kw_lines.append(i)
        if line in ('PITCHING', 'PITCHERS', 'PITCHER') and _cols_match(lines, i + 1, PIT_COLS):
            pit_sections.append(('', i + 1 + len(PIT_COLS)))
            pit_kw_lines.append(i)

    # ── pick the section belonging to the scouted team ────────────────────────
    _MIN_SECTION_SCORE = 0.40

    def best_section(sections):
        """Pick batting section by fuzzy team-name match (unchanged logic)."""
        if not sections:
            return None
        scored = [(fuzzy_score(scouted_name, th), th, start) for th, start in sections]
        scored.sort(reverse=True)
        log(f"    Section scores: {[(round(s,2), t[:30]) for s,t,_ in scored]}")
        best_score, best_th, best_start = scored[0]
        if best_score < _MIN_SECTION_SCORE:
            log(f"    ✗ No section matched '{scouted_name}' (best={best_score:.2f} '{best_th[:30]}') — skipping")
            return None
        return (best_th, best_start)

    def best_pit_section_positional(bat_sec_result):
        """
        Pick the pitching section positionally: find the first PITCHING keyword
        that appears after the scouted batting section's LINEUP keyword.
        Falls back to the first pitching section if no batting anchor.
        """
        if not pit_sections:
            log(f"    ✗ No PITCHING section found on page")
            return None
        if bat_sec_result is None or not bat_kw_lines:
            _, start = pit_sections[0]
            log(f"    Pitching: using first section (no batting anchor)")
            return start
        # Find the batting section index that matched the scouted team
        bat_data_start = bat_sec_result[1]
        bat_idx = next((j for j, (_, s) in enumerate(bat_sections) if s == bat_data_start), 0)
        bat_kw  = bat_kw_lines[bat_idx]
        # First pitching section after that batting keyword
        for j, pit_kw in enumerate(pit_kw_lines):
            if pit_kw > bat_kw:
                _, start = pit_sections[j]
                log(f"    Pitching: positional match at line {pit_kw} (after batting LINEUP at {bat_kw})")
                return start
        # Fallback: last pitching section on page
        _, start = pit_sections[-1]
        log(f"    Pitching: fallback to last section (line {pit_kw_lines[-1]})")
        return start

    _POS_RE = re.compile(r'^#(\d+)(?:\s*\(([^)]*)\))?$')

    def parse_players(start_idx, cols, is_pitching=False):
        players = []
        j = start_idx
        STOP = {'TEAM', 'LINEUP', 'PITCHING', 'PITCHERS', 'PITCHER', 'RECAP', 'PLAYS', 'VIDEOS', 'INFO'}
        while j < n:
            line = lines[j]
            if line in STOP or line.startswith('2B:') or line.startswith('HR:') or \
               line.startswith('TB:') or line.startswith('SB:') or line.startswith('HBP:'):
                break
            # Player name: next line starts with # (number + optional positions)
            if j + 1 < n and lines[j + 1].startswith('#'):
                player_name = line
                pos_line    = lines[j + 1]
                pm          = _POS_RE.match(pos_line)
                player_num  = pm.group(1) if pm else ''
                positions   = [p.strip() for p in pm.group(2).split(',')] if (pm and pm.group(2)) else []
                j += 2
                stats = {}
                for col in cols:
                    if j < n and _is_num(lines[j]):
                        stats[col] = lines[j]
                        j += 1
                    else:
                        break
                if stats:
                    players.append({
                        'player_name': player_name,
                        'player_num':  player_num,
                        'positions':   positions,
                        **stats,
                    })
            else:
                j += 1
        return players

    # ── batting ───────────────────────────────────────────────────────────────
    # Collect raw lines per section for extras parsing (SB, CS, WP, PB)
    def _section_lines(sections, th, start):
        """Lines from start until the next section of the same type."""
        starts = [s for (_, s) in sections if s > start]
        end_i  = min(starts, default=n)
        return lines[start:end_i]

    bat_sec = best_section(bat_sections)
    if bat_sec:
        team_hint, start = bat_sec
        log(f"    Text: using batting section for '{team_hint[:40]}'")
        raw_bat = parse_players(start, BAT_COLS)
        for p in raw_bat:
            batters.append({
                'player_name': p['player_name'],
                'player_num':  p.get('player_num', ''),
                'positions':   p.get('positions', []),
                'ab':  to_int(p.get('AB')),
                'r':   to_int(p.get('R')),
                'h':   to_int(p.get('H')),
                'rbi': to_int(p.get('RBI')),
                'bb':  to_int(p.get('BB')),
                'so':  to_int(p.get('SO')),
            })

    # ── catchers ──────────────────────────────────────────────────────────────
    # Identify catchers: any player whose positions include 'C'
    for b in batters:
        if 'C' in b.get('positions', []):
            catchers.append({
                'player_name': b['player_name'],
                'player_num':  b.get('player_num', ''),
                'positions':   b.get('positions', []),
                'inn': 0, 'pb': 0, 'sb': 0, 'sb_att': 0,
                'cs': 0, 'cs_pct': None, 'pik': 0, 'ci': 0,
            })

    if catchers:
        log(f"    Catchers from lineup: {[c['player_name'] for c in catchers]}")
        # For single-catcher games: derive SB/CS/PB from box score extras directly.
        # Opponent's batting extras tell us SB and CS against our catcher.
        # Our pitching extras tell us WP and PB.
        if len(catchers) == 1 and bat_sec:
            opp_sections  = [(th, s) for (th, s) in bat_sections if th != bat_sec[0]]
            opp_sb = opp_cs = 0
            for (th, s) in opp_sections:
                sl = _section_lines(bat_sections, th, s)
                opp_sb += _count_extras(sl, 'SB')
                opp_cs += _count_extras(sl, 'CS')
            our_pb = 0
            _pit_extras_start = best_pit_section_positional(bat_sec)
            if _pit_extras_start is not None:
                pl = lines[_pit_extras_start: next(
                    (s for (_, s) in pit_sections if s > _pit_extras_start), n)]
                our_pb = _count_extras(pl, 'PB')
            c = catchers[0]
            c['sb']     = opp_sb
            c['cs']     = opp_cs
            c['sb_att'] = opp_sb + opp_cs
            c['pb']     = our_pb
            c['cs_pct'] = round(opp_cs / (opp_sb + opp_cs) * 100, 2) if (opp_sb + opp_cs) > 0 else None
            log(f"    Single catcher extras: SB={opp_sb} CS={opp_cs} PB={our_pb}")

    # ── pitching ──────────────────────────────────────────────────────────────
    if not pit_sections:
        # Help diagnose: show lines around any PITCHER-like keyword
        for i, line in enumerate(lines):
            if 'PITCH' in line.upper():
                log(f"    Pitching hint at [{i}]: {lines[max(0,i-1):i+8]}")
    pit_start = best_pit_section_positional(bat_sec)
    if pit_start is not None:
        raw_pit = parse_players(pit_start, PIT_COLS, is_pitching=True)
        for p in raw_pit:
            pitchers.append({
                'player_name': p['player_name'],
                'player_num':  p.get('player_num', ''),
                'ip':  to_float(p.get('IP')),
                'h':   to_int(p.get('H')),
                'r':   to_int(p.get('R')),
                'er':  to_int(p.get('ER')),
                'bb':  to_int(p.get('BB')),
                'so':  to_int(p.get('SO')),
            })

    return batters, pitchers, catchers

# ── Numeric coercion ──────────────────────────────────────────────────────────

def to_int(val, default: int = 0) -> int:
    if val is None or val == "" or val in ("-", "--", "N/A"):
        return default
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return default

def to_float(val, default: float = 0.0) -> float:
    if val is None or val == "" or val in ("-", "--", "N/A"):
        return default
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default

# ── Play-by-play fetch ───────────────────────────────────────────────────────

def fetch_play_lines(page, gc_team_id: str, gc_event_id: str) -> list:
    """
    Navigate to the /plays page and return raw text lines.
    Returns [] on timeout or if the page has no inning headers.
    """
    url = f"{GC_BASE}/teams/{gc_team_id}/schedule/{gc_event_id}/plays"
    log(f"    [plays] → {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
    except PWTimeout:
        log(f"    [plays] Timeout — skipping")
        return []
    except Exception as e:
        log(f"    [plays] Error: {e}")
        return []
    time.sleep(4)
    try:
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return []
    raw = [l.strip() for l in body.replace('\xa0', ' ').split('\n')]
    lines = [l for l in raw if l]
    # Sanity check: must have at least one inning header
    if not any(re.match(r'(top|bottom)\s+\d+', l, re.I) for l in lines):
        log(f"    [plays] No inning headers found ({len(lines)} lines) — skipping")
        return []
    log(f"    [plays] {len(lines)} lines")
    return lines


# ── Catching inference from play-by-play ─────────────────────────────────────

# Matches GC substitution lines:
#   "Lineup changed: Laila J in at catcher"
#   "Mia S now pitching"
#   "Laila J moves to shortstop"
_SUB_RE = re.compile(
    r'(?:lineup\s+changed?[:\s]+)?'
    r'([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\s+'
    r'(?:in\s+at|now\s+(?:at|playing|pitching)|moves?\s+to|entered?\s+(?:as|at))\s+'
    r'(pitcher|catcher|first\s+base(?:man)?|second\s+base(?:man)?|'
    r'third\s+base(?:man)?|shortstop|short\s+field(?:er)?|'
    r'left\s+field(?:er)?|center\s+field(?:er)?|right\s+field(?:er)?)',
    re.IGNORECASE,
)

def _match_player_name(abbreviated: str, player_list: list) -> str | None:
    """
    Match a GC-abbreviated name to a full name in player_list.
    Handles common GC formats:
      'Emma Johnson'  → exact / prefix match
      'Emma J'        → first name full, last initial
      'E Johnson'     → first initial, last name full
      'E J'           → both initials (last resort)
    """
    ab = abbreviated.strip().lower()
    ab_parts = ab.split()
    if not ab_parts or len(ab) < 2:
        return None

    # Pass 1: direct prefix (covers exact name and 'Emma J' with ≥3 chars)
    if len(ab) >= 3:
        for name in player_list:
            if name.lower().startswith(ab):
                return name

    if len(ab_parts) >= 2:
        first_ab = ab_parts[0]
        last_ab  = ab_parts[-1]

        for name in player_list:
            nl     = name.lower()
            np     = nl.split()
            if len(np) < 2:
                continue
            first_full = np[0]
            last_full  = np[-1]

            # 'E Johnson' — first is a single initial
            if (len(first_ab) == 1
                    and first_full[0] == first_ab
                    and last_full.startswith(last_ab[:4])):
                return name

            # 'Emma J' — last is a single initial
            if (len(last_ab) == 1
                    and last_full[0] == last_ab
                    and first_full.startswith(first_ab[:4])):
                return name

            # 'Emma Joh' — both abbreviated but long enough
            if (first_full.startswith(first_ab[:4])
                    and last_full.startswith(last_ab[:4])
                    and (len(first_ab) >= 3 or len(last_ab) >= 3)):
                return name

    return None


def _outs_on_play(line: str) -> int:
    """Return the number of outs recorded on a single play-by-play line."""
    tl = line.lower()
    if re.search(r'triple\s+play', tl):
        return 3
    if re.search(r'double\s+play', tl):
        return 2
    if re.search(
        r'strikes?\s+out|strikeout|'
        r'fl(?:ies|y|ied|ew)\s+out|'
        r'ground(?:s|ed)?\s+out|'
        r'line(?:s|d)?\s+out|'
        r'pop(?:s|ped)?\s+out|'
        r'out\s+at\s+(?:first|second|third|home|the\s+plate)|'
        r'tagged\s+out|'
        r'caught\s+stealing|'
        r'picked\s+off|'
        r'\bretired\b|'
        r'\bput\s+out\b',
        tl,
    ):
        return 1
    return 0


def infer_catching_from_plays(catchers: list, all_batters: list,
                               play_lines: list, scouted_home: bool) -> list:
    """
    Use play-by-play lines to fill in catching stats (inn, sb, cs, pb, sb_att, cs_pct)
    for each catcher identified from the box score lineup.

    catchers    : list of dicts with player_name, player_num, positions
    all_batters : full lineup (used to build initial pos→player map)
    play_lines  : raw text lines from /plays page
    scouted_home: True if our team batted in the bottom half

    Innings are split in thirds based on outs recorded.  Within each
    defensive half-inning each catcher accumulates outs while they are
    active; at the close of the half the innings credited are
    (catcher_outs / 3.0) for each catcher.  If no outs were detected
    for a half-inning (regex miss), the whole inning is credited to
    whoever was catching when the half ended.

    Returns updated catchers list.
    """
    if not catchers or not play_lines:
        return catchers

    def_half = 'top' if scouted_home else 'bottom'

    # Build initial position → player map from lineup order
    # (positions[0] = starting position)
    pos_to_player: dict = {}
    player_to_pos: dict = {}
    player_list:   list = []
    for p in all_batters:
        name = p.get('player_name', '')
        positions = [x.strip().upper() for x in p.get('positions', []) if x.strip()]
        if name and positions:
            player_list.append(name)
            starting = positions[0]
            player_to_pos[name] = starting
            pos_to_player[starting] = name

    def _blank_catcher(name: str, num: str = '') -> dict:
        return {'player_name': name, 'player_num': num,
                'inn': 0.0, 'pb': 0, 'sb': 0,
                'sb_att': 0, 'cs': 0, 'cs_pct': None,
                'pik': 0, 'ci': 0}

    # Catching accumulators — pre-populate from identified catchers
    acc: dict = {}
    for c in catchers:
        entry = _blank_catcher(c['player_name'], c.get('player_num', ''))
        # Carry over any stats already parsed from box score extras
        for k in ('sb', 'cs', 'sb_att', 'pb', 'pik', 'ci'):
            if k in c:
                entry[k] = c[k]
        acc[c['player_name']] = entry

    inning_re  = re.compile(r'(top|bottom)\s+(?:of\s+)?(\d+)(?:st|nd|rd|th)?', re.I)
    in_def_half = False
    # outs_this_half[catcher_name] = outs recorded while that catcher was active
    outs_this_half: dict = {}

    def _cur_catcher() -> str | None:
        return pos_to_player.get('C')

    def _ensure_in_acc(name: str, num: str = '') -> None:
        if name not in acc:
            acc[name] = _blank_catcher(name, num)
            log(f"    [plays] Dynamically added catcher to acc: {name}")

    def _close_def_half() -> None:
        """
        Credit innings at the close of a defensive half.
        Uses outs recorded per catcher (out_count / 3.0).
        Falls back to 1.0 for the current catcher if no outs were detected.
        """
        total_outs = sum(outs_this_half.values())
        if total_outs > 0:
            for name, outs in outs_this_half.items():
                _ensure_in_acc(name)
                acc[name]['inn'] = round(acc[name]['inn'] + outs / 3.0, 4)
            log(f"    [plays] Inning close (outs): { {n: o for n, o in outs_this_half.items()} }")
        else:
            # No outs detected — credit full inning to current catcher
            cur = _cur_catcher()
            if cur:
                _ensure_in_acc(cur)
                acc[cur]['inn'] += 1.0
                log(f"    [plays] Inning close (no outs detected) → {cur} +1.0")
        outs_this_half.clear()

    for line in play_lines:
        # ── Inning header ────────────────────────────────────────────────────
        im = inning_re.match(line)
        if im:
            new_half = im.group(1).lower()
            if in_def_half:
                _close_def_half()
            in_def_half = (new_half == def_half)
            continue

        # ── Substitution ─────────────────────────────────────────────────────
        sm = _SUB_RE.search(line)
        if sm:
            entering_raw = sm.group(1).strip()
            pos_word     = sm.group(2).strip().lower()
            new_pos = ('C' if 'catch' in pos_word else
                       'P' if 'pitch' in pos_word else
                       None)
            if new_pos:
                matched = _match_player_name(entering_raw, player_list)
                if not matched:
                    matched = _match_player_name(
                        entering_raw, [c['player_name'] for c in catchers])
                if matched:
                    old_pos = player_to_pos.get(matched)
                    player_to_pos[matched] = new_pos
                    pos_to_player[new_pos] = matched
                    if (old_pos and old_pos != new_pos
                            and pos_to_player.get(old_pos) == matched):
                        del pos_to_player[old_pos]
                    if matched not in player_list:
                        player_list.append(matched)
                    log(f"    [plays] Sub: {matched} → {new_pos}  ({line[:60]})")
                else:
                    log(f"    [plays] Sub unmatched: '{entering_raw}' → {new_pos}  ({line[:60]})")
            continue

        # ── Defensive half: track outs and catching events ────────────────────
        if in_def_half:
            cur = _cur_catcher()
            if cur:
                _ensure_in_acc(cur)
                tl = line.lower()

                # Outs
                n_outs = _outs_on_play(line)
                if n_outs:
                    outs_this_half[cur] = outs_this_half.get(cur, 0) + n_outs

                # SB / CS / PB (CS also counted as an out above — that's correct)
                if 'stolen base' in tl or 'steals' in tl:
                    acc[cur]['sb']     += 1
                    acc[cur]['sb_att'] += 1
                elif 'caught stealing' in tl or 'picked off' in tl:
                    acc[cur]['cs']     += 1
                    acc[cur]['sb_att'] += 1
                elif 'passed ball' in tl:
                    acc[cur]['pb'] += 1

    # Close the final defensive half-inning
    if in_def_half:
        _close_def_half()

    # Round innings to 2 decimal places and finalise cs_pct
    for name, stats in acc.items():
        stats['inn'] = round(stats['inn'], 2)
        att = stats['sb_att']
        stats['cs_pct'] = round(stats['cs'] / att * 100, 2) if att > 0 else None

    result = list(acc.values())
    log(f"    [plays] Catching inferred: "
        f"{ {r['player_name']: {'inn': r['inn'], 'sb': r['sb'], 'cs': r['cs'], 'pb': r['pb']} for r in result} }")
    return result


# ── Playwright browser ───────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(__file__), ".gc_state.json")
_pw        = None   # playwright instance (kept open for the session)

def start_playwright():
    global _pw
    _pw = sync_playwright().start()
    return _pw

def stop_playwright():
    global _pw
    if _pw:
        try: _pw.stop()
        except Exception: pass
        _pw = None

def new_page(headless: bool):
    """
    Launch a Chromium browser and return (browser, context, page).
    If STATE_FILE exists the context is pre-authenticated.
    """
    browser = _pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    if os.path.exists(STATE_FILE):
        ctx = browser.new_context(
            storage_state=STATE_FILE,
            viewport={"width": 1280, "height": 900},
        )
        log(f"Loaded browser state from {STATE_FILE}")
    else:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(PAGE_TIMEOUT * 1000)
    return browser, ctx, page

def save_state(ctx):
    """Persist cookies + localStorage so headless runs stay authenticated."""
    try:
        ctx.storage_state(path=STATE_FILE)
        log(f"Browser state saved to {STATE_FILE}")
    except Exception as e:
        log(f"State save error: {e}")

def ensure_logged_in(page, ctx):
    """
    Open GC login page (visible browser), wait for user to log in, save state.
    """
    page.goto("https://web.gc.com/login")
    time.sleep(1)

    print("\n" + "="*60, flush=True)
    print("  A browser window has opened to GameChanger.", flush=True)
    print("  Please log in, then come back to this terminal", flush=True)
    print("  and press ENTER to continue.", flush=True)
    print("="*60, flush=True)
    sys.stdout.flush()

    try:
        input("\n  -> Press ENTER after logging in: ")
    except EOFError:
        print("  (stdin not interactive -- waiting 90 seconds for login)", flush=True)
        time.sleep(90)

    time.sleep(2)
    save_state(ctx)
    return True

# ── JavaScript: extract box score from React fiber / __NEXT_DATA__ ────────────

JS_EXTRACT = """
(function() {
  // ── helpers ──────────────────────────────────────────────────────────────
  function isObj(v) { return v !== null && typeof v === 'object'; }

  function isBattingRow(v) {
    if (!isObj(v)) return false;
    return 'ab' in v || 'atBats' in v || 'at_bats' in v ||
           (isObj(v.stats) && ('ab' in v.stats || 'atBats' in v.stats));
  }

  function isPitchingRow(v) {
    if (!isObj(v)) return false;
    return 'ip' in v || 'inningsPitched' in v || 'innings_pitched' in v ||
           (isObj(v.stats) && ('ip' in v.stats || 'inningsPitched' in v.stats));
  }

  function isBattingArr(v)  { return Array.isArray(v) && v.length > 0 && isBattingRow(v[0]); }
  function isPitchingArr(v) { return Array.isArray(v) && v.length > 0 && isPitchingRow(v[0]); }

  function isBoxScore(v) {
    if (!isObj(v)) return false;
    const lk = Object.keys(v).map(k => k.toLowerCase());
    return (lk.includes('batting') || lk.includes('batters') || lk.includes('battingstats')) &&
           (lk.includes('pitching') || lk.includes('pitchers') || lk.includes('pitchingstats'));
  }

  const BATTER_KEYS  = ['batters','batting','battingStats','batting_stats','hitters'];
  const PITCHER_KEYS = ['pitchers','pitching','pitchingStats','pitching_stats'];
  const SIDES        = ['home','away','us','them','team','opponent'];

  function checkObj(v, out) {
    if (!isObj(v)) return;
    if (isBattingRow(v))  { out.batters  = out.batters  || [v]; return; }
    if (isPitchingRow(v)) { out.pitchers = out.pitchers || [v]; return; }
    if (isBattingArr(v))  { out.batters  = out.batters  || v;  return; }
    if (isPitchingArr(v)) { out.pitchers = out.pitchers || v;  return; }
    if (isBoxScore(v)) {
      for (const k of BATTER_KEYS)  if (v[k]) { out.batters  = out.batters  || v[k]; break; }
      for (const k of PITCHER_KEYS) if (v[k]) { out.pitchers = out.pitchers || v[k]; break; }
      // also check per-side
      for (const side of SIDES) {
        if (isObj(v[side])) {
          for (const k of BATTER_KEYS)  if (v[side][k]) { out.batters  = out.batters  || v[side][k]; break; }
          for (const k of PITCHER_KEYS) if (v[side][k]) { out.pitchers = out.pitchers || v[side][k]; break; }
        }
      }
    }
    // Direct key check regardless of isBoxScore
    for (const k of BATTER_KEYS)  if (isBattingArr(v[k]))  { out.batters  = out.batters  || v[k]; }
    for (const k of PITCHER_KEYS) if (isPitchingArr(v[k])) { out.pitchers = out.pitchers || v[k]; }
    for (const side of SIDES) {
      if (!isObj(v[side])) continue;
      for (const k of BATTER_KEYS)  if (isBattingArr(v[side][k]))  { out.batters  = out.batters  || v[side][k]; }
      for (const k of PITCHER_KEYS) if (isPitchingArr(v[side][k])) { out.pitchers = out.pitchers || v[side][k]; }
    }
  }

  // ── walk React fiber ──────────────────────────────────────────────────────
  function walkFiber(node, depth, out) {
    if (!node || depth <= 0 || (out.batters && out.pitchers)) return;

    // memoizedState chain
    let s = node.memoizedState, sd = 0;
    while (s && sd < 25) {
      if (isObj(s.memoizedState)) checkObj(s.memoizedState, out);
      // also check queue last rendered state
      try {
        if (isObj(s.queue) && isObj(s.queue.lastRenderedState))
          checkObj(s.queue.lastRenderedState, out);
      } catch(e) {}
      s = s.next; sd++;
    }

    // memoizedProps
    if (isObj(node.memoizedProps)) checkObj(node.memoizedProps, out);

    // pending props
    if (isObj(node.pendingProps)) checkObj(node.pendingProps, out);

    walkFiber(node.child,   depth - 1, out);
    walkFiber(node.sibling, depth - 1, out);
  }

  // ── deep search plain object (for __NEXT_DATA__) ──────────────────────────
  function deepSearch(obj, depth, visited, out) {
    if (!isObj(obj) || depth <= 0) return;
    if (visited.has(obj)) return;
    visited.add(obj);
    checkObj(obj, out);
    if (out.batters && out.pitchers) return;
    for (const k of Object.keys(obj)) {
      try { deepSearch(obj[k], depth - 1, visited, out); } catch(e) {}
      if (out.batters && out.pitchers) return;
    }
  }

  // ── entry point ───────────────────────────────────────────────────────────
  // ── also capture per-side (home/away) data with team names ──────────────
  function captureSide(v, sideOut) {
    for (const k of BATTER_KEYS)  if (isBattingArr(v[k]))  { sideOut.batters  = sideOut.batters  || v[k]; }
    for (const k of PITCHER_KEYS) if (isPitchingArr(v[k])) { sideOut.pitchers = sideOut.pitchers || v[k]; }
    // team name
    const nameKeys = ['teamName','team_name','name','displayName','title'];
    for (const k of nameKeys) if (typeof v[k] === 'string' && v[k].length > 1) {
      sideOut.name = sideOut.name || v[k]; break;
    }
  }

  const out = { batters: null, pitchers: null, source: null,
                home: { name: '', batters: null, pitchers: null },
                away: { name: '', batters: null, pitchers: null } };

  // 1. Try __NEXT_DATA__ (SSR/initial props)
  const nd = document.getElementById('__NEXT_DATA__');
  if (nd) {
    try {
      const data = JSON.parse(nd.textContent);
      deepSearch(data, 10, new Set(), out);
      if (out.batters || out.pitchers) out.source = '__NEXT_DATA__';
    } catch(e) {}
  }

  // 2. Walk React fiber tree
  if (!out.batters && !out.pitchers) {
    const root = document.getElementById('__next') || document.body;
    const fk = Object.keys(root).find(k =>
      k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
    );
    if (fk) {
      walkFiber(root[fk], 60, out);
      if (out.batters || out.pitchers) out.source = 'fiber';
    }
  }

  // 3. Try to capture home/away split from box score objects in the fiber
  try {
    const root2 = document.getElementById('__next') || document.body;
    const fk2 = Object.keys(root2).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
    if (fk2) {
      function grabSides(node, depth) {
        if (!node || depth <= 0) return;
        const candidates = [
          node.memoizedState?.memoizedState,
          node.memoizedProps,
          node.pendingProps
        ].filter(isObj);
        for (const v of candidates) {
          for (const side of ['home','away']) {
            if (isObj(v[side])) captureSide(v[side], out[side]);
          }
          if (isObj(v.boxScore)) {
            for (const side of ['home','away']) {
              if (isObj(v.boxScore[side])) captureSide(v.boxScore[side], out[side]);
            }
          }
        }
        grabSides(node.child,   depth - 1);
        grabSides(node.sibling, depth - 1);
      }
      grabSides(root2[fk2], 60);
    }
  } catch(e) {}

  return {
    batters:  out.batters  || [],
    pitchers: out.pitchers || [],
    home:     { name: out.home.name || '', batters: out.home.batters || [], pitchers: out.home.pitchers || [] },
    away:     { name: out.away.name || '', batters: out.away.batters || [], pitchers: out.away.pitchers || [] },
    source:   out.source,
  };
})();
"""

# ── JavaScript: DOM table fallback ────────────────────────────────────────────

JS_DOM_TABLES = """
(function() {
  const result = { batters: [], pitchers: [], debug: [] };
  const tables = Array.from(document.querySelectorAll('table'));

  for (const tbl of tables) {
    const ths = Array.from(tbl.querySelectorAll('thead th, thead td'))
                     .map(h => h.textContent.trim().toUpperCase());
    if (ths.length === 0) {
      // Try first row as header
      const firstRow = tbl.querySelector('tr');
      if (firstRow) {
        ths.push(...Array.from(firstRow.querySelectorAll('td,th'))
                          .map(c => c.textContent.trim().toUpperCase()));
      }
    }

    const hasAB = ths.some(h => h === 'AB' || h === 'AT BATS');
    const hasIP = ths.some(h => h === 'IP' || h === 'INNINGS');
    if (!hasAB && !hasIP) continue;

    result.debug.push({ headers: ths, hasAB, hasIP });

    const bodyRows = Array.from(tbl.querySelectorAll('tbody tr'));
    const rows = bodyRows.length ? bodyRows :
      Array.from(tbl.querySelectorAll('tr')).slice(1); // skip header row

    const parsed = [];
    for (const tr of rows) {
      const cells = Array.from(tr.querySelectorAll('td')).map(c => c.textContent.trim());
      if (cells.length < 3) continue;
      const entry = { _cells: cells };
      ths.forEach((h, i) => { if (cells[i] !== undefined) entry[h] = cells[i]; });
      // Try to find a dedicated player name element
      const nameEl = tr.querySelector('[class*="player" i],[class*="name" i],[data-testid*="player" i]');
      if (nameEl) entry['_name_override'] = nameEl.textContent.trim();
      parsed.push(entry);
    }

    (hasIP ? result.pitchers : result.batters).push(...parsed);
  }
  return result;
})();
"""

# ── Parse fiber data into DB row dicts ────────────────────────────────────────

def _get(entry: dict, *keys, default=None):
    """Try multiple field-name aliases on a dict, also checking nested .stats."""
    for k in keys:
        if k in entry:
            return entry[k]
    stats = entry.get("stats")
    if isinstance(stats, dict):
        for k in keys:
            if k in stats:
                return stats[k]
    return default

def parse_batter(entry: dict) -> dict:
    name = _get(entry, "playerName","player_name","name","fullName","full_name","displayName",default="")
    num  = _get(entry, "uniformNumber","number","jersey","player_num","playerNumber","num",default="")
    pos  = _get(entry, "positions","position","pos",default="")
    if isinstance(pos, list):
        pos = ",".join(str(p) for p in pos)
    return {
        "player_name": str(name).strip(),
        "player_num":  str(num).strip(),
        "positions":   str(pos).strip(),
        "ab":      to_int(_get(entry,   "ab","atBats","at_bats")),
        "r":       to_int(_get(entry,   "r","runs")),
        "h":       to_int(_get(entry,   "h","hits")),
        "rbi":     to_int(_get(entry,   "rbi","runsBattedIn","runs_batted_in")),
        "bb":      to_int(_get(entry,   "bb","walks","baseOnBalls","base_on_balls")),
        "so":      to_int(_get(entry,   "so","k","strikeouts","strikeOuts","strike_outs")),
        "doubles": to_int(_get(entry,   "doubles","2b","twoBasers","two_basers")),
        "triples": to_int(_get(entry,   "triples","3b","threeBasers","three_basers")),
        "hr":      to_int(_get(entry,   "hr","homeRuns","home_runs","homeruns")),
        "hbp":     to_int(_get(entry,   "hbp","hitByPitch","hit_by_pitch")),
        "sb":      to_int(_get(entry,   "sb","stolenBases","stolen_bases")),
        "cs":      to_int(_get(entry,   "cs","caughtStealing","caught_stealing")),
    }

def parse_pitcher(entry: dict) -> dict:
    name = _get(entry, "playerName","player_name","name","fullName","full_name","displayName",default="")
    num  = _get(entry, "uniformNumber","number","jersey","player_num","playerNumber","num",default="")
    return {
        "player_name": str(name).strip(),
        "player_num":  str(num).strip(),
        "ip":          to_float(_get(entry, "ip","inningsPitched","innings_pitched")),
        "h":           to_int(  _get(entry, "h","hits","hitsAllowed","hits_allowed")),
        "r":           to_int(  _get(entry, "r","runs","runsAllowed","runs_allowed")),
        "er":          to_int(  _get(entry, "er","earnedRuns","earned_runs")),
        "bb":          to_int(  _get(entry, "bb","walks","baseOnBalls")),
        "so":          to_int(  _get(entry, "so","k","strikeouts","strikeOuts")),
        "wp":          to_int(  _get(entry, "wp","wildPitches","wild_pitches")),
        "hbp":         to_int(  _get(entry, "hbp","hitBatters","hitByPitch")),
        "num_pitches": to_int(  _get(entry, "numPitches","num_pitches","pitchCount","pitch_count","np")),
        "bf":          to_int(  _get(entry, "bf","battersFaced","batters_faced")),
    }

# Parse DOM table rows (header keys are ALL CAPS)
def parse_batter_dom(row: dict) -> dict:
    def g(*keys):
        for k in keys:
            if k in row: return row[k]
        return ""
    # Player name: GC sometimes puts "12  Jane Smith" in one cell
    raw_name = g("PLAYER","NAME","BATTER","_name_override")
    name = re.sub(r"^\d+\s+", "", str(raw_name)).strip()
    return {
        "player_name": name,
        "player_num":  str(g("#","NUM","NO","NUMBER")).strip(),
        "positions":   str(g("POS","POSITION","POSITIONS")).strip(),
        "ab":      to_int(g("AB","AT BATS")),
        "r":       to_int(g("R","RUNS")),
        "h":       to_int(g("H","HITS")),
        "rbi":     to_int(g("RBI")),
        "bb":      to_int(g("BB","WALKS")),
        "so":      to_int(g("SO","K","STRIKEOUTS")),
        "doubles": to_int(g("2B","DOUBLES")),
        "triples": to_int(g("3B","TRIPLES")),
        "hr":      to_int(g("HR","HOME RUNS")),
        "hbp":     to_int(g("HBP")),
        "sb":      to_int(g("SB","STOLEN BASES")),
        "cs":      to_int(g("CS")),
    }

def parse_pitcher_dom(row: dict) -> dict:
    def g(*keys):
        for k in keys:
            if k in row: return row[k]
        return ""
    raw_name = g("PLAYER","NAME","PITCHER","_name_override")
    name = re.sub(r"^\d+\s+", "", str(raw_name)).strip()
    return {
        "player_name": name,
        "player_num":  str(g("#","NUM","NO")).strip(),
        "ip":          to_float(g("IP","INNINGS")),
        "h":           to_int(g("H","HITS")),
        "r":           to_int(g("R","RUNS")),
        "er":          to_int(g("ER","EARNED RUNS")),
        "bb":          to_int(g("BB","WALKS")),
        "so":          to_int(g("SO","K","STRIKEOUTS")),
        "wp":          to_int(g("WP","WILD PITCHES")),
        "hbp":         to_int(g("HBP")),
        "num_pitches": to_int(g("NP","PITCHES","NUM PITCHES")),
        "bf":          to_int(g("BF","BATTERS FACED")),
    }

# ── Capture API responses via CDP performance log ─────────────────────────────

def get_api_responses(page) -> list:
    """
    Return captured XHR/fetch response bodies that look like box score API
    calls.  Playwright stores these via the response handler set in scrape_game.
    """
    return getattr(page, "_gc_api_blobs", [])

# ── Navigate + scrape one game ────────────────────────────────────────────────

def pick_side(fiber_result: dict, team_name: str) -> tuple:
    """
    When GC returns both home and away stats, fuzzy-match team_name to decide
    which side is ours.  Falls back to the top-level arrays if unclear.
    Returns (raw_batters, raw_pitchers).
    """
    home   = fiber_result.get("home", {})
    away   = fiber_result.get("away", {})
    h_name = home.get("name", "")
    a_name = away.get("name", "")
    h_bat  = home.get("batters") or []
    h_pit  = home.get("pitchers") or []
    a_bat  = away.get("batters") or []
    a_pit  = away.get("pitchers") or []
    top_bat = fiber_result.get("batters") or []
    top_pit = fiber_result.get("pitchers") or []

    if (h_name or a_name) and team_name:
        h_score = fuzzy_score(team_name, h_name) if h_name else 0.0
        a_score = fuzzy_score(team_name, a_name) if a_name else 0.0
        log(f"    Side match — home '{h_name}' {h_score:.2f} | away '{a_name}' {a_score:.2f}")
        if h_score > a_score and h_score >= 0.60:
            log(f"    → using HOME side (fuzzy match {h_score:.2f})")
            return (h_bat or top_bat), (h_pit or top_pit)
        if a_score > h_score and a_score >= 0.60:
            log(f"    → using AWAY side (fuzzy match {a_score:.2f})")
            return (a_bat or top_bat), (a_pit or top_pit)
        log(f"    → neither side ≥0.60 — using top-level arrays")

    return top_bat, top_pit


def scrape_game(page, gc_team_id: str, gc_event_id: str, team_name: str = "") -> tuple:
    """
    Navigate to a game's box score on GC and extract batting, pitching, and catching stats.
    team_name is used for fuzzy side-selection when GC returns both home/away.
    Returns (batters, pitchers, catchers) as lists of parsed dicts.
    Returns ([], [], []) if nothing found.
    """
    urls = [
        f"{GC_BASE}/teams/{gc_team_id}/schedule/{gc_event_id}/box-score",
        f"{GC_BASE}/teams/{gc_team_id}/schedule/{gc_event_id}",
        f"{GC_BASE}/teams/{gc_team_id}/games/{gc_event_id}",
    ]

    batters, pitchers, catchers = [], [], []

    for url in urls:
        log(f"    → {url}")
        # Capture API responses for this navigation
        page._gc_api_blobs = []
        def _capture(response):
            u = response.url.lower()
            if any(kw in u for kw in ["stat","box","batting","pitching","boxscore","game","event"]):
                try:
                    body = response.body()
                    if body[:1] in (b"{", b"["):
                        page._gc_api_blobs.append(json.loads(body))
                except Exception:
                    pass
        page.on("response", _capture)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
        except PWTimeout:
            log(f"    Page load timed out, proceeding with partial load")
        except Exception as e:
            log(f"    Navigation error: {e}")
            page.remove_listener("response", _capture)
            continue

        time.sleep(4)  # let initial render settle

        # ── Click the Box Score tab ───────────────────────────────────────
        try:
            tab = page.locator(
                "button:has-text('Box Score'), "
                "a:has-text('Box Score'), "
                "[role='tab']:has-text('Box Score')"
            ).first
            if tab.count() > 0:
                tab.click()
                log(f"    Clicked Box Score tab")
                time.sleep(REACT_WAIT)
            else:
                log(f"    Box Score tab not found — proceeding anyway")
                time.sleep(REACT_WAIT - 4)
        except Exception as te:
            log(f"    Tab click error: {te}")
            time.sleep(REACT_WAIT - 4)

        # ── Diagnostic: log page state + dump HTML structure ─────────────
        try:
            diag = page.evaluate("""() => ({
                title:    document.title,
                bodyLen:  document.body ? document.body.innerText.length : 0,
                hasNext:  !!document.getElementById('__next'),
                tables:   document.querySelectorAll('table').length,
                hasStat:  document.body.innerText.includes('AB') || document.body.innerText.includes('IP'),
                snippet:  document.body ? document.body.innerText.slice(0, 800) : '',
                outerSnippet: document.getElementById('__next')
                              ? document.getElementById('__next').innerHTML.slice(0, 1200)
                              : ''
            })""")
            log(f"    Page: bodyLen={diag.get('bodyLen')} tables={diag.get('tables')} "
                f"hasNext={diag.get('hasNext')} hasStat={diag.get('hasStat')}")
            log(f"    Text snippet: {diag.get('snippet','')!r}")
            # Dump every table's headers so we can see exactly what GC uses
            tbl_info = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('table').forEach((t, i) => {
                    const ths = [...t.querySelectorAll('th,thead td')].map(h => h.textContent.trim());
                    const firstRow = t.querySelector('tr') ? [...t.querySelector('tr').querySelectorAll('td,th')].map(c => c.textContent.trim()) : [];
                    out.push({i, ths, firstRow, rows: t.querySelectorAll('tr').length});
                });
                return out;
            }""")
            for ti in tbl_info:
                log(f"    Table {ti['i']}: {ti['rows']} rows | ths={ti['ths']} | first={ti['firstRow']}")
        except Exception as de:
            log(f"    Diag error: {de}")

        # ── Strategy 1: Text parse (primary — GC renders stats as plain text) ─
        try:
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            if body_text and ('LINEUP' in body_text or 'PITCHERS' in body_text):
                batters, pitchers, catchers = parse_boxscore_text(body_text, team_name)
                if batters or pitchers:
                    log(f"    Text parse: {len(batters)} batters, {len(pitchers)} pitchers, {len(catchers)} catchers")
        except Exception as e:
            log(f"    Text parse error: {e}")

        # ── Strategy 2: React fiber / __NEXT_DATA__ ──────────────────────────
        if not batters and not pitchers:
            try:
                fiber_result = page.evaluate(JS_EXTRACT)
                if not isinstance(fiber_result, dict):
                    raise ValueError(f"JS_EXTRACT returned {type(fiber_result).__name__}, expected dict")
                raw_batters, raw_pitchers = pick_side(fiber_result, team_name)
                source = fiber_result.get("source", "fiber")
                if raw_batters or raw_pitchers:
                    log(f"    Fiber ({source}): {len(raw_batters)} batters, {len(raw_pitchers)} pitchers")
                    batters  = [parse_batter(b)  for b in raw_batters  if isinstance(b, dict)]
                    pitchers = [parse_pitcher(p) for p in raw_pitchers if isinstance(p, dict)]
            except Exception as e:
                log(f"    Fiber JS error: {e}")

        # ── Strategy 2: CDP captured API responses ────────────────────────
        if not batters and not pitchers:
            log(f"    Trying CDP network capture…")
            page.remove_listener("response", _capture)
            api_blobs = get_api_responses(page)
            for blob in api_blobs:
                # Recursively hunt for batting/pitching arrays
                def hunt(obj, depth=0):
                    nonlocal batters, pitchers
                    if not isinstance(obj, dict) or depth > 8:
                        return
                    if batters and pitchers:
                        return
                    for k, v in obj.items():
                        kl = k.lower()
                        if isinstance(v, list) and v:
                            if not batters and ("batt" in kl or "hitter" in kl):
                                if isinstance(v[0], dict) and ("ab" in v[0] or "atBats" in v[0]):
                                    batters = [parse_batter(b) for b in v if isinstance(b, dict)]
                            if not pitchers and ("pitch" in kl):
                                if isinstance(v[0], dict) and ("ip" in v[0] or "inningsPitched" in v[0]):
                                    pitchers = [parse_pitcher(p) for p in v if isinstance(p, dict)]
                        elif isinstance(v, dict):
                            hunt(v, depth + 1)
                hunt(blob)
            if batters or pitchers:
                log(f"    CDP: {len(batters)} batters, {len(pitchers)} pitchers")

        # ── Strategy 3: DOM table extraction ─────────────────────────────
        if not batters and not pitchers:
            log(f"    Trying DOM table extraction…")
            try:
                dom = page.evaluate(JS_DOM_TABLES)
                if not isinstance(dom, dict):
                    raise ValueError(f"JS_DOM_TABLES returned {type(dom).__name__}, expected dict")
                log(f"    DOM debug: {dom.get('debug', [])}")
                raw_b = dom.get("batters", [])
                raw_p = dom.get("pitchers", [])
                if raw_b or raw_p:
                    log(f"    DOM: {len(raw_b)} batter rows, {len(raw_p)} pitcher rows")
                    batters  = [parse_batter_dom(b)  for b in raw_b  if isinstance(b, dict)]
                    pitchers = [parse_pitcher_dom(p) for p in raw_p  if isinstance(p, dict)]
            except Exception as e:
                log(f"    DOM JS error: {e}")

        # Filter nameless entries
        batters  = [b for b in batters  if b.get("player_name")]
        pitchers = [p for p in pitchers if p.get("player_name")]
        catchers = [c for c in catchers if c.get("player_name")]

        if batters or pitchers:
            break   # found data on this URL, stop trying

    return batters, pitchers, catchers

# ── Supabase helpers ──────────────────────────────────────────────────────────

def get_team_db_id(sb, gc_team_id: str):
    resp = sb.table("teams").select("id").eq("gc_team_id", gc_team_id).execute()
    if resp.data:
        return resp.data[0]["id"]
    return None

def games_needing_boxscore(sb, team_db_id: int, force: bool,
                           catching_only: bool = False,
                           pitching_only: bool = False) -> list:
    """
    Return games to process.
    catching_only=True  → games that already have batting but are missing catching stats.
    catching_only=False → games missing batting stats entirely.
    force               → return all games regardless.
    Each returned dict has a '_has_batting' key so the main loop can skip re-insert.
    """
    g_resp = sb.table("games") \
               .select("id,gc_event_id,date,opponent,home_away,innings") \
               .eq("team_id", team_db_id) \
               .execute()
    all_games = g_resp.data or []
    if not all_games:
        return []

    # Games with at least one batting-stats row
    bs_resp = sb.table("game_batting_stats") \
                .select("game_id") \
                .eq("team_id", team_db_id) \
                .execute()
    has_batting = {r["game_id"] for r in (bs_resp.data or [])}

    # Games with at least one catching-stats row
    cs_resp = sb.table("game_catching_stats") \
                .select("game_id") \
                .eq("team_id", team_db_id) \
                .execute()
    has_catching = {r["game_id"] for r in (cs_resp.data or [])}

    # Games with at least one pitching-stats row
    ps_resp = sb.table("game_pitching_stats") \
                .select("game_id") \
                .eq("team_id", team_db_id) \
                .execute()
    has_pitching = {r["game_id"] for r in (ps_resp.data or [])}

    if force:
        games = all_games
    elif pitching_only:
        # Only games missing pitching (has batting context but no pitching rows)
        games = [g for g in all_games
                 if g["id"] in has_batting and g["id"] not in has_pitching]
    elif catching_only:
        # Has batting, missing catching
        games = [g for g in all_games
                 if g["id"] in has_batting and g["id"] not in has_catching]
    else:
        # Missing batting entirely
        games = [g for g in all_games if g["id"] not in has_batting]

    # Tag each game so the main loop knows what already exists
    for g in games:
        g["_has_batting"]  = g["id"] in has_batting
        g["_has_pitching"] = g["id"] in has_pitching

    return games

def insert_batting(sb, team_db_id, game_db_id, gc_event_id, rows) -> int:
    if not rows:
        return 0
    payload = [
        {"team_id": team_db_id, "game_id": game_db_id, "gc_event_id": gc_event_id, **r}
        for r in rows if r.get("player_name")
    ]
    if not payload:
        return 0
    resp = sb.table("game_batting_stats").insert(payload).execute()
    return len(resp.data) if resp.data else 0

def insert_pitching(sb, team_db_id, game_db_id, gc_event_id, rows) -> int:
    if not rows:
        return 0
    payload = [
        {"team_id": team_db_id, "game_id": game_db_id, "gc_event_id": gc_event_id, **r}
        for r in rows if r.get("player_name")
    ]
    if not payload:
        return 0
    # Delete existing rows first to avoid duplicates on re-patch
    sb.table("game_pitching_stats") \
      .delete() \
      .eq("team_id", team_db_id) \
      .eq("game_id", game_db_id) \
      .execute()
    resp = sb.table("game_pitching_stats").insert(payload).execute()
    return len(resp.data) if resp.data else 0



def insert_catching(sb, team_db_id, game_db_id, gc_event_id, rows) -> int:
    if not rows:
        return 0
    # Delete existing rows first to avoid duplicates on re-patch
    sb.table("game_catching_stats") \
      .delete() \
      .eq("team_id", team_db_id) \
      .eq("game_id", game_db_id) \
      .execute()
    payload = [
        {
            "team_id":    team_db_id,
            "game_id":    game_db_id,
            "gc_event_id": gc_event_id,
            "player_name": r["player_name"],
            "player_num":  r.get("player_num"),
            "inn":         r.get("inn", 0),
            "pb":          r.get("pb", 0),
            "sb":          r.get("sb", 0),
            "sb_att":      r.get("sb_att", 0),
            "cs":          r.get("cs", 0),
            "cs_pct":      r.get("cs_pct"),
            "pik":         r.get("pik", 0),
            "ci":          r.get("ci", 0),
        }
        for r in rows if r.get("player_name")
    ]
    if not payload:
        return 0
    resp = sb.table("game_catching_stats").insert(payload).execute()
    return len(resp.data) if resp.data else 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Patch GC box score data (no name matching)")
    p.add_argument("--headless",  action="store_true", help="Run Chrome headless")
    p.add_argument("--team-id",   action="append", dest="team_ids",   metavar="ID",   default=[])
    p.add_argument("--team",      action="append", dest="team_names", metavar="NAME", default=[])
    p.add_argument("--limit",     type=int, default=0,
                   help="Max games to process per team (0 = all)")
    p.add_argument("--dry-run",   action="store_true",
                   help="Scrape but do not write to Supabase")
    p.add_argument("--force",          action="store_true",
                   help="Re-patch games that already have batting stats")
    p.add_argument("--catching-only",  action="store_true",
                   help="Only patch games that already have batting but are missing catching stats")
    p.add_argument("--pitching-only",  action="store_true",
                   help="Re-patch pitching only (delete+reinsert); skip batting and catching entirely")
    args = p.parse_args()

    if len(args.team_ids) != len(args.team_names):
        sys.exit("ERROR: --team-id and --team must be provided in pairs (same count)")
    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit("ERROR: Set SUPABASE_URL and SUPABASE_KEY environment variables")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # If no teams specified, pull every team from the DB
    if not args.team_ids:
        log("No --team-id specified — loading all teams from Supabase…")
        resp = sb.table("teams").select("gc_team_id,name").order("name").execute()
        all_teams = resp.data or []
        if not all_teams:
            sys.exit("ERROR: No teams found in Supabase teams table")
        teams = [(t["gc_team_id"], t["name"]) for t in all_teams]
        log(f"Found {len(teams)} teams in DB")
    else:
        teams = list(zip(args.team_ids, args.team_names))

    log(f"gc_boxscore_patch.py — {len(teams)} team(s) | headless={args.headless} "
        f"| dry_run={args.dry_run} | force={args.force} | catching_only={args.catching_only} "
        f"| pitching_only={args.pitching_only}")
    start_playwright()
    browser, ctx, page = new_page(args.headless)

    # ── Auth ─────────────────────────────────────────────────────────────────
    if args.headless:
        if not os.path.exists(STATE_FILE):
            browser.close(); stop_playwright()
            sys.exit("ERROR: No saved state. Run once WITHOUT --headless to log in first.")
        # Spot-check: confirm the state is still valid
        page.goto("https://web.gc.com", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT*1000)
        time.sleep(4)
        body_len = page.evaluate("document.body ? document.body.innerText.length : 0")
        if body_len < 1000:
            log("Saved state appears expired (page body too short).")
            os.remove(STATE_FILE)
            browser.close(); stop_playwright()
            sys.exit("ERROR: State expired. Run once WITHOUT --headless to log in again.")
        log("GC authenticated via saved state ✓")
    else:
        ensure_logged_in(page, ctx)

    totals = {"teams": 0, "games": 0, "batting": 0, "pitching": 0, "catching": 0}
    failed = []

    try:
        for gc_team_id, team_name in teams:
            log(f"\n{'═'*60}")
            log(f"  {team_name}  ({gc_team_id})")
            log(f"{'═'*60}")

            team_db_id = get_team_db_id(sb, gc_team_id)
            if not team_db_id:
                log(f"  ⚠  Not found in DB — skipping")
                continue

            missing = games_needing_boxscore(sb, team_db_id, args.force,
                                             args.catching_only, args.pitching_only)
            mode_label = ("catching" if args.catching_only
                          else "pitching" if args.pitching_only
                          else "box score")
            log(f"  {len(missing)} games need {mode_label} data")

            if not missing:
                log(f"  ✓ Nothing to patch")
                continue

            if args.limit:
                missing = missing[: args.limit]
                log(f"  (limited to {args.limit} games)")

            totals["teams"] += 1
            t_bat = t_pit = t_catch = 0

            for game in missing:
                game_id     = game["id"]
                gc_event_id = game["gc_event_id"]
                label       = f"{game.get('date','?')}  vs {game.get('opponent','?')}"
                log(f"\n  Game {gc_event_id[:8]}…  {label}")

                has_batting = game.get("_has_batting", False)

                batters = pitchers = catchers = None
                for attempt in range(1, RETRY_LIMIT + 1):
                    try:
                        batters, pitchers, catchers = scrape_game(page, gc_team_id, gc_event_id, team_name)
                        break
                    except Exception as exc:
                        log(f"  Attempt {attempt} error: {exc}")
                        if attempt < RETRY_LIMIT:
                            time.sleep(3)

                if batters  is None: batters  = []
                if pitchers is None: pitchers = []
                if catchers is None: catchers = []

                if not batters and not pitchers:
                    log(f"  ✗ No box score found")
                    failed.append({"team": team_name, "event": gc_event_id, "reason": "no_data"})
                    time.sleep(INTER_GAME)
                    continue

                # Log sampled player names
                if batters:
                    sample = ", ".join(b["player_name"] for b in batters[:4])
                    tail   = "…" if len(batters) > 4 else ""
                    log(f"  Batters  ({len(batters)}): {sample}{tail}")
                if pitchers:
                    sample = ", ".join(p["player_name"] for p in pitchers[:3])
                    tail   = "…" if len(pitchers) > 3 else ""
                    log(f"  Pitchers ({len(pitchers)}): {sample}{tail}")
                if catchers:
                    sample = ", ".join(c["player_name"] for c in catchers)
                    log(f"  Catchers ({len(catchers)}): {sample}")

                # Catching stat strategy:
                #   Single catcher → stats already in extras from box score parse;
                #                    set inn = game innings (no play-by-play needed).
                #   Multiple catchers → use play-by-play to split inn/SB/CS/PB.
                if catchers:
                    game_inn = game.get("innings") or 0
                    if len(catchers) == 1:
                        catchers[0]["inn"] = float(game_inn)
                        log(f"  Single catcher {catchers[0]['player_name']}: inn={game_inn} "
                            f"sb={catchers[0]['sb']} cs={catchers[0]['cs']} pb={catchers[0]['pb']}")
                    else:
                        try:
                            scouted_home = (game.get("home_away", "away") or "away").lower() == "home"
                            play_lines = fetch_play_lines(page, gc_team_id, gc_event_id)
                            if play_lines:
                                catchers = infer_catching_from_plays(
                                    catchers, batters, play_lines, scouted_home
                                )
                            else:
                                log(f"  [plays] No play lines — skipping multi-catcher inference")
                        except Exception as pe:
                            log(f"  [plays] Inference error: {pe}")

                    # If sub detection left some catchers at 0 innings, distribute
                    # the unaccounted innings equally among them.  Every catcher
                    # with 'C' in their box score positions DID catch; 0 just means
                    # the play-by-play substitution line wasn't matched.
                    total_inferred = sum(float(c.get('inn', 0)) for c in catchers)
                    zero_inn = [c for c in catchers if float(c.get('inn', 0)) == 0]
                    if zero_inn and game_inn and total_inferred < float(game_inn):
                        remaining = float(game_inn) - total_inferred
                        share = round(remaining / len(zero_inn), 1)
                        for c in zero_inn:
                            c['inn'] = share
                        log(f"  [plays] Distributed {remaining} remaining inn equally "
                            f"({share} each) to: "
                            f"{[c['player_name'] for c in zero_inn]}")

                if args.dry_run:
                    bat_note = "(skipped — already present)" if has_batting else f"{len(batters)} rows"
                    log(f"  [dry-run] batting={bat_note}  pitching={len(pitchers)}  catching={len(catchers)}")
                else:
                    try:
                        nb = np = nc = 0
                        if args.pitching_only:
                            np = insert_pitching(sb, team_db_id, game_id, gc_event_id, pitchers)
                        elif not has_batting:
                            nb = insert_batting( sb, team_db_id, game_id, gc_event_id, batters)
                            np = insert_pitching(sb, team_db_id, game_id, gc_event_id, pitchers)
                            nc = insert_catching(sb, team_db_id, game_id, gc_event_id, catchers)
                        else:
                            log(f"  Batting already in DB — skipping batting/pitching insert")
                            nc = insert_catching(sb, team_db_id, game_id, gc_event_id, catchers)
                        log(f"  ✓ Inserted {nb} batting, {np} pitching, {nc} catching rows")
                        t_bat   += nb
                        t_pit   += np
                        t_catch += nc
                        totals["batting"]  += nb
                        totals["pitching"] += np
                        totals["catching"] += nc
                    except Exception as exc:
                        log(f"  ✗ DB error: {exc}")
                        failed.append({"team": team_name, "event": gc_event_id, "reason": str(exc)})

                totals["games"] += 1
                time.sleep(INTER_GAME)

            log(f"\n  Team total: {t_bat} batting, {t_pit} pitching, {t_catch} catching rows")

    finally:
        try: browser.close()
        except Exception: pass
        stop_playwright()

    log(f"\n{'═'*60}")
    log(f"DONE")
    log(f"  Teams:    {totals['teams']}")
    log(f"  Games:    {totals['games']}")
    log(f"  Batting:  {totals['batting']} rows inserted")
    log(f"  Pitching: {totals['pitching']} rows inserted")
    log(f"  Catching: {totals['catching']} rows inserted")
    if failed:
        log(f"\n  Failed ({len(failed)}):")
        for f in failed:
            log(f"    {f['team']}  {f['event'][:8]}…  {f['reason']}")
    log(f"{'═'*60}")


if __name__ == "__main__":
    main()
