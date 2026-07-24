#!/usr/bin/env python3
"""
gc_scrape_poller.py
Polls Supabase for pending scrape_jobs and runs gc_scraper.py for each.
Cron entry:
  */5 * * * * /Users/bryanhaviland/backpic-scouting-v2/gc_scrape_poller.py >> /Users/bryanhaviland/backpic-scouting-v2/poller.log 2>&1
"""

import os, sys, subprocess, datetime, pathlib, urllib.request, urllib.error, json

SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
ENV_FILE   = SCRIPT_DIR / ".env"
SCRAPER    = SCRIPT_DIR / "gc_scraper.py"
PYTHON     = sys.executable

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def load_env():
    if not ENV_FILE.exists():
        log(f"ERROR: .env not found at {ENV_FILE}"); sys.exit(1)
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def sb_request(url, method, key, data=None):
    body = json.dumps(data).encode() if data else None
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
                "Content-Type": "application/json", "Prefer": "return=representation"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code}: {e.read().decode()}"); return None

def get_pending_jobs(supabase_url, key):
    url = f"{supabase_url}/rest/v1/scrape_jobs?status=eq.pending&order=created_at.asc&limit=3"
    return sb_request(url, "GET", key) or []

def update_job(supabase_url, key, job_id, fields):
    url = f"{supabase_url}/rest/v1/scrape_jobs?id=eq.{job_id}"
    sb_request(url, "PATCH", key, fields)

def main():
    env = load_env()
    supabase_url = env.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = env.get("SUPABASE_KEY", "")
    if not supabase_url or not supabase_key:
        log("ERROR: SUPABASE_URL or SUPABASE_KEY missing from .env"); sys.exit(1)

    jobs = get_pending_jobs(supabase_url, supabase_key)
    if not jobs: sys.exit(0)

    log(f"Found {len(jobs)} pending job(s)")
    run_env = os.environ.copy()
    run_env.update({k: v for k, v in env.items()})

    for job in jobs:
        job_id, team_name = job["id"], job["team_name"]
        log(f"Starting job {job_id} — team: {team_name!r}")
        update_job(supabase_url, supabase_key, job_id, {
            "status": "running",
            "started_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
        try:
            result = subprocess.run(
                [PYTHON, str(SCRAPER), "--team", team_name, "--headless", "--timeout", "30000"],
                capture_output=True, text=True, timeout=900,
                cwd=str(SCRIPT_DIR), env=run_env,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0:
                summary = next((l.strip() for l in reversed(output.splitlines())
                                if "✓" in l or "games=" in l), "Completed")
                log(f"Job {job_id} completed: {summary}")
                update_job(supabase_url, supabase_key, job_id, {
                    "status": "completed",
                    "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "result_summary": summary,
                })
            else:
                err_msg = " | ".join(output.strip().splitlines()[-3:])
                log(f"Job {job_id} FAILED: {err_msg}")
                update_job(supabase_url, supabase_key, job_id, {
                    "status": "failed",
                    "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "error": err_msg[:500],
                })
        except subprocess.TimeoutExpired:
            log(f"Job {job_id} timed out")
            update_job(supabase_url, supabase_key, job_id, {
                "status": "failed", "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                "error": "Timed out after 15 minutes",
            })
        except Exception as e:
            log(f"Job {job_id} exception: {e}")
            update_job(supabase_url, supabase_key, job_id, {
                "status": "failed", "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                "error": str(e)[:500],
            })

if __name__ == "__main__":
    main()
