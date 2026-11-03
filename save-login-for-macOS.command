#!/usr/bin/env bash
# Saves (or replaces) your CWL login in the macOS Keychain for background re-login.
# Run "python3 elentra_keepalive.py --forget-login" to remove it.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ then run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi
"$PY" elentra_keepalive.py --set-login
echo
read -r -p "Press Enter to close..."
