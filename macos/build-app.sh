#!/usr/bin/env bash
# Build "AI Radio Host.app" around an installed venv (used by the Homebrew formula).
#
#   macos/build-app.sh VENV_DIR OUTPUT.app [VERSION]
#
# VENV_DIR is baked into the launcher and may start with ~/ (expanded at launch). The app holds
# no Python code itself, so it keeps working when the venv is upgraded in place.
# Needs: cc (Xcode Command Line Tools), rsvg-convert (librsvg), iconutil.
set -euo pipefail

VENV="$1"
APP="$2"
VERSION="${3:-0}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${HOMEBREW_PREFIX:-$(brew --prefix)}"
SVG="$SRC/data/icons/hicolor/scalable/apps/dev.zavoram.RadioHost.svg"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed "s|@VERSION@|$VERSION|g" "$SRC/macos/Info.plist" > "$APP/Contents/Info.plist"

cc -O2 -Wall -DVENV="\"$VENV\"" -DHOMEBREW_PREFIX="\"$PREFIX\"" \
   -Wl,-rpath,"$PREFIX/lib" -o "$APP/Contents/MacOS/AI Radio Host" "$SRC/macos/launcher.c"

ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  rsvg-convert -w "$s" -h "$s" "$SVG" -o "$ICONSET/icon_${s}x${s}.png"
  rsvg-convert -w $((s * 2)) -h $((s * 2)) "$SVG" -o "$ICONSET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"
echo "Built $APP"
