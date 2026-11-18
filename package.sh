#!/bin/bash
# Builds a real .app bundle so notifications/launch-at-login work and the
# app survives closing the terminal. Run on the Mac from the project root.
set -euo pipefail

APP="Starfleet Command.app"

# Panel contract, checked BEFORE building. The app writes numbers into the panel's
# channels and the theme flashed onto it (panel-theme/img.dat, via upload_theme.py)
# decides what each number looks like -- when the two disagree the panel draws the
# wrong words with no error anywhere (2026-09-14: GLM-5.3 rendered as "OTHER TP2").
# Fails the build if PanelController.swift and build_theme.py disagree; warns if the
# theme last flashed onto the panel is not the one this build speaks.
python3 panel-theme/panel_contract.py check

# In the macOS 27 SDK SwiftUI's @State is a macro, and its plugin (SwiftUIMacros) ships
# with Xcode.app only: built with just the Command Line Tools selected, every @State fails
# with "plugin for module 'SwiftUIMacros' not found". Use Xcode for this build when it is
# installed and nothing else was chosen; DEVELOPER_DIR set by hand still wins.
if [ -z "${DEVELOPER_DIR:-}" ] && [ -d /Applications/Xcode.app ] \
   && xcode-select -p | grep -q CommandLineTools; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi

swift build -c release

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Sources/OpencodeMonitor/Info.plist "$APP/Contents/Info.plist"
cp .build/release/OpencodeMonitor "$APP/Contents/MacOS/StarfleetCommand"
# App icon: shows in Finder/Get Info/Spotlight (not the Dock -- LSUIElement has no
# Dock icon). Built from the Starfleet delta mark via assets/gen-icon.sh;
# regenerate that script's output if the mark or badge design ever changes.
cp assets/app-icon.icns "$APP/Contents/Resources/app-icon.icns"
# Stamp the contract this build speaks into the bundle, so upload_theme.py can refuse
# to flash a theme this app would drive wrong. Before codesign: it is a sealed resource.
python3 panel-theme/panel_contract.py stamp "$APP/Contents/Resources/panel-contract.json"
codesign --force --sign - "$APP" >/dev/null

echo "Built $APP. Move it to /Applications (or ~/Applications) and open it,"
echo "or run it in place with: open $APP"
