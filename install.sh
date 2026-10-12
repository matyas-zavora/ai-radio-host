#!/usr/bin/env bash
# Install AI Radio Host for the current user (no sudo).
#
#   ./install.sh              install or update from this folder
#   ./install.sh --uninstall  remove the app (keeps ~/.config/ai-radio-host and caches)
#
# Needs system packages (Arch): python python-gobject gtk4 libadwaita pipewire
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA_HOME/ai-radio-host/app"
BIN_DIR="$HOME/.local/bin"
CONF_DIR="$CONFIG_HOME/ai-radio-host"
APPS_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
DESKTOP="dev.zavoram.RadioHost.desktop"

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$APP_DIR"
  rm -f "$BIN_DIR/radiohost" "$BIN_DIR/radiohost-gui" "$APPS_DIR/$DESKTOP"
  rm -f "$ICONS_DIR"/ai-radio-host-*.svg "$ICONS_DIR/dev.zavoram.RadioHost.svg"
  echo "Removed. Your settings and keys are still in $CONF_DIR (delete it by hand if you want)."
  exit 0
fi

PY=/usr/bin/python3
if ! "$PY" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
  echo "Missing GTK4/libadwaita for Python. On Arch: sudo pacman -S python-gobject gtk4 libadwaita"
  exit 1
fi
command -v pw-play >/dev/null || command -v paplay >/dev/null || command -v mpv >/dev/null \
  || echo "Warning: no audio player found (pw-play, paplay or mpv); the host won't be audible."

echo "== Installing into $APP_DIR"
# --system-site-packages: PyGObject comes from the system, not from PyPI.
if command -v uv >/dev/null; then
  [ -x "$APP_DIR/bin/python" ] || uv venv -q --system-site-packages -p "$PY" "$APP_DIR"
  uv pip install -q --python "$APP_DIR/bin/python" --reinstall-package ai-radio-host "$SRC[piper,google]"
else
  [ -x "$APP_DIR/bin/python" ] || "$PY" -m venv --system-site-packages "$APP_DIR"
  "$APP_DIR/bin/pip" install -q --upgrade "$SRC[piper,google]"
fi

mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICONS_DIR" "$CONF_DIR"
ln -sf "$APP_DIR/bin/radiohost" "$BIN_DIR/radiohost"
ln -sf "$APP_DIR/bin/radiohost-gui" "$BIN_DIR/radiohost-gui"
cp "$SRC"/data/icons/hicolor/scalable/apps/*.svg "$ICONS_DIR/"
sed "s|@VENV@|$APP_DIR|" "$SRC/data/$DESKTOP" > "$APPS_DIR/$DESKTOP"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS_DIR" || true

if [ ! -e "$CONF_DIR/config.toml" ]; then
  cp "$SRC/config.example.toml" "$CONF_DIR/config.toml"
  echo "Created $CONF_DIR/config.toml"
fi
if [ ! -e "$CONF_DIR/.env" ]; then
  (umask 077 && cp "$SRC/.env.example" "$CONF_DIR/.env")
  echo "Created $CONF_DIR/.env (fill in your keys)"
fi

echo
echo "Installed. Next steps:"
echo "  1. Put your keys in $CONF_DIR/.env (see README: Spotify app, Anthropic key, Google TTS)"
echo "  2. radiohost auth          # Spotify login, once"
echo "  3. Open 'AI Radio Host' from your launcher (or run: radiohost-gui)"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Note: $BIN_DIR is not in your PATH.";; esac
