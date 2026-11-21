#!/bin/bash
# End to end test of the Defiant updates: the real fleet.py against a made-up Defiant, then the real desktop and
# phone pages in WebKit. Starts what it needs on free random ports, prints one line per check, always tears
# everything down and exits 0 only when every check passed. Nothing real is touched (see README.md).
#
#   ./run.sh              every story, about 6 minutes
#   ./run.sh install      only the stories whose name contains "install" (the names are in e2e.py)
#   PW=<folder of the playwright package>   needed for the page stories (page-desktop, page-phone)
#   OUT=<folder>          the pictures of the page stories (default: ./out)
#   KEEP=1                keep the sandbox folder with the logs of Fleet, the host and Telegram
#   FONTS=1               let the pages load Google Fonts, for prettier pictures (otherwise the browser's proxy is a dead loopback port)
cd "$(dirname "$0")" || exit 2
command -v python3 > /dev/null || { echo "run.sh: python3 is needed" >&2; exit 2; }
# Whatever the shell holds for a real Fleet (the push key, the events folder) is not passed on: Fleet gets a fresh
# environment from e2e.py, and these never reach it or the page scripts either.
unset APNS_KEY_FILE APNS_KEY_ID APNS_TEAM_ID APNS_TOPIC FLEET_APNS_HOST FLEET_EVENTS_DIR
python3 e2e.py "$@" &
py=$!
trap 'kill -TERM $py 2> /dev/null' INT TERM
wait $py
rc=$?
wait $py 2> /dev/null         # after a signal: let the teardown finish
exit $rc
