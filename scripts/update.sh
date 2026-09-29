#!/usr/bin/env bash
# Non interactive updater: pulls main, installs requirements, applies pending migrations, restarts the service.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ ! -d venv ]; then
  echo "update: venv/ not found, run scripts/install.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
. venv/bin/activate
exec python3 core/installer.py --update "$@"
