#!/usr/bin/env bash
# Install a launcher for the current user so the app shows up in the
# desktop's application search (GNOME, KDE, ...). Runs from this checkout,
# so pulling new code updates the installed app.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

(cd "$repo" && uv sync --quiet)

mkdir -p "$apps" "$icons"
install -m 644 "$repo/src/kicad_project_manager/resources/kicad-project-manager.svg" "$icons/"
sed "s|^Exec=.*|Exec=$repo/.venv/bin/kicad-project-manager|" \
    "$repo/packaging/linux/kicad-project-manager.desktop" > "$apps/kicad-project-manager.desktop"
chmod 644 "$apps/kicad-project-manager.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database -q "$apps" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${icons%/scalable/apps}" || true

echo "Installed. Search for 'KiCad Project Manager' in your application launcher."
