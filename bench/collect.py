"""Turn one run folder into summary.json (schema: brief section 11.6).

    python bench/collect.py <run_dir> --exp exp1 --system own --algorithm round_robin \
        --concurrency 200 --rtt-ms 0 --run 1

Reads, from <run_dir>: hey_c*.csv, t0.txt, kill_ts.txt, stats_s0/s30/s60 (.json for the
own LB, .csv for HAProxy), lb.jsonl or haproxy.log, cpu.csv, cpu_clients.csv, ulimit.txt.
Standard library only. Exit code: 0 for a valid run, 3 for an invalid one.

Time: every timestamp is milliseconds since the epoch from the same host clock.
Windows: pre = offset in [0, 30) s, post = [30, 60) s (halves of --duration), offsets are hey's, relative to its start.
A request counts as failed when its status is >= 500. `hey -o csv` writes only the
requests that returned a response, so transport errors do not appear in the CSV; a proxy
that cannot reach a backend answers 502/503/504, which is what the failover window shows.
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

DURATION_S = 60.0   # hey -z; the kill happens at half of it (a shorter value is for dry runs)
KILL_TOLERANCE_MS = 500
CLIENT_CPU_PCT = 90.0   # of the client CPU limit ...
CLIENT_CPU_SECONDS = 5  # ... for more than this many consecutive samples
MIN_NOFILE = 65535
MAX_DETECTION_S = 10.0
KILLED = "b3"


def percentile(sorted_vals, p):
    """Nearest-rank percentile of an ascending list (same rule as lb/obs/metrics.py)."""
    if not sorted_vals:
        return 0.0
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


def read_hey_csv(path):
    """Return [(offset_s, response_ms, status)] from one hey CSV, columns found by name."""
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names = {n.strip().lower(): n for n in (reader.fieldnames or [])}
        try:
            t_col, s_col, o_col = (names[k] for k in ("response-time", "status-code", "offset"))
        except KeyError as exc:
            raise ValueError("%s: column %s missing (found %s)" % (path, exc, list(names)))
        for r in reader:
            try:
                rows.append((float(r[o_col]), float(r[t_col]) * 1000.0, int(float(r[s_col]))))
            except (TypeError, ValueError):
                continue  # a truncated last line
    return rows


def window_stats(rows, lo, hi):
    """Counts and nearest-rank percentiles for rows with lo <= offset < hi."""
    sel = [r for r in rows if lo <= r[0] < hi]
    lat = sorted(r[1] for r in sel)
    return {
        "rps": round(len(sel) / (hi - lo), 2),
        "p50_ms": round(percentile(lat, 50), 3),
        "p95_ms": round(percentile(lat, 95), 3),
        "p99_ms": round(percentile(lat, 99), 3),
        "requests": len(sel),
        "failed": sum(1 for r in sel if r[2] >= 500),
    }


def iso_to_ms(ts):
    """'2026-10-13T17:50:27.560Z' (any fraction length, also docker's 9 digits) -> epoch ms."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", ts)
    if not m:
        raise ValueError("bad timestamp %r" % ts)
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(base.timestamp()) * 1000 + int((m.group(2) or "0").ljust(3, "0")[:3])


def first_down_ms(run_dir, kill_ms):
    """Epoch ms of the first KILLED-is-down event at or after the kill, or None."""
    own = os.path.join(run_dir, "lb.jsonl")
    ha = os.path.join(run_dir, "haproxy.log")
    if os.path.exists(own):
        with open(own, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                    if ev.get("event") == "backend_down" and ev.get("backend") == KILLED:
                        ms = iso_to_ms(ev["ts"])
                        if ms >= kill_ms:
                            return ms
                except (ValueError, KeyError):
                    continue
    elif os.path.exists(ha):
        pat = re.compile(r"Server \S+/%s is DOWN" % KILLED)
        with open(ha, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if pat.search(line):
                    try:
                        ms = iso_to_ms(line.split(None, 1)[0])  # `docker logs -t` prefix
                    except ValueError:
                        continue
                    if ms >= kill_ms:
                        return ms
    return None


def backend_requests(run_dir, snap):
    """{backend: total requests} from stats_<snap>.json (own LB) or .csv (HAProxy)."""
    js = os.path.join(run_dir, "stats_%s.json" % snap)
    cs = os.path.join(run_dir, "stats_%s.csv" % snap)
    if os.path.exists(js):
        with open(js, encoding="utf-8") as fh:
            return {b: int(v["requests"]) for b, v in json.load(fh)["per_backend"].items()}
    if os.path.exists(cs):
        with open(cs, newline="", encoding="utf-8") as fh:
            text = fh.read().lstrip("# ")  # the header line starts with "# pxname"
        out = {}
        for r in csv.DictReader(text.splitlines()):
            if r.get("pxname") == "be" and re.fullmatch(r"b\d+", r.get("svname", "")):
                out[r["svname"]] = int(r["stot"] or 0)
        return out
    raise FileNotFoundError("stats_%s.json/.csv" % snap)


def diff(after, before):
    return {b: after.get(b, 0) - before.get(b, 0) for b in sorted(after)}


def read_columns(path):
    """CSV -> {column: [float, ...]}; empty dict if the file is missing."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as fh:
        cols = {}
        for r in csv.DictReader(fh):
            for k, v in r.items():
                cols.setdefault(k, []).append(float(v))
    return cols


def mean(vals):
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def cpu_summary(run_dir, kill_at_s):
    c = read_columns(os.path.join(run_dir, "cpu.csv"))
    t, v = c.get("t_s", []), c.get("cpu_pct", [])
    pre = [x for x, s in zip(v, t) if s <= kill_at_s]
    post = [x for x, s in zip(v, t) if s > kill_at_s]
    return {"pre_mean": mean(pre), "post_mean": mean(post), "peak": round(max(v, default=0.0), 1)}


def client_overloaded(run_dir, limit):
    """True if any client CPU exceeded 90 % of its limit for more than 5 consecutive samples."""
    cols = read_columns(os.path.join(run_dir, "cpu_clients.csv"))
    cols.pop("t_s", None)
    for vals in cols.values():
        run = 0
        for x in vals:
            run = run + 1 if x > CLIENT_CPU_PCT * limit else 0
            if run > CLIENT_CPU_SECONDS:
                return True
    return False


def read_number(path):
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def build_summary(run_dir, exp, system, algorithm, concurrency, rtt_ms, run,
                  client_cpu_limit=1.0, duration=DURATION_S):
    half = duration / 2
    problems = []
    t0_ms = read_number(os.path.join(run_dir, "t0.txt"))
    kill_ms = read_number(os.path.join(run_dir, "kill_ts.txt"))
    if t0_ms is None:
        problems.append("t0.txt missing")
    if kill_ms is None:
        problems.append("kill_ts.txt missing")

    rows = []
    csvs = sorted(glob.glob(os.path.join(run_dir, "hey_c*.csv")))
    if len(csvs) != 4:
        problems.append("expected 4 hey csv files, found %d" % len(csvs))
    for p in csvs:
        try:
            rows.extend(read_hey_csv(p))
        except (OSError, ValueError) as exc:
            problems.append("unreadable %s: %s" % (os.path.basename(p), exc))
    windows = {"pre": window_stats(rows, 0.0, half), "post": window_stats(rows, half, duration)}
    if any(w["requests"] == 0 for w in windows.values()):
        problems.append("no requests in a window")
    if windows["pre"]["failed"]:
        problems.append("%d failed requests before the kill" % windows["pre"]["failed"])

    down_ms = detection_s = failed_detect = None
    if kill_ms is not None:
        if t0_ms is not None and abs(kill_ms - t0_ms - half * 1000) > KILL_TOLERANCE_MS:
            problems.append("kill at t0+%.2f s, not t0+%g s" % ((kill_ms - t0_ms) / 1000.0, half))
        down_ms = first_down_ms(run_dir, kill_ms)
        if down_ms is None:
            problems.append("no backend_down/DOWN event for %s after the kill" % KILLED)
        else:
            detection_s = round((down_ms - kill_ms) / 1000.0, 3)
            if not 0 < detection_s <= MAX_DETECTION_S:
                problems.append("detection_s %.3f outside (0, 10]" % detection_s)
            if t0_ms is not None:
                failed_detect = sum(
                    1 for off, _, st in rows
                    if st >= 500 and kill_ms <= t0_ms + off * 1000.0 <= down_ms)

    try:
        s0, s30, s60 = (backend_requests(run_dir, s) for s in ("s0", "s30", "s60"))
        per_pre, per_post = diff(s30, s0), diff(s60, s30)
    except (OSError, ValueError, KeyError) as exc:
        problems.append("stats snapshots unreadable: %s" % exc)
        per_pre = per_post = {}

    if client_overloaded(run_dir, client_cpu_limit):
        problems.append("a client exceeded %d%% CPU for more than %d s"
                        % (CLIENT_CPU_PCT, CLIENT_CPU_SECONDS))
    nofile = read_number(os.path.join(run_dir, "ulimit.txt"))
    if nofile is None or nofile < MIN_NOFILE:
        problems.append("ulimit -n is %s, need >= %d" % (nofile, MIN_NOFILE))

    return {
        "exp": exp, "system": system, "algorithm": algorithm, "concurrency": concurrency,
        "rtt_ms": rtt_ms, "run": run,
        "t0_ms": t0_ms, "kill_ms": kill_ms, "down_ms": down_ms, "detection_s": detection_s,
        "windows": windows,
        "failed_in_detection_window": failed_detect,
        "per_backend_pre": per_pre, "per_backend_post": per_post,
        "cpu_pct": cpu_summary(run_dir, half),
        "valid": not problems, "invalid_reason": "; ".join(problems) or None,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--exp", required=True)
    ap.add_argument("--system", required=True, choices=("own", "haproxy"))
    ap.add_argument("--algorithm", required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--rtt-ms", type=float, required=True)
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--client-cpu-limit", type=float, default=1.0,
                    help="CPU limit of one client container, in cores")
    ap.add_argument("--duration", type=float, default=DURATION_S,
                    help="length of the hey run in seconds (60 for real runs)")
    a = ap.parse_args(argv)
    rtt = int(a.rtt_ms) if a.rtt_ms.is_integer() else a.rtt_ms
    summary = build_summary(a.run_dir, a.exp, a.system, a.algorithm, a.concurrency,
                            rtt, a.run, a.client_cpu_limit, a.duration)
    with open(os.path.join(a.run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    print("valid" if summary["valid"] else "INVALID: " + summary["invalid_reason"])
    return 0 if summary["valid"] else 3


if __name__ == "__main__":
    sys.exit(main())
