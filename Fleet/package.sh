#!/bin/bash
# Builds a real .app bundle so it has a Dock icon, its own window, and
# survives closing the terminal. Run on the Mac from the project root.
set -euo pipefail

APP="Fleet.app"
swift build -c release

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Sources/Fleet/Info.plist "$APP/Contents/Info.plist"
cp .build/release/Fleet "$APP/Contents/MacOS/Fleet"
cp assets/app-icon.icns "$APP/Contents/Resources/app-icon.icns"
codesign --force --sign - "$APP" >/dev/null

echo "Built $APP. Move it to /Applications (or ~/Applications) and open it,"
echo "or run it in place with: open \"$APP\""
