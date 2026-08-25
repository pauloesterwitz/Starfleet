#!/bin/bash
# Regenerates app-icon.icns from gen-icon.swift. Run from this directory.
set -euo pipefail

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

swift gen-icon.swift "$WORKDIR/master-1024.png"

mkdir -p "$WORKDIR/AppIcon.iconset"
for spec in "16:icon_16x16.png" "32:icon_16x16@2x.png" "32:icon_32x32.png" "64:icon_32x32@2x.png" \
            "128:icon_128x128.png" "256:icon_128x128@2x.png" "256:icon_256x256.png" \
            "512:icon_256x256@2x.png" "512:icon_512x512.png" "1024:icon_512x512@2x.png"; do
    size="${spec%%:*}"
    name="${spec##*:}"
    sips -z "$size" "$size" "$WORKDIR/master-1024.png" --out "$WORKDIR/AppIcon.iconset/$name" >/dev/null
done

iconutil -c icns "$WORKDIR/AppIcon.iconset" -o app-icon.icns
echo "wrote app-icon.icns"
