"""Alarm clips: the annotated video from PRE seconds before an alarm to POST seconds after.

The output encoder also writes its H.264 to short MPEG-TS segments (ffmpeg tee + segment muxer),
named by their start time in epoch seconds, in a RAM ring directory (/dev/shm). Old segments are
deleted. When an alarm fires, a job waits until POST seconds have passed, then the covering
segments are joined into an MP4 without re-encoding (a few ms of CPU).

Alarms close together share one clip: a new alarm inside a pending job's window extends that job
(up to MAX_LEN seconds) instead of cutting an almost identical second clip.
"""
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("clip")


class ClipJob:
    def __init__(self, start, end):
        self.start = start          # wall clock (epoch s)
        self.end = end
        self.expected = 0           # alarms that will report an id
        self.alarm_ids = []
        self.cond = threading.Condition()

    def expect(self):
        with self.cond:
            self.expected += 1

    def add_id(self, alarm_id):
        """Called by the uploader once the API has assigned the alarm id."""
        with self.cond:
            self.alarm_ids.append(alarm_id)
            self.cond.notify_all()

    def give_up(self):
        """Alarm upload failed for good: stop waiting for its id."""
        with self.cond:
            self.expected -= 1
            self.cond.notify_all()

    def wait_ids(self, timeout):
        with self.cond:
            self.cond.wait_for(lambda: len(self.alarm_ids) >= self.expected, timeout)
            return list(self.alarm_ids)


class ClipRecorder(threading.Thread):
    MAX_LEN = 40.0

    def __init__(self, ring_dir, on_clip, pre=10.0, post=10.0, seg_sec=2, keep_sec=60):
        super().__init__(daemon=True, name="clips")
        self.dir = Path(ring_dir)
        shutil.rmtree(self.dir, ignore_errors=True)       # leftovers of a previous run
        (self.dir / "clips").mkdir(parents=True, exist_ok=True)
        self.on_clip = on_clip      # on_clip(job, mp4_path); the file is deleted after it returns
        self.pre, self.post, self.seg_sec, self.keep_sec = pre, post, seg_sec, keep_sec
        self.jobs = []
        self.lock = threading.Lock()

    # ---- used by the encoder ----
    def tee_output(self):
        """Second output for ffmpeg's tee muxer (the first one is the RTSP push)."""
        return (f"[f=segment:segment_time={self.seg_sec}:segment_format=mpegts:strftime=1]"
                f"{self.dir}/%s.ts")

    # ---- used by the analysis thread ----
    def request(self, t=None):
        """Returns the job whose clip will cover an alarm at wall time t."""
        t = time.time() if t is None else t
        with self.lock:
            for job in self.jobs:
                if job.start <= t <= job.end:
                    job.end = min(max(job.end, t + self.post), job.start + self.MAX_LEN)
                    return job
            job = ClipJob(t - self.pre, t + self.post)
            self.jobs.append(job)
            return job

    # ---- background ----
    def segments(self):
        """[(start_epoch, path)] sorted; the newest one may still be being written."""
        out = []
        for p in self.dir.glob("*.ts"):
            try:
                out.append((int(p.stem), p))
            except ValueError:
                pass
        return sorted(out)

    def cut(self, job):
        """Join the segments overlapping [job.start, job.end] into an MP4. None if nothing is there."""
        segs = self.segments()
        picked = [p for i, (s, p) in enumerate(segs)
                  if s < job.end and (segs[i + 1][0] if i + 1 < len(segs) else s + self.seg_sec) > job.start]
        if not picked:
            return None
        lst = self.dir / "clips" / f"{int(job.start)}.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in picked))
        mp4 = lst.with_suffix(".mp4")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", "-movflags", "+faststart", str(mp4)], capture_output=True, text=True)
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            log.error("clip failed: %s", r.stderr.strip()[-300:])
            return None
        return mp4

    def cleanup(self, now):
        with self.lock:
            oldest_needed = min((j.start for j in self.jobs), default=now) - self.seg_sec
        limit = min(now - self.keep_sec, oldest_needed)
        for s, p in self.segments()[:-1]:        # never the one being written
            if s < limit:
                p.unlink(missing_ok=True)

    def run(self):
        while True:
            time.sleep(0.5)
            now = time.time()
            # Ready once the segment containing job.end is closed (+1 s margin for the muxer)
            with self.lock:
                ready = [j for j in self.jobs if now >= j.end + self.seg_sec + 1]
            for job in ready:
                try:
                    mp4 = self.cut(job)
                    if mp4:
                        log.info("clip %.0fs (%s .. %s) for %d alarm(s)", job.end - job.start,
                                 time.strftime("%H:%M:%S", time.localtime(job.start)),
                                 time.strftime("%H:%M:%S", time.localtime(job.end)), job.expected)
                        try:
                            self.on_clip(job, mp4)
                        finally:
                            mp4.unlink(missing_ok=True)
                    else:
                        log.warning("no segments for clip at %s", time.strftime("%H:%M:%S"))
                except Exception:
                    log.exception("clip job failed")
                with self.lock:
                    self.jobs.remove(job)
            self.cleanup(now)
