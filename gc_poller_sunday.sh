#!/bin/bash
# Sunday tournament poller: started by launchd at 9:30 AM on Sundays.
# Checks the scrape_jobs queue every 10s so the app's "Refresh scores" button runs right away.
# Stops at 8:00 PM, or as soon as the file .poller_stop exists (touch it when eliminated).
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
export BACKPIC_GUI=1                    # this runs in the logged-in desktop session
rm -f .poller_stop                      # fresh start each Sunday
END=$(date -v20H -v0M -v0S +%s)
while [ ! -f .poller_stop ] && [ "$(date +%s)" -lt "$END" ]; do
  /opt/homebrew/bin/python3 gc_scrape_poller.py
  sleep 10
done
echo "[$(date '+%F %T')] Sunday poller stopped ($( [ -f .poller_stop ] && echo 'stop file' || echo '8 PM' ))"
