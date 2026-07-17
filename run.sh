#!/bin/bash
# Load .env and run the recognizer using the venv's python.
# Handy for testing by hand; the launchd plist runs the same thing at boot.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec ./venv/bin/python recognizer.py
