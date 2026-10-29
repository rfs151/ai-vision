#!/usr/bin/env bash
# Usage: scripts/run_worker.sh [stream_name]   live/<name> -> ai/<name>
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=${1:-test}
exec .venv/bin/python worker/worker.py --src "rtsp://127.0.0.1/live/$NAME" --dst "rtsp://127.0.0.1/ai/$NAME" "${@:2}"
