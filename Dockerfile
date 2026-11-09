# ai-vision app image: control service (API + web UI) + AI workers (started by the API) + ffmpeg.
# Build args (all optional; the defaults are the official sources, see .env.example for CN mirrors):
#   REGISTRY      prefix for base images, e.g. docker.1ms.run/library/
#   PIP_INDEX     PyPI index          NPM_REGISTRY  npm registry       APT_MIRROR  Debian mirror host
#   TORCH_INDEX   CPU torch wheels    MODEL_URL / POSE_MODEL_URL  yolo11n(-pose).pt (only when no exported
#                                     model is in the context)
ARG REGISTRY=

# ---- 1. web UI ----
FROM ${REGISTRY}node:20-slim AS web
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry="$NPM_REGISTRY" --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 2. OpenVINO models ----
# yolo11n (人车检测) and yolo11n-pose (姿态识别). Uses worker/models/<name>_openvino_model from the
# build context if present (local builds). Otherwise downloads the .pt and exports it here; torch
# stays in this stage only.
FROM ${REGISTRY}python:3.12-slim AS model
ARG PIP_INDEX=https://pypi.org/simple
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG MODEL_URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
ARG POSE_MODEL_URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt
COPY worker/models/ /models/
RUN set -e; cd /models; \
    if [ -f yolo11n_openvino_model/yolo11n.xml ] && [ -f yolo11n-pose_openvino_model/yolo11n-pose.xml ]; then \
        echo "using exported models from build context"; exit 0; fi; \
    apt-get update && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*; \
    # TORCH_INDEX may be an index (download.pytorch.org) or a links page (aliyun): pass it both ways.
    # CPU wheels are "+cpu" versions, which pip prefers over the multi-GB CUDA build on PyPI.
    pip install --no-cache-dir -i "$PIP_INDEX" --extra-index-url "$TORCH_INDEX" -f "$TORCH_INDEX" \
        torch torchvision ultralytics openvino; \
    for m in "yolo11n $MODEL_URL" "yolo11n-pose $POSE_MODEL_URL"; do \
        set -- $m; [ -f "$1_openvino_model/$1.xml" ] && continue; \
        [ -s "$1.pt" ] || curl -fL -o "$1.pt" "$2"; \
        PIP_INDEX_URL="$PIP_INDEX" yolo export model="$1.pt" format=openvino imgsz=640 half=False; \
    done

# ---- 3. runtime ----
FROM ${REGISTRY}python:3.12-slim
ARG PIP_INDEX=https://pypi.org/simple
ARG APT_MIRROR=
RUN set -e; \
    if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi; \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg curl tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX" -r requirements.txt
COPY api api
COPY worker worker
COPY scripts scripts
COPY rules rules
COPY --from=model /models/yolo11n_openvino_model worker/models/yolo11n_openvino_model
COPY --from=model /models/yolo11n-pose_openvino_model worker/models/yolo11n-pose_openvino_model
COPY --from=web /web/dist web/dist
# TZ: alarm times, file names and logs are local time (the UI shows them as they are)
ENV TZ=Asia/Shanghai \
    DATA_DIR=/app/data \
    WORKER_PYTHON=/usr/local/bin/python \
    PYTHONUNBUFFERED=1 \
    API_PORT=8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fs "http://127.0.0.1:${API_PORT}/api/system" > /dev/null || exit 1
# API_HOST=127.0.0.1 when a reverse proxy on the same host is the only way in
CMD ["sh", "-c", "exec uvicorn api.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT}"]
