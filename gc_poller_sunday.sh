#!/bin/bash
# Sunday tournament poller: started by launchd at 9:30 AM on Sundays.
# Checks the scrape_jobs queue every 10s so the app's "Refresh scores" button runs right away.
# Stops at 11:59 PM, or as soon as the file .poller_stop exists (touch it when eliminated).
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
rm -f .poller_stop                      # fresh start each Sunday
END=$(date -v23H -v59M -v0S +%s)
while [ ! -f .poller_stop ] && [ "$(date +%s)" -lt "$END" ]; do
  /opt/homebrew/bin/python3 gc_scrape_poller.py
  sleep 10
done
echo "[$(date '+%F %T')] Sunday poller stopped ($( [ -f .poller_stop ] && echo 'stop file' || echo 'end of day' ))"
