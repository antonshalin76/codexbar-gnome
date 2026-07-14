#!/usr/bin/env sh
set -eu

pkill -f "^python3 $HOME/.local/bin/codexbar-gnome-indicator$" 2>/dev/null || true
rm -f "$HOME/.local/bin/codexbar-gnome-indicator"
rm -f "$HOME/.local/share/applications/codexbar-gnome-indicator.desktop"
rm -f "$HOME/.config/autostart/codexbar-gnome-indicator.desktop"

echo "Removed codexbar-gnome-indicator"
