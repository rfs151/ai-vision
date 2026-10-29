#!/usr/bin/env bash
# Control service on :8090. Env: ZLM_API, ZLM_SECRET / ZLM_CONFIG, AUTO_APPS, MAX_WORKERS ... (see api/config.py)
set -euo pipefail
cd "$(dirname "$0")/.."
# Local calls (ZLM, workers) must never go through an http proxy
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
exec .venv/bin/uvicorn api.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8090}" "$@"
