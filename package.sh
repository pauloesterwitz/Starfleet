#!/bin/bash
# Builds a real .app bundle so notifications/launch-at-login work and the
# app survives closing the terminal. Run on the Mac from the project root.
set -euo pipefail

APP="Starfleet Command.app"
swift build -c release

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Sources/OpencodeMonitor/Info.plist "$APP/Contents/Info.plist"
cp .build/release/OpencodeMonitor "$APP/Contents/MacOS/StarfleetCommand"
# App icon: shows in Finder/Get Info/Spotlight (not the Dock -- LSUIElement has no
# Dock icon). Built from the Starfleet delta mark via assets/gen-icon.sh;
# regenerate that script's output if the mark or badge design ever changes.
cp assets/app-icon.icns "$APP/Contents/Resources/app-icon.icns"
codesign --force --sign - "$APP" >/dev/null

echo "Built $APP. Move it to /Applications (or ~/Applications) and open it,"
echo "or run it in place with: open $APP"
