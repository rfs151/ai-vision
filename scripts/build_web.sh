#!/usr/bin/env bash
# Build the frontend into web/dist (served by the control service at http://<host>:8090/).
set -euo pipefail
cd "$(dirname "$0")/../web"
REGISTRY=${REGISTRY:-https://registry.npmmirror.com}
[ -d node_modules ] || npm install --registry "$REGISTRY" --no-audit --no-fund
npx vite build
