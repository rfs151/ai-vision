#!/usr/bin/env python3
"""Resource report for a running system: per-process CPU / memory / disk writes + per-stream AI stats.

    python3 scripts/bench.py                    # 20 s sample, API at http://127.0.0.1:8090
    python3 scripts/bench.py -s 60 --api http://127.0.0.1:18090

Run it while streams are flowing (scripts/streams.sh start, or docker compose --profile demo).
Linux only (reads /proc); inside Docker run it on the host (host networking, same /proc view
of the container processes).

Verdict per stream: a worker keeps up while one detection takes less than 1/det_fps
(200 ms at 5 det/s) and the output fps matches the source.
"""
import argparse
import json
import os
import re
import time
import urllib.request

HZ = os.sysconf("SC_CLK_TCK")
PAT = re.compile(r"worker\.py|ffmpeg|MediaServer|uvicorn")


def label(cmd):
    if m := re.search(r"--name (\S+)", cmd):
        return "worker", m.group(1)
    m = re.search(r"rtsp://[^/\s]+/([^\s|]+)", cmd)
    if "rawvideo" in cmd:
        return "encoder", m.group(1) if m else "?"
    if "stream_loop" in cmd:
        return "push", m.group(1) if m else "?"
    if "MediaServer" in cmd:
        return "zlm", ""
    if "uvicorn" in cmd:
        return "api", ""
    return None, None


def snapshot():
    out = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
            if not PAT.search(cmd):
                continue
            kind, name = label(cmd)
            if not kind:
                continue
            st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            io = dict(l.split(": ") for l in open(f"/proc/{pid}/io").read().splitlines())
            pss = 0
            try:
                pss = int(open(f"/proc/{pid}/smaps_rollup").read().split("Pss:")[1].split()[0]) // 1024
            except Exception:
                pss = int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
            out[pid] = dict(kind=kind, name=name, cpu=int(st[11]) + int(st[12]),
                            wb=int(io.get("write_bytes", 0)), pss=pss)
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
            pass
    return out


def cpu_total():
    f = open("/proc/stat").readline().split()[1:]
    v = list(map(int, f))
    return sum(v), v[3] + v[4]       # total, idle+iowait


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception as e:
        print(f"  (API not reachable at {url}: {e})")
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--seconds", type=float, default=20)
    ap.add_argument("--api", default=os.getenv("API_URL", "http://127.0.0.1:8090"))
    args = ap.parse_args()
    os.environ.pop("http_proxy", None)
    os.environ.pop("HTTP_PROXY", None)

    ncpu = os.cpu_count()
    model = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "?")
    print(f"CPU: {model}, {ncpu} logical CPUs; sampling {args.seconds:.0f} s ...")
    a, ta = snapshot(), cpu_total()
    infer = {}
    t_end = time.time() + args.seconds
    while time.time() < t_end:           # average worker stats over the window (heartbeats every 5 s)
        for w in get(f"{args.api}/api/workers") or []:
            s = w.get("stats") or {}
            if w.get("state") == "running" and s.get("infer_ms"):
                infer.setdefault(w["stream"], []).append((s["infer_ms"], s.get("out_fps", 0), s.get("resolution")))
        time.sleep(min(5, max(0, t_end - time.time())))
    b, tb = snapshot(), cpu_total()
    T = args.seconds

    groups = {}
    for pid, y in b.items():
        x = a.get(pid)
        if not x:
            continue
        g = groups.setdefault((y["kind"], y["name"]), [0.0, 0, 0.0])
        g[0] += (y["cpu"] - x["cpu"]) / HZ / T * 100
        g[1] += y["pss"]
        g[2] += (y["wb"] - x["wb"]) / T / 1024

    print(f"\n{'process':24}{'CPU %':>8}{'mem MB':>9}{'disk wr KB/s':>14}")
    tot = [0.0, 0, 0.0]
    order = {"zlm": 0, "api": 1, "worker": 2, "encoder": 3, "push": 4}
    for (kind, name), (c, m, w) in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
        print(f"{(kind + ' ' + name).strip():24}{c:8.1f}{m:9d}{w:14.1f}")
        tot[0] += c; tot[1] += m; tot[2] += w
    busy = 1 - (tb[1] - ta[1]) / max(1, tb[0] - ta[0])
    print(f"{'TOTAL':24}{tot[0]:8.1f}{tot[1]:9d}{tot[2]:14.1f}")
    print(f"machine CPU busy: {busy * 100:.0f}% of {ncpu} CPUs (includes everything else running)")

    if infer:
        print(f"\n{'stream':12}{'resolution':>12}{'infer ms':>10}{'out fps':>9}  verdict")
        for s, v in sorted(infer.items()):
            ms = sum(x[0] for x in v) / len(v)
            fps = sum(x[1] for x in v) / len(v)
            ok = "ok" if ms < 200 else "TOO SLOW for 5 det/s"
            print(f"{s:12}{str(v[-1][2]):>12}{ms:10.1f}{fps:9.1f}  {ok}")
        per = [c for (k, _), (c, _, _) in groups.items() if k in ("worker", "encoder")]
        n = len(infer)
        if n:
            per_stream = sum(per) / n / 100       # CPUs per stream (worker + encoder)
            fixed = sum(c for (k, _), (c, _, _) in groups.items() if k in ("zlm", "api")) / 100
            cap = int((ncpu * 0.8 - fixed) / per_stream) if per_stream else 0
            print(f"\n{n} AI streams: {per_stream:.2f} CPU per stream (worker + encoder), "
                  f"~{cap} streams at 80% of {ncpu} CPUs")
            print("(rough: per-stream cost rises as the machine fills up, turbo clocks drop and "
                  "hyper-threads share cores; measure with the target number of streams)")


if __name__ == "__main__":
    main()
