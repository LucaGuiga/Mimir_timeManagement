#!/usr/bin/env bash
# Builds dist/mimir-lambda.zip for Lambda python3.12 x86_64. boto3 is already in the runtime, so it is not packaged.
# Handler: lambda_handler.handler. Set MIMIR_FRONTEND_DIST=/var/task/frontend_dist and the other MIMIR_* variables on the function.
# Run `npm run build` in frontend/ first so the dashboard is included.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/dist/lambda-build"
rm -rf "$BUILD" "$ROOT/dist/mimir-lambda.zip"
mkdir -p "$BUILD"
python3 -m pip install --quiet --upgrade --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: --target "$BUILD" fastapi mangum pyyaml
cp "$ROOT"/api/*.py "$BUILD"/
if [ -d "$ROOT/frontend/dist" ]; then
  cp -R "$ROOT/frontend/dist" "$BUILD/frontend_dist"
else
  echo "warning: frontend/dist not found, zip has no dashboard" >&2
fi
find "$BUILD" -name __pycache__ -prune -exec rm -rf {} +
(cd "$BUILD" && zip -q -r "$ROOT/dist/mimir-lambda.zip" .)
echo "built $ROOT/dist/mimir-lambda.zip ($(du -h "$ROOT/dist/mimir-lambda.zip" | cut -f1))"
