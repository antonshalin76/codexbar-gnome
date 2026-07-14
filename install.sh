#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="$HOME/.local/bin"
apps_dir="$HOME/.local/share/applications"
autostart_dir="$HOME/.config/autostart"

mkdir -p "$bin_dir" "$apps_dir" "$autostart_dir"

install -m 0755 "$repo_dir/bin/codexbar-gnome-indicator" "$bin_dir/codexbar-gnome-indicator"

desktop_tmp=$(mktemp)
sed "s|@HOME@|$HOME|g" "$repo_dir/share/codexbar-gnome-indicator.desktop.in" > "$desktop_tmp"
install -m 0644 "$desktop_tmp" "$apps_dir/codexbar-gnome-indicator.desktop"
install -m 0644 "$desktop_tmp" "$autostart_dir/codexbar-gnome-indicator.desktop"
rm -f "$desktop_tmp"

echo "Installed codexbar-gnome-indicator"
echo "Start it now with: gtk-launch codexbar-gnome-indicator"
