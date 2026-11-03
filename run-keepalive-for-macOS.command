#!/usr/bin/env bash
# Elentra keepalive launcher for macOS (foreground — shows live status; Ctrl+C to stop; may take up to ~25 seconds to stop).
# Load the Brave extension in ./brave-extension once first (see its README).
# Double-click this file in Finder to run it (the .command extension makes Finder open it in Terminal).
# If Finder says it can't be opened, right-click it > Open > Open, or run once:  chmod +x run-keepalive-for-macOS.command
set -u
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ then run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

# The script prints its own startup notes and keeps the Mac awake itself (caffeinate) while it runs.
"$PY" elentra_keepalive.py "$@"

echo
echo "Keepalive exited."
read -r -p "Press Enter to close..."
