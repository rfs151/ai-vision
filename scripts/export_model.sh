#!/usr/bin/env bash
# Export YOLO to OpenVINO (one time). Uses a separate venv so the runtime stays torch-free.
# Usage: scripts/export_model.sh [yolo11n]
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL=${1:-yolo11n}
PIP_INDEX=${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}
TORCH_INDEX=${TORCH_INDEX:-https://mirrors.aliyun.com/pytorch-wheels/cpu/}

[ -d .venv-export ] || python3 -m venv .venv-export
# CPU-only torch, pinned "+cpu" so pip never picks the multi-GB CUDA build from PyPI
TORCH_VER=${TORCH_VER:-2.14.1}
TV_VER=${TV_VER:-0.29.1}
.venv-export/bin/pip install --timeout 120 -i "$PIP_INDEX" -f "$TORCH_INDEX" \
  "torch==${TORCH_VER}+cpu" "torchvision==${TV_VER}+cpu" -r requirements-export.txt openvino

# ultralytics may auto-install extra deps via pip; keep them on the same mirror
export PIP_INDEX_URL="$PIP_INDEX"
cd worker/models
../../.venv-export/bin/yolo export model="$MODEL.pt" format=openvino imgsz=640 half=False
ls -d "${MODEL}_openvino_model"
