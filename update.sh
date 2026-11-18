#!/bin/bash
# Self-update, run by Starfleet Command at every start (SelfUpdate in App.swift) from the
# clone the running build came from, with the app's bundle path and process id:
#   1. fast-forwards main from origin (never a merge, never on another branch),
#   2. iPhone: when ios/ changed since the last install and signing is set up
#      (ios/Local.xcconfig), builds the app and installs it on the paired iPhone,
#   3. Mac: when the app's sources changed since this build, rebuilds with package.sh,
#      quits the running app, swaps the bundle and opens the new one.
# The web page needs none of this: Fleet serves it fresh.
set -euo pipefail
exec >>"$HOME/Library/Logs/Starfleet Command update.log" 2>&1
echo "== $(date '+%F %T') update check"

APP_PATH=${1:?app bundle path}
APP_PID=${2:?app process id}
case "$APP_PATH" in */*.app) ;; *) echo "not an app bundle: $APP_PATH"; exit 1 ;; esac

cd "$(dirname "$0")"
EMPTY_TREE=$(git hash-object -t tree /dev/null)

[ "$(git symbolic-ref --short HEAD)" = main ] || { echo "clone is not on main, no update"; exit 0; }
if git fetch -q origin main; then
    git merge -q --ff-only origin/main || echo "main has diverged from origin, staying on $(git rev-parse --short HEAD)"
else
    echo "fetch failed (offline?), checking against $(git rev-parse --short HEAD)"
fi

# Same rule as package.sh: Xcode, not the Command Line Tools (and xcodebuild needs Xcode anyway).
if [ -z "${DEVELOPER_DIR:-}" ] && [ -d /Applications/Xcode.app ] \
   && xcode-select -p | grep -q CommandLineTools; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi

# iPhone. An iOS app cannot replace itself, so the Mac installs it. A failure here never
# blocks the Mac update; the next start tries again (the marker only moves on success).
install_iphone() {
    local marker=ios/build/installed-commit since json device
    [ -f ios/Local.xcconfig ] || { echo "iPhone: no ios/Local.xcconfig (signing team) yet, skipped"; return 0; }
    since=$(cat "$marker" 2>/dev/null || echo "$EMPTY_TREE")
    git diff --quiet "$since" HEAD -- ios 2>/dev/null && return 0
    json=$(mktemp)
    xcrun devicectl list devices --json-output "$json" >/dev/null || return 1
    device=$(python3 - "$json" <<'PY'
import json, sys
for d in json.load(open(sys.argv[1])).get("result", {}).get("devices", []):
    if d.get("hardwareProperties", {}).get("deviceType") == "iPhone" \
       and d.get("connectionProperties", {}).get("pairingState") == "paired":
        print(d["identifier"]); break
PY
)
    rm -f "$json"
    [ -n "$device" ] || { echo "iPhone: no paired iPhone, retrying at the next start"; return 1; }
    echo "iPhone: building and installing $(git rev-parse --short HEAD)"
    mkdir -p ios/build
    xcodebuild -quiet -project ios/Starfleet.xcodeproj -target Starfleet -configuration Release \
        -sdk iphoneos -allowProvisioningUpdates CONFIGURATION_BUILD_DIR="$PWD/ios/build" build \
      && xcrun devicectl device install app --device "$device" ios/build/Starfleet.app >/dev/null \
      && git rev-parse HEAD > "$marker" \
      && echo "iPhone: installed"
}
install_iphone || echo "iPhone: not updated"

# Mac. package.sh stamps the commit it built into the bundle.
built=$(cat "$APP_PATH/Contents/Resources/build-commit" 2>/dev/null || echo "$EMPTY_TREE")
if git diff --quiet "$built" HEAD -- Sources Package.swift package.sh assets panel-theme 2>/dev/null; then
    echo "Mac: up to date"
    exit 0
fi
echo "Mac: rebuilding $(git rev-parse --short HEAD)"
./package.sh
kill "$APP_PID" 2>/dev/null || true
while kill -0 "$APP_PID" 2>/dev/null; do sleep 0.2; done
if [ ! "$APP_PATH" -ef "Starfleet Command.app" ]; then
    rm -rf "$APP_PATH"
    mv "Starfleet Command.app" "$APP_PATH"
fi
open "$APP_PATH"
echo "Mac: updated and relaunched"
