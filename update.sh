#!/bin/bash
# Self-update, run by Starfleet Command at every start and from Check for Updates (SelfUpdate
# in App.swift) in the clone the running build came from, with the app's bundle path and
# process id:
#   1. fast-forwards main from Gitea (the remote "gitea"; never a merge, never on another branch),
#   2. Mac: when the app's sources changed since this build, rebuilds with package.sh,
#      swaps the bundle, quits the running app and opens the new one (whose own start
#      then does step 3),
#   3. iPhone: when ios/ changed since the last install, or the installed build's signing
#      profile has expired, and signing is set up (ios/Local.xcconfig), builds the app and
#      installs it on the iPhone that is connected now.
# The web page needs none of this: Fleet serves it fresh.
#
# Exit codes, for the dropdown: 0 nothing (more) to do, 3 Gitea not reachable, 4 the clone
# is not on main, cannot fast-forward, or has uncommitted changes in the app's sources, 5 the
# Mac app is up to date but the iPhone app was not updated, 6 another update is running;
# anything else is a failure. A Mac update does not report back: it quits the app and opens
# the new one.
set -euo pipefail
exec >>"$HOME/Library/Logs/Starfleet Command update.log" 2>&1
echo "== $(date '+%F %T') update check"

APP_PATH=${1:?app bundle path}
APP_PID=${2:?app process id}
case "$APP_PATH" in */*.app) ;; *) echo "not an app bundle: $APP_PATH"; exit 1 ;; esac

cd "$(dirname "$0")"
EMPTY_TREE=$(git hash-object -t tree /dev/null)

# One run per clone. The app allows one at a time, but a run outlives the app it was started
# from, so quitting and reopening during a build would start a second. shlock (macOS) clears
# a lock whose process is gone; one whose number now belongs to something else is dropped
# here. The same trap opens the app again once it has been quit.
LOCK="$(git rev-parse --git-dir)/starfleet-update.lock"
if [ -f "$LOCK" ]; then
    case "$(ps -p "$(cat "$LOCK")" -o command= 2>/dev/null)" in *update.sh*) ;; *) rm -f "$LOCK" ;; esac
fi
shlock -f "$LOCK" -p $$ || { echo "another update is running"; exit 6; }
trap 'rm -f "$LOCK"; [ -z "${relaunch:-}" ] || open "$APP_PATH"' EXIT

[ "$(git symbolic-ref -q --short HEAD)" = main ] || { echo "clone is not on main, no update"; exit 4; }
stuck=0
# Updates come from Gitea on the NAS, not GitHub. The clone may have been made from GitHub, so the remote is added on first use;
# `git config starfleet.updateRemote <name>` or `starfleet.updateUrl <url>` overrides it.
REMOTE=$(git config --get starfleet.updateRemote || echo gitea)
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
    git remote add "$REMOTE" "$(git config --get starfleet.updateUrl || echo ssh://git@deep-space-nas:2223/cluster/starfleet.git)"
fi
if git fetch -q "$REMOTE" main; then
    git merge -q --ff-only "$REMOTE/main" \
        || { echo "main cannot fast-forward (local commits or changes), staying on $(git rev-parse --short HEAD)"; stuck=4; }
else
    echo "fetch failed (offline?), checking against $(git rev-parse --short HEAD)"; stuck=3
fi

# Same rule as package.sh: Xcode, not the Command Line Tools (and xcodebuild needs Xcode anyway).
if [ -z "${DEVELOPER_DIR:-}" ] && [ -d /Applications/Xcode.app ] \
   && xcode-select -p | grep -q CommandLineTools; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi

# iPhone. An iOS app cannot replace itself, so the Mac installs it. The marker only moves on
# success, so a phone that is away gets the build at a later start or check.
install_iphone() {
    local marker=ios/build/installed-commit since exp json device
    [ -f ios/Local.xcconfig ] || { echo "iPhone: no ios/Local.xcconfig (signing team) yet, skipped"; return 0; }
    since=$(cat "$marker" 2>/dev/null || echo "$EMPTY_TREE")
    # A free Apple ID signs for 7 days only: once the installed build's profile has expired,
    # sign and install again without a change in ios/.
    exp=$(security cms -D -i ios/build/Starfleet.app/embedded.mobileprovision 2>/dev/null \
          | plutil -extract ExpirationDate raw -o - - 2>/dev/null || true)
    if [ -n "$exp" ] && [[ "$exp" < "$(date -u +%FT%TZ)" ]]; then
        echo "iPhone: the signing profile expired $exp, signing again"
        since=$EMPTY_TREE
        # The marker goes too, so a phone that is away now still gets it at a later run.
        rm -rf ios/build/Starfleet.app "$marker"
    fi
    git diff --quiet "$since" HEAD -- ios 2>/dev/null && return 0
    json=$(mktemp)
    xcrun devicectl list devices --json-output "$json" >/dev/null || { rm -f "$json"; return 1; }
    # CoreDevice also lists paired phones that are away; only one on the cable or the
    # network now can take the install.
    device=$(python3 - "$json" <<'PY'
import json, sys
for d in json.load(open(sys.argv[1])).get("result", {}).get("devices", []):
    c = d.get("connectionProperties", {})
    if d.get("hardwareProperties", {}).get("deviceType") == "iPhone" \
       and c.get("pairingState") == "paired" \
       and (c.get("tunnelState") == "connected"
            or (c.get("transportType") or "").lower() in ("wired", "localnetwork")):
        print(d["identifier"]); break
PY
)
    rm -f "$json"
    [ -n "$device" ] || { echo "iPhone: no paired iPhone connected now (cable or Wi-Fi)"; return 1; }
    echo "iPhone: building and installing $(git rev-parse --short HEAD)"
    mkdir -p ios/build
    xcodebuild -quiet -project ios/Starfleet.xcodeproj -target Starfleet -configuration Release \
        -sdk iphoneos -allowProvisioningUpdates CONFIGURATION_BUILD_DIR="$PWD/ios/build" build \
      && xcrun devicectl device install app --device "$device" ios/build/Starfleet.app >/dev/null \
      && git rev-parse HEAD > "$marker" \
      && echo "iPhone: installed"
}

# Mac. package.sh stamps the commit it built into the bundle.
built=$(cat "$APP_PATH/Contents/Resources/build-commit" 2>/dev/null || echo "$EMPTY_TREE")
if git diff --quiet "$built" HEAD -- Sources Package.swift package.sh assets panel-theme 2>/dev/null; then
    echo "Mac: up to date"
    iphone=0
    install_iphone || { echo "iPhone: not updated"; iphone=5; }
    [ "$stuck" -ne 0 ] && exit "$stuck"
    exit "$iphone"
fi
echo "Mac: rebuilding $(git rev-parse --short HEAD)"
# The stamp says HEAD, so a build of uncommitted edits would pass for HEAD later on. SwiftPM
# also compiles untracked Swift files.
dirty=$(git status --porcelain -- 'Sources/*.swift'; git status --porcelain -uno -- Sources Package.swift package.sh assets panel-theme)
[ -z "$dirty" ] || { echo "uncommitted changes in the app's sources, not building:"; echo "$dirty"; exit 4; }
./package.sh
# Everything that can fail happens while the app still runs (macOS lets a running app's
# bundle be renamed): a failure stops the update here, with the app up and no quit-reopen
# loop. Renames only, never a delete inside the old bundle, so the old one stays whole.
new="$APP_PATH.new" old="$APP_PATH.old.$$"
if [ ! "$APP_PATH" -ef "Starfleet Command.app" ]; then
    rm -rf "$new"
    mv "Starfleet Command.app" "$new"
    mv "$APP_PATH" "$old"
    mv "$new" "$APP_PATH" || { mv "$old" "$APP_PATH"; exit 1; }
fi
# Quit every copy running from this path: the one that started this run, and one opened
# again during the build (its own run stopped at the lock).
bin=$(printf '%s' "$APP_PATH/Contents/MacOS/StarfleetCommand" | sed 's/[][\.*^$+?(){}|]/\\&/g')
kill "$APP_PID" 2>/dev/null || true
pkill -U "$(id -u)" -f "$bin" || true
while kill -0 "$APP_PID" 2>/dev/null || pgrep -U "$(id -u)" -f "$bin" >/dev/null; do sleep 0.2; done
relaunch=1
rm -rf "$old" || echo "could not remove $old"
echo "Mac: updated, relaunching"
