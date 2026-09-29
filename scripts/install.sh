#!/usr/bin/env bash
# Fresh clone installer: creates the venv if needed, activates it, runs the interactive installer.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
. venv/bin/activate
exec python3 core/installer.py --install "$@"
