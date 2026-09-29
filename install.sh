#!/bin/sh
# Install rosebuds commands and desktop integration in user directories.
#
# Usage: ./install.sh              install the commands, icons and app menu entry, start the tray app at login
#        ./install.sh --uninstall  remove all of that again
set -eu

cd "$(dirname "$0")"
bin=$HOME/.local/bin
data=${XDG_DATA_HOME:-$HOME/.local/share}
app=$data/rosebuds
icons=$data/icons/hicolor/scalable/status
autostart=${XDG_CONFIG_HOME:-$HOME/.config}/autostart/rosebuds.desktop
menu=$data/applications/rosebuds.desktop

reload_icons() {
    # Plasma caches icon lookups; without this the tray icon stays blank until next login.
    dbus-send --session --type=signal /KIconLoader org.kde.KIconLoader.iconChanged int32:0 2>/dev/null || true
}

# Usage: write_entry <file> [extra argument]
# Absolute path: ~/.local/bin isn't always on PATH when autostart runs.
write_entry() {
    mkdir -p "$(dirname "$1")"
    python3 - "$1" "\"$app/rosebuds-tray.py\"${2:+ $2}" <<'EOF'
import pathlib
import sys

template = pathlib.Path("rosebuds.desktop").read_text()
pathlib.Path(sys.argv[1]).write_text(template.replace("@EXEC@", sys.argv[2]))
EOF
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$bin/rosebuds" "$bin/rosebuds-tray" "$autostart" "$menu"
    for f in icons/rosebuds*.svg; do rm -f "$icons/${f##*/}"; done
    rm -rf "$app"
    reload_icons
    echo "Removed the commands, icons, app menu entry and autostart entry."
    exit 0
fi

if ! python3 -c "import PySide6.QtSvg" 2>/dev/null; then
    echo "PySide6 is missing. Install it with your package manager and re-run the install script." >&2
    exit 1
fi

mkdir -p "$bin" "$app/icons"
install -m 755 rosebuds.py rosebuds-tray.py "$app/"
install -m 644 icons/*.svg "$app/icons/"
ln -sf "$app/rosebuds.py" "$bin/rosebuds"
ln -sf "$app/rosebuds-tray.py" "$bin/rosebuds-tray"

mkdir -p "$icons"
install -m 644 icons/rosebuds*.svg "$icons/"
reload_icons

# From the menu it opens the panel; at login it starts in the tray only.
write_entry "$menu" --show
write_entry "$autostart"

echo "Installed. The tray app will start at login."
echo "To start it now, open rosebuds from the app menu."
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Note: $bin is not on your PATH, so the rosebuds commands won't be found." ;;
esac
