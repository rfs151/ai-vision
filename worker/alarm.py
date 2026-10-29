"""Alarm sink: save snapshot + append JSON line. M3 will also POST to the control API."""
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import cv2

log = logging.getLogger("alarm")


class AlarmSink:
    def __init__(self, stream, out_dir):
        self.stream = stream
        self.dir = Path(out_dir) / stream
        (self.dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "events.jsonl"

    def emit(self, event, annotated_frame):
        """annotated_frame: the exact frame the event was detected on, already drawn."""
        rec = asdict(event)
        rec["stream"] = self.stream
        rec["wall_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if event.alarm:
            name = f"{time.strftime('%Y%m%d_%H%M%S')}_{event.type}_{event.rule_id}_{event.track_id}.jpg"
            path = self.dir / "snapshots" / name
            cv2.imwrite(str(path), annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            rec["snapshot"] = str(path)
            log.warning("ALARM %s rule=%s(%s) track=%s %s", event.type, event.rule_name,
                        event.rule_id, event.track_id, event.detail)
        else:
            log.info("event %s rule=%s track=%s %s", event.type, event.rule_name, event.track_id, event.detail)
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
