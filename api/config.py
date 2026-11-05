"""Settings from environment variables (defaults work for a single-machine setup)."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _zlm_secret():
    """ZLM_SECRET env, else read [api] secret from ZLM's config.ini (ZLM_CONFIG)."""
    if os.getenv("ZLM_SECRET"):
        return os.environ["ZLM_SECRET"]
    cfg = Path(os.getenv("ZLM_CONFIG", "/opt/zlm/config.ini"))
    if cfg.exists():
        section = None
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if m := re.match(r"^\[(.+)\]$", line):
                section = m.group(1)
            elif section == "api" and line.startswith("secret="):
                return line.split("=", 1)[1].strip()
    return ""


class Settings:
    data_dir = Path(os.getenv("DATA_DIR", ROOT / "data"))
    db_url = os.getenv("DB_URL", f"sqlite:///{data_dir / 'ai-vision.db'}")
    api_url = os.getenv("API_URL", "http://127.0.0.1:8090")     # how workers reach this service
    zlm_api = os.getenv("ZLM_API", "http://127.0.0.1")           # ZLM http api
    zlm_rtsp = os.getenv("ZLM_RTSP", "rtsp://127.0.0.1")         # where workers pull/push
    zlm_secret = _zlm_secret()
    # Ports in the play URLs handed to browsers (ZLM's public http / rtsp ports)
    play_http_port = int(os.getenv("PLAY_HTTP_PORT", "80"))
    play_rtsp_port = int(os.getenv("PLAY_RTSP_PORT", "554"))
    # Streams published to these apps get a device record + AI worker automatically
    auto_apps = set(filter(None, os.getenv("AUTO_APPS", "live").split(",")))
    ai_app = os.getenv("AI_APP", "ai")                            # workers push annotated output here
    ai_ref_app = os.getenv("AI_REF_APP", "ai_ref")                # compare mode: baseline encode
    worker_python = os.getenv("WORKER_PYTHON", str(ROOT / ".venv" / "bin" / "python"))
    worker_script = str(ROOT / "worker" / "worker.py")
    worker_args = os.getenv("WORKER_ARGS", "")                    # extra args for every worker
    heartbeat_timeout = float(os.getenv("HEARTBEAT_TIMEOUT", "20"))
    max_workers = int(os.getenv("MAX_WORKERS", "8"))
    # Alarm snapshots + clips: delete files older than N days, and the oldest beyond N GB (0 = off)
    media_keep_days = float(os.getenv("MEDIA_KEEP_DAYS", "7"))
    media_max_gb = float(os.getenv("MEDIA_MAX_GB", "5"))
    # Test streams (scripts/streams.sh): the list, and the on/off choice made in the UI. The choice
    # lives under data/ so the demo container (Docker) sees it too.
    streams_conf = Path(os.getenv("STREAMS_CONF", ROOT / "scripts" / "streams.conf"))
    streams_selection = Path(os.getenv("STREAMS_SELECTION", data_dir / "streams.enabled"))


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
