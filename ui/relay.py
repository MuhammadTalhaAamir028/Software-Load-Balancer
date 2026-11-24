#!/usr/bin/env python3
"""Localhost-only JSON relay for the Project 5 UI.

Run it inside WSL/Linux next to the lab:  bash ui/start.sh   (or: python3 ui/relay.py)

The browser cannot reach the container IPs (10.0.x.x) from Windows, so this small server
runs a FIXED set of read commands (docker exec, curl, tshark) and a FIXED set of demo
controls, and returns the answers as JSON. It accepts no command text from the browser.
Standard library only.
"""

from __future__ import annotations

import collections
import csv
import datetime
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
STATIC = REPO / "ui" / "static"
UI_RUN = REPO / "results" / "ui"            # files the UI creates (ignored by git)
KILL_TS_FILE = UI_RUN / "kill_ts.txt"
PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}

PREFIX = "clab-lb-"
LB_IP, LB_PORT, LB_STATS_PORT = "10.0.2.10", 8080, 9090
HAPROXY_IP, HAPROXY_STATS_PORT = "10.0.2.11", 8404
BACKEND_PORT = 8000
CLIENT = "c1"                      # every in-lab request/measurement is made from this client
DEMO_UA = "project5-ui-demo"       # marks the packets of the capture demo
RTT_VALUES = {"0", "20", "100", "200"}
ALGORITHMS = {"round_robin", "least_conn", "ip_hash"}
ACTIVE_SYSTEMS = {"own", "haproxy", "both"}     # "both" is for the live side-by-side demo only, never for experiments
HAPROXY_BALANCE = {"roundrobin": "round_robin", "leastconn": "least_conn", "source": "ip_hash"}

NODE_INFO = {
    "c1": ("10.0.1.11", "client"), "c2": ("10.0.1.12", "client"),
    "c3": ("10.0.1.13", "client"), "c4": ("10.0.1.14", "client"),
    "sw-clients": ("bridge 10.0.1.0/24", "bridge"),
    "r1": ("10.0.1.1 / 10.0.2.1", "router"),
    "sw-lb": ("bridge 10.0.2.0/24", "bridge"),
    "lb": (LB_IP, "load balancer"), "haproxy": (HAPROXY_IP, "reference LB"),
    "r2": ("10.0.2.2 / 10.0.3.1", "router"),
    "sw-backends": ("bridge 10.0.3.0/24", "bridge"),
    "b1": ("10.0.3.11", "backend"), "b2": ("10.0.3.12", "backend"),
    "b3": ("10.0.3.13", "backend"), "b4": ("10.0.3.14", "backend"),
}
# The real wiring (topo/clab.yml): every subnet is a Linux bridge container.
LINKS = [
    ["c1", "sw-clients"], ["c2", "sw-clients"], ["c3", "sw-clients"], ["c4", "sw-clients"],
    ["sw-clients", "r1"], ["r1", "sw-lb"], ["sw-lb", "lb"], ["sw-lb", "haproxy"],
    ["sw-lb", "r2"], ["r2", "sw-backends"],
    ["sw-backends", "b1"], ["sw-backends", "b2"], ["sw-backends", "b3"], ["sw-backends", "b4"],
]
# The lb, haproxy and backend containers share the network of their "-net" holder container,
# and only the holder image has the `ip` tool.
ROUTE_NODES = {"r1": "r1", "r2": "r2", "lb": "lb-net", "c1": "c1", "b1": "b1-net"}


# --------------------------------------------------------------------------- helpers

def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(args: list[str], timeout: float = 15, env_extra: dict | None = None) -> dict:
    """Run one fixed command. Never raises; returns ok, returncode, stdout, stderr."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "timestamp": utc_now()}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc), "timestamp": utc_now()}


def docker_exec(node: str, *command: str, timeout: float = 15) -> dict:
    return run(["docker", "exec", PREFIX + node, *command], timeout=timeout)


_cache: dict = {}
_cache_lock = threading.Lock()


def cached(key: str, ttl: float, producer):
    """Return a recent answer for `key`, so several panels asking at once cost one command."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


_ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z?$")


def iso_to_ms(text: str):
    """'2026-11-21T16:10:51.354123456Z' -> epoch milliseconds, or None."""
    match = _ISO.match((text or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    millis = int((match.group(7) or "0")[:3].ljust(3, "0"))
    base = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    return int(base.timestamp()) * 1000 + millis


def tail_text(path: pathlib.Path, max_bytes: int = 65536) -> str:
    """Last max_bytes of a text file without reading all of it (the LB log gets very large)."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]          # the first line is probably cut in half
    return text


def check_request(method: str, headers) -> tuple:
    """Return (ok, reason). Blocks DNS-rebinding (Host) and cross-site POSTs (Origin, header)."""
    if headers.get("Host", "") not in ALLOWED_HOSTS:
        return False, "unexpected Host header"
    if method == "POST":
        origin = headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False, "cross-origin request refused"
        if headers.get("X-UI-Control") != "1":
            return False, "missing X-UI-Control header"
    return True, ""


# --------------------------------------------------------------------------- parsers

def parse_netem_state(text: str) -> dict:
    """topo/.netem_state is ONE line: 'RTT_MS=100 LOSS_PCT=0'."""
    out = {"RTT_MS": 0.0, "LOSS_PCT": 0.0}
    for token in (text or "").split():
        key, _, value = token.partition("=")
        if key in out:
            try:
                out[key] = float(value)
            except ValueError:
                pass
    return out


def parse_ping(text: str) -> dict:
    avg = re.search(r"= [\d.]+/([\d.]+)/", text or "")
    loss = re.search(r"([\d.]+)% packet loss", text or "")
    return {"rtt_avg_ms": float(avg.group(1)) if avg else None,
            "loss_pct": float(loss.group(1)) if loss else None}


def cpu_limit(nano_cpus: int, quota: int, period: int):
    """containerlab sets CpuQuota/CpuPeriod, not NanoCpus. Returns CPUs or the string 'unlimited'."""
    if nano_cpus > 0:
        return round(nano_cpus / 1e9, 3)
    if quota > 0 and period > 0:
        return round(quota / period, 3)
    return "unlimited"


def parse_haproxy_csv(text: str) -> list:
    header = next((line for line in (text or "").splitlines() if line.startswith("# ")), "")
    if not header:
        return []
    fields = [item.strip() for item in header[2:].split(",")]
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        values = next(csv.reader([line]))
        if len(values) >= len(fields):
            rows.append(dict(zip(fields, values)))
    return rows


_HAPROXY_EVENT = re.compile(r"^(\S+)\s+.*?Server (\S+)/(\S+) is (DOWN|UP)(?:, reason: ([^,]+))?")


def parse_haproxy_events(text: str) -> list:
    events, seen = [], set()
    for line in (text or "").splitlines():
        match = _HAPROXY_EVENT.match(line)
        if not match:
            continue
        ts_ms = iso_to_ms(match.group(1))
        key = (match.group(3), match.group(4), (ts_ms or 0) // 1000)
        if ts_ms is None or key in seen:          # HAProxy prints every event twice
            continue
        seen.add(key)
        events.append({"system": "haproxy", "ts_ms": ts_ms, "backend": match.group(3),
                       "state": match.group(4), "reason": (match.group(5) or "").strip()})
    return events


def parse_own_events(text: str) -> list:
    events = []
    for line in (text or "").splitlines():
        if '"backend_down"' not in line and '"backend_up"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_ms = iso_to_ms(record.get("ts", ""))
        if ts_ms is None:
            continue
        events.append({"system": "own", "ts_ms": ts_ms, "backend": record.get("backend"),
                       "state": "DOWN" if record["event"] == "backend_down" else "UP",
                       "reason": record.get("reason", "")})
    return events


# ------------------------------------------------------------- packet capture, annotated

TSHARK_FIELDS = ["frame.number", "frame.time_relative", "ip.src", "ip.dst", "tcp.srcport",
                 "tcp.dstport", "tcp.flags", "tcp.stream", "http.request.method",
                 "http.request.uri", "http.response.code", "http.user_agent",
                 "http.x_forwarded_for", "http.connection"]


def parse_tshark(text: str) -> list:
    rows = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        parts += [""] * (len(TSHARK_FIELDS) - len(parts))
        rows.append(dict(zip(TSHARK_FIELDS, parts)))
    return rows


def flag_names(hex_text: str) -> set:
    try:
        value = int(hex_text, 16)
    except (TypeError, ValueError):
        return set()
    return {name for bit, name in ((1, "FIN"), (2, "SYN"), (4, "RST"), (8, "PSH"), (16, "ACK")) if value & bit}


def _packet_label(row: dict, flags: set, handshake_done: bool):
    """(label, explanation) for the packets worth showing, else None."""
    if flags == {"SYN"}:
        return "SYN", "opens a TCP connection"
    if flags == {"SYN", "ACK"}:
        return "SYN-ACK", "the server accepts"
    if "RST" in flags:
        return "RST", "connection refused or reset"
    if "FIN" in flags:
        return "FIN", "connection closed"
    if row["http.request.method"]:
        extra = []
        if row["http.x_forwarded_for"]:
            extra.append("X-Forwarded-For: " + row["http.x_forwarded_for"])
        if row["http.connection"]:
            extra.append("Connection: " + row["http.connection"])
        return f"HTTP {row['http.request.method']} {row['http.request.uri']}", "; ".join(extra)
    if row["http.response.code"]:
        return f"HTTP {row['http.response.code']}", "response"
    if flags == {"ACK"} and not handshake_done:
        return "ACK", "handshake complete"
    return None


def annotate_packets(rows: list, demo_ua: str = DEMO_UA) -> dict:
    """Turn decoded packets into labelled events and plain-English notes."""
    streams: dict = {}
    for row in rows:
        flags = flag_names(row["tcp.flags"])
        ports = (row["tcp.srcport"], row["tcp.dstport"])
        leg = "client-LB" if str(LB_PORT) in ports else "LB-backend" if str(BACKEND_PORT) in ports else "other"
        stream = streams.setdefault(row["tcp.stream"], {
            "leg": leg, "rows": [], "syn": 0, "fin": 0, "rst": 0, "requests": 0, "responses": 0,
            "uris": [], "ua": "", "xff": "", "req_connection": "", "client": "", "server": "", "t0": None})
        try:
            if stream["t0"] is None:
                stream["t0"] = float(row["frame.time_relative"])
        except ValueError:
            pass
        stream["rows"].append((row, flags))
        if flags == {"SYN"}:
            stream["syn"] += 1
            stream["client"], stream["server"] = row["ip.src"], row["ip.dst"]
        stream["fin"] += "FIN" in flags
        stream["rst"] += "RST" in flags
        if row["http.request.method"]:
            stream["requests"] += 1
            stream["uris"].append(row["http.request.uri"])
            stream["ua"] = stream["ua"] or row["http.user_agent"]
            stream["xff"] = stream["xff"] or row["http.x_forwarded_for"]
            stream["req_connection"] = stream["req_connection"] or row["http.connection"]
        if row["http.response.code"]:
            stream["responses"] += 1

    def kind(s):
        if "/health" in s["uris"]:
            return "probe"
        if s["ua"] == demo_ua and s["leg"] == "client-LB":
            return "demo-client"
        if s["ua"] == demo_ua and s["leg"] == "LB-backend":
            return "demo-forward"
        if s["leg"] == "LB-backend" and s["requests"] == 0 and s["syn"] and s["rst"]:
            return "refused"
        return "other"

    for s in streams.values():
        s["kind"] = kind(s)

    def of(name):
        return [s for s in streams.values() if s["kind"] == name]

    demo_client, demo_forward, probes, refused = of("demo-client"), of("demo-forward"), of("probe"), of("refused")
    other_streams = of("other")

    probe_backends: dict = {}
    for s in sorted(probes, key=lambda item: item["t0"] or 0):
        entry = probe_backends.setdefault(s["server"] or "?", {"count": 0, "times": []})
        entry["count"] += 1
        entry["times"].append(s["t0"] or 0)
    for entry in probe_backends.values():
        gaps = [b - a for a, b in zip(entry["times"], entry["times"][1:])]
        entry["interval_s"] = round(sum(gaps) / len(gaps), 2) if gaps else None
        del entry["times"]
    refused_backends: dict = {}
    for s in refused:
        refused_backends[s["server"] or "?"] = refused_backends.get(s["server"] or "?", 0) + 1

    events = []
    for s in sorted(demo_client + demo_forward, key=lambda item: item["t0"] or 0):
        done = False
        for row, flags in s["rows"]:
            labelled = _packet_label(row, flags, done)
            if labelled is None:
                continue
            if labelled[0] == "ACK":
                done = True
            events.append({"t": row["frame.time_relative"], "leg": s["leg"], "src": row["ip.src"],
                           "dst": row["ip.dst"], "label": labelled[0], "detail": labelled[1]})
    notes = []
    if demo_client:
        requests = sum(s["requests"] for s in demo_client)
        syn = sum(s["syn"] for s in demo_client)
        notes.append(f"Client to LB: {requests} HTTP requests used {len(demo_client)} TCP connection "
                     f"({syn} SYN). The client leg is persistent (HTTP keep-alive).")
    else:
        notes.append("The demo exchange was not found in the capture. Is the own LB running and answering?")
    if demo_forward:
        xff = next((s["xff"] for s in demo_forward if s["xff"]), "")
        conn = next((s["req_connection"] for s in demo_forward if s["req_connection"]), "")
        notes.append(f"LB to backend: {len(demo_forward)} forwarded requests, each on its own TCP connection "
                     f"(Connection: {conn or 'n/a'}), X-Forwarded-For: {xff or 'n/a'}.")
    if probes:
        parts = ", ".join(f"{ip} every {v['interval_s']} s" if v["interval_s"] else f"{ip} once"
                          for ip, v in sorted(probe_backends.items()))
        notes.append(f"Health probes (GET /health): {len(probes)} in the capture ({parts}).")
    if refused:
        notes.append(f"{len(refused)} connections to a backend were answered with RST (refused). "
                     "This is how a killed backend looks to the load balancer.")
    if other_streams:
        notes.append(f"{len(other_streams)} other TCP streams (background load) are counted, not listed.")
    return {
        "summary": {
            "packets": len(rows), "streams": len(streams),
            "demo_client": {"connections": len(demo_client), "syn": sum(s["syn"] for s in demo_client),
                            "requests": sum(s["requests"] for s in demo_client),
                            "responses": sum(s["responses"] for s in demo_client)},
            "demo_forward": [{"backend": s["server"], "x_forwarded_for": s["xff"],
                              "connection": s["req_connection"]} for s in demo_forward],
            "probes": {"count": len(probes), "backends": probe_backends},
            "refused": {"count": len(refused), "backends": refused_backends},
            "other_streams": len(other_streams),
        },
        "events": events[:100],
        "notes": notes,
    }


def packet_capture() -> dict:
    """Capture on the LB port, send 3 marked requests on one connection, decode and label."""
    pcap = REPO / "captures" / "ui_live.pcap"
    if not shutil.which("tshark"):
        return {"ok": False, "error": "tshark is not installed in this Linux", "timestamp": utc_now()}
    pcap.unlink(missing_ok=True)
    capture = subprocess.Popen(
        ["bash", str(REPO / "topo" / "capture.sh"), "ui_live", "lb", "eth1",
         f"tcp port {LB_PORT} or tcp port {BACKEND_PORT}", "6"],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2.5)                                    # tcpdump needs about 1-2 s to start
    url = f"http://{LB_IP}:{LB_PORT}/"
    demo = docker_exec(CLIENT, "curl", "-s", "-o", "/dev/null", "-m", "3", "-A", DEMO_UA, url, url, url)
    try:
        capture.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        capture.kill()
        return {"ok": False, "error": "capture timed out", "timestamp": utc_now()}
    if not pcap.exists():
        return {"ok": False, "error": "no capture file was written", "timestamp": utc_now()}
    fields = [item for name in TSHARK_FIELDS for item in ("-e", name)]
    decoded = run(["tshark", "-n", "-r", str(pcap), "-Y", "tcp", "-T", "fields",
                   "-E", "header=n", "-E", "separator=/t", *fields], timeout=40)
    if not decoded["ok"]:
        return {"ok": False, "error": "tshark could not decode the capture: " + decoded["stderr"],
                "timestamp": utc_now()}
    result = annotate_packets(parse_tshark(decoded["stdout"]))
    result.update({"ok": True, "demo_requests_ok": demo["ok"], "pcap": "/api/captures/ui_live.pcap",
                   "timestamp": utc_now()})
    return result


# --------------------------------------------------------------------------- live data

def topology() -> dict:
    ps = run(["docker", "ps", "-a", "--filter", "name=" + PREFIX, "--format", "{{.Names}} {{.State}}"])
    states = dict(line.split(" ", 1) for line in ps["stdout"].splitlines() if " " in line)
    nodes = [{"id": node, "address": address, "kind": kind,
              "running": states.get(PREFIX + node) == "running"} for node, (address, kind) in NODE_INFO.items()]
    return {"ok": ps["ok"], "error": ps["stderr"], "nodes": nodes, "links": LINKS, "timestamp": utc_now()}


def route_data() -> dict:
    with ThreadPoolExecutor(6) as pool:
        futures = {name: pool.submit(docker_exec, holder, "ip", "route") for name, holder in ROUTE_NODES.items()}
        trace = pool.submit(docker_exec, CLIENT, "traceroute", "-n", "-w", "2", "-q", "1", "10.0.3.11", timeout=25)
        routes = {name: future.result() for name, future in futures.items()}
        traceroute = trace.result()
    hops = []
    for line in traceroute["stdout"].splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            hops.append({"hop": int(parts[0]), "ip": parts[1], "ms": parts[2] if len(parts) > 2 else ""})
    ok = all(item["ok"] for item in routes.values()) and traceroute["ok"]
    return {"ok": ok, "routes": routes, "traceroute": traceroute, "hops": hops, "timestamp": utc_now()}


def netem_data() -> dict:
    with ThreadPoolExecutor(2) as pool:
        qdisc = pool.submit(docker_exec, "r1", "tc", "qdisc", "show")
        ping = pool.submit(docker_exec, CLIENT, "ping", "-c", "3", "-i", "0.2", LB_IP, timeout=20)
        qdisc, ping = qdisc.result(), ping.result()
    state_path = REPO / "topo" / ".netem_state"
    configured = parse_netem_state(state_path.read_text(encoding="utf-8") if state_path.exists() else "")
    measured = parse_ping(ping["stdout"])
    lines = [line for line in qdisc["stdout"].splitlines() if "netem" in line]
    return {"ok": qdisc["ok"] and ping["ok"], "configured": configured, "measured": measured,
            "qdisc_lines": lines, "error": ping["stderr"] or qdisc["stderr"], "timestamp": utc_now()}


def request_walkthrough() -> dict:
    result = docker_exec(CLIENT, "curl", "-s", "-m", "3", "-i", f"http://{LB_IP}:{LB_PORT}/", timeout=6)
    head, _, body = result["stdout"].replace("\r\n", "\n").partition("\n\n")
    headers, status = {}, ""
    for number, line in enumerate(head.splitlines()):
        if number == 0:
            status = line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    return {"ok": result["ok"] and bool(status), "status": status, "headers": headers, "body": body.strip(),
            "error": result["stderr"], "timestamp": utc_now()}


# --- own LB statistics: one fetch per second in a background thread, kept as history
HISTORY: collections.deque = collections.deque(maxlen=900)
_latest: dict = {}
_stats_lock = threading.Lock()


def fetch_stats() -> dict:
    result = docker_exec(CLIENT, "curl", "-s", "-m", "2", f"http://{LB_IP}:{LB_STATS_PORT}/stats", timeout=5)
    try:
        return {"ok": True, "data": json.loads(result["stdout"])}
    except json.JSONDecodeError:
        return {"ok": False, "error": result["stderr"] or "the LB stats endpoint did not answer"}


def sample_once() -> None:
    global _latest
    result = fetch_stats()
    now = time.time()
    if not result["ok"]:
        with _stats_lock:
            _latest = {"ok": False, "t": now, "error": result["error"]}
        return
    data = result["data"]
    total = int(data.get("total_requests", 0))
    with _stats_lock:
        previous = HISTORY[-1] if HISTORY else None
        rps = None
        if previous and total >= previous["total"] and now > previous["t"]:
            rps = round((total - previous["total"]) / (now - previous["t"]), 1)
        latency = data.get("latency_ms", {})
        sample = {"ok": True, "t": now, "total": total, "errors": int(data.get("errors", 0)), "rps": rps,
                  "p50": latency.get("p50"), "p95": latency.get("p95"), "p99": latency.get("p99"),
                  "per_backend": {k: v.get("requests", 0) for k, v in data.get("per_backend", {}).items()}}
        HISTORY.append(sample)
        _latest = {**sample, "data": data}


def sampler_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            sample_once()
        except Exception:                                   # keep sampling whatever happens
            pass
        time.sleep(max(0.05, 1.0 - (time.monotonic() - started)))


def stats_data() -> dict:
    with _stats_lock:
        latest, history = dict(_latest), list(HISTORY)[-120:]
    if not latest or time.time() - latest.get("t", 0) > 5:
        return {"ok": False, "error": "no recent sample from the LB stats endpoint (is the own LB running?)",
                "history": [], "timestamp": utc_now()}
    if not latest["ok"]:
        return {"ok": False, "error": latest["error"], "history": [], "timestamp": utc_now()}
    return {"ok": True, "data": latest["data"], "rps": latest["rps"], "history": history,
            "sampled_at": latest["t"], "timestamp": utc_now()}


def haproxy_algorithm() -> str:
    cfg = REPO / "haproxy" / "haproxy.cfg"
    match = re.search(r"^\s*balance\s+(\S+)", cfg.read_text(encoding="utf-8") if cfg.exists() else "", re.MULTILINE)
    return HAPROXY_BALANCE.get(match.group(1), match.group(1)) if match else ""


def haproxy_summary(rows: list) -> dict:
    """Totals of the HAProxy backend ('be'): requests, current sessions, average response time, connection errors."""
    for row in rows:
        if row.get("pxname") == "be" and row.get("svname") == "BACKEND":
            def number(key):
                try:
                    return float(row.get(key) or 0)
                except ValueError:
                    return 0.0
            return {"total": int(number("stot")), "scur": int(number("scur")), "rtime_ms": number("rtime"),
                    "errors": int(number("econ") + number("eresp"))}
    return {"total": 0, "scur": 0, "rtime_ms": 0.0, "errors": 0}


HAPROXY_HISTORY: collections.deque = collections.deque(maxlen=120)
_haproxy_lock = threading.Lock()


def haproxy_data() -> dict:
    csv_result = docker_exec(CLIENT, "curl", "-s", "-m", "2", f"http://{HAPROXY_IP}:{HAPROXY_STATS_PORT}/;csv")
    rows = parse_haproxy_csv(csv_result["stdout"])
    wanted = ("svname", "status", "stot", "scur", "rtime", "ttime", "chkfail", "last_chk")
    servers = [{k: row.get(k, "") for k in wanted} for row in rows if row.get("pxname") == "be"
               and row.get("svname") not in ("FRONTEND", "BACKEND")]
    problem = csv_result["stderr"] or ("HAProxy statistics did not answer (is the HAProxy node running with a "
                                       "rendered haproxy.cfg?)")
    summary, rps, history = haproxy_summary(rows), None, []
    if servers:                      # requests/s from the growth of HAProxy's own request counter between two calls
        now = time.time()
        with _haproxy_lock:
            previous = HAPROXY_HISTORY[-1] if HAPROXY_HISTORY else None
            if previous and summary["total"] >= previous["total"] and now > previous["t"]:
                rps = round((summary["total"] - previous["total"]) / (now - previous["t"]), 1)
            HAPROXY_HISTORY.append({"t": now, "total": summary["total"], "rps": rps, "rtime_ms": summary["rtime_ms"]})
            history = list(HAPROXY_HISTORY)
    return {"ok": bool(servers), "servers": servers, "algorithm": haproxy_algorithm(), "summary": summary,
            "rps": rps, "history": history, "error": "" if servers else problem, "timestamp": utc_now()}


class LogTail:
    """Reads new backend_down/backend_up lines from the LB log incrementally.

    With INFO logging the file grows by about 250 KB/s under load, so re-reading a fixed tail would
    lose the event within seconds. Each call reads only the bytes added since the last call.
    """

    def __init__(self, path: pathlib.Path, first_window: int = 20_000_000):
        self.path, self.window, self.offset = path, first_window, None
        self.events: collections.deque = collections.deque(maxlen=500)
        self.lock = threading.Lock()

    def read(self) -> list:
        with self.lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                return list(self.events)
            first = self.offset is None
            if first:
                self.offset = max(0, size - self.window)
            elif size < self.offset:                              # the file was replaced or truncated
                self.offset, first = 0, True
                self.events.clear()
            if size > self.offset:
                with open(self.path, "rb") as fh:
                    fh.seek(self.offset)
                    data = fh.read(min(size - self.offset, 64_000_000))
                end = data.rfind(b"\n") + 1                       # complete lines only
                chunk = data[:end].decode("utf-8", errors="replace")
                if first and self.offset > 0 and "\n" in chunk:
                    chunk = chunk.split("\n", 1)[1]               # the first line may be cut in half
                self.offset += end
                self.events.extend(parse_own_events(chunk))
            return list(self.events)


LOG_TAIL = LogTail(REPO / "logs" / "lb.jsonl")
_detections: dict = {}                    # (system, kill_ms) -> detection, kept once it has been found


def haproxy_events_since(since_ms: int) -> list:
    """HAProxy health events after since_ms. Streams `docker logs` and keeps only the Server lines,
    because HAProxy also logs every request."""
    since = datetime.datetime.fromtimestamp(since_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        proc = subprocess.Popen(["docker", "logs", "--timestamps", "--since", since, PREFIX + "haproxy"],
                                cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError:
        return []
    kept = []
    try:
        for line in proc.stdout:
            if " Server " in line or line.startswith("Server "):
                kept.append(line)
    finally:
        proc.stdout.close()
        proc.wait(timeout=10)
    return parse_haproxy_events("".join(kept))


def failover_data() -> dict:
    kill_ms = None
    if KILL_TS_FILE.exists():
        try:
            kill_ms = int(KILL_TS_FILE.read_text().strip())
        except ValueError:
            pass
    own = LOG_TAIL.read()
    since = (kill_ms - 10_000) if kill_ms else int((time.time() - 600) * 1000)
    hap = haproxy_events_since(since)
    events = sorted(own + hap, key=lambda e: e["ts_ms"])[-30:]

    def detection(system):
        if kill_ms is None:
            return None
        if (system, kill_ms) in _detections:
            return _detections[(system, kill_ms)]
        for event in sorted(own + hap, key=lambda e: e["ts_ms"]):
            if (event["system"] == system and event["backend"] == "b3" and event["state"] == "DOWN"
                    and event["ts_ms"] >= kill_ms):
                found = {"seconds": round((event["ts_ms"] - kill_ms) / 1000, 3), "ts_ms": event["ts_ms"],
                         "reason": event["reason"]}
                _detections[(system, kill_ms)] = found
                return found
        return None

    own_detect, hap_detect = detection("own"), detection("haproxy")
    failed = None
    if kill_ms and own_detect:
        with _stats_lock:
            samples = list(HISTORY)
        before = [s for s in samples if s["t"] * 1000 <= kill_ms]
        after = [s for s in samples if s["t"] * 1000 >= own_detect["ts_ms"]]
        if before and after:
            failed = max(0, after[0]["errors"] - before[-1]["errors"])
    return {"ok": True, "kill_ms": kill_ms, "own": own_detect, "haproxy": hap_detect,
            "failed_requests_approx": failed, "events": events, "timestamp": utc_now()}


def read_env_file() -> dict:
    env, path = {}, REPO / "backends" / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def live_cpu_limits() -> dict:
    names = ["lb", "haproxy", "b1", "b2", "b3", "b4", "c1", "c2", "c3", "c4"]
    result = run(["docker", "inspect", "-f",
                  "{{.Name}} {{.HostConfig.NanoCpus}} {{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}}",
                  *[PREFIX + n for n in names]])
    limits = {}
    for line in result["stdout"].splitlines():
        parts = line.split()
        if len(parts) == 4:
            try:
                limits[parts[0].lstrip("/").replace(PREFIX, "")] = cpu_limit(int(parts[1]), int(parts[2]), int(parts[3]))
            except ValueError:
                pass
    return limits


def backend_capacity() -> dict:
    env, live = read_env_file(), cached("cpu", 10, live_cpu_limits)

    def probe(index: int) -> dict:
        backend_id = f"b{index}"
        result = docker_exec(CLIENT, "curl", "-s", "-m", "2", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                             f"http://10.0.3.{10 + index}:{BACKEND_PORT}/health", timeout=5)
        code, _, seconds = result["stdout"].partition(" ")
        ok = code == "200"
        return {"id": backend_id, "address": f"10.0.3.{10 + index}", "delay_ms": env.get(f"B{index}_DELAY_MS"),
                "cpu": env.get(f"B{index}_CPUS"), "live_cpu": live.get(backend_id),
                "health": {"ok": ok, "ms": round(float(seconds) * 1000, 1) if ok and seconds else None}}

    with ThreadPoolExecutor(4) as pool:
        backends = list(pool.map(probe, range(1, 5)))
    return {"ok": True, "backends": backends, "timestamp": utc_now()}


def load_results() -> dict:
    records = []
    for path in sorted((REPO / "results").glob("**/summary.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["_source"] = str(path.relative_to(REPO)).replace("\\", "/")
            records.append(record)
        except (OSError, json.JSONDecodeError):
            continue
    return {"ok": True, "dry_run": any(r.get("exp") == "checkpoint2" for r in records),
            "records": records, "timestamp": utc_now()}


ENV_SHOW = ("date_utc", "git_commit", "cpu_model", "cpu_cores", "memory", "os", "kernel", "docker",
            "containerlab", "python", "haproxy", "ulimit_host", "ulimit_c1")


def environment_data() -> dict:
    files = {}
    for path in sorted((REPO / "results").glob("**/environment.txt")):
        parsed = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in ENV_SHOW:
                parsed[key.strip()] = value.strip()
        files[str(path.relative_to(REPO)).replace("\\", "/")] = parsed
    limits = cached("cpu", 10, live_cpu_limits)
    return {"ok": bool(limits), "environment_files": files, "container_cpu_limits": limits,
            "note": "CPU limits come from the running containers (CpuQuota/CpuPeriod). The CPU lines in older "
                    "environment.txt files said 'unlimited' because of a bug, so they are not shown.",
            "timestamp": utc_now()}


def state_data() -> dict:
    topo = cached("topology", 1.5, topology)
    running = {n["id"]: n["running"] for n in topo["nodes"]}
    state_path = REPO / "topo" / ".netem_state"
    netem = parse_netem_state(state_path.read_text(encoding="utf-8") if state_path.exists() else "")
    with _stats_lock:
        own_algorithm = (_latest.get("data") or {}).get("algorithm", "") if _latest.get("ok") else ""
    load = cached("load", 2, lambda: docker_exec(CLIENT, "pgrep", "-x", "hey", timeout=5))
    return {"ok": True, "rtt_ms": netem["RTT_MS"], "loss_pct": netem["LOSS_PCT"],
            "own_algorithm": own_algorithm, "haproxy_algorithm": haproxy_algorithm(),
            "lb_running": running.get("lb", False), "haproxy_running": running.get("haproxy", False),
            "b3_running": running.get("b3", False), "load_running": load["returncode"] == 0,
            "timestamp": utc_now()}


# --------------------------------------------------------------------------- controls

_control_lock = threading.Lock()


def wait_for_http(url: str, seconds: float = 20) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = docker_exec(CLIENT, "curl", "-s", "-m", "2", "-o", "/dev/null", "-w", "%{http_code}", url, timeout=5)
        if result["stdout"] == "200":
            return True
        time.sleep(0.5)
    return False


def deployed_config_path():
    """Host path of the file mounted as /etc/lb/config.json in the LB container."""
    result = run(["docker", "inspect", "-f",
                  '{{range .Mounts}}{{if eq .Destination "/etc/lb/config.json"}}{{.Source}}{{end}}{{end}}',
                  PREFIX + "lb"])
    return pathlib.Path(result["stdout"]) if result["ok"] and result["stdout"] else None


def switch_own_algorithm(algorithm: str) -> tuple:
    source = deployed_config_path()
    if source is None:
        return False, "The own LB container was not found. Deploy the lab first (bash ui/start.sh).", ""
    if (REPO / "results") not in source.resolve().parents:
        return False, ("The lab was deployed with LB_CONFIG=" + str(source) + ", which the UI must not edit. "
                       "Redeploy with: bash ui/start.sh"), ""
    configs = UI_RUN / "configs"
    made = run([sys.executable, str(REPO / "bench" / "make_configs.py"), "--out-dir", str(configs)], timeout=20)
    new_config = configs / f"config.{algorithm}.json"
    if not made["ok"] or not new_config.exists():
        return False, "Could not generate the config: " + made["stderr"], made["stdout"]
    source.write_text(new_config.read_text(encoding="utf-8"), encoding="utf-8")   # in place: the bind mount follows
    restart = run(["docker", "restart", "-t", "1", PREFIX + "lb"], timeout=40)
    if not restart["ok"]:
        return False, "Config written but the LB did not restart: " + restart["stderr"], ""
    ready = wait_for_http(f"http://{LB_IP}:{LB_PORT}/")
    return ready, (f"Own LB now uses {algorithm}." if ready else "LB restarted but is not answering yet."), ""


def control(path: str, body: dict) -> tuple:
    """Run one fixed control. Returns (ok, message, detail)."""
    env = {"BACKEND_PREFIX": PREFIX}
    if path == "kill-b3":
        UI_RUN.mkdir(parents=True, exist_ok=True)
        result = run(["bash", str(REPO / "backends" / "kill_backend.sh"), "b3"], timeout=20,
                     env_extra={**env, "KILL_TS_FILE": str(KILL_TS_FILE)})
        return result["ok"], ("b3 killed (docker kill)." if result["ok"] else "Kill failed: " + result["stderr"]), result["stderr"]
    if path == "start-b3":
        result = run(["bash", str(REPO / "backends" / "start_backend.sh"), "b3"], timeout=40,
                     env_extra={**env, "BACKEND_PORT": str(BACKEND_PORT)})
        return result["ok"], ("b3 restored and healthy." if result["ok"] else "Restore failed: " + result["stderr"]), result["stderr"]
    if path == "start-load":
        if docker_exec(CLIENT, "pgrep", "-x", "hey", timeout=5)["returncode"] == 0:
            return True, "Load is already running.", ""
        # Aim the load at whichever system is running; when both run (demo), split the connections between them.
        targets = [(name, ip) for name, ip, node in (("own LB", LB_IP, "lb"), ("HAProxy", HAPROXY_IP, "haproxy"))
                   if run(["docker", "inspect", "-f", "{{.State.Running}}", PREFIX + node])["stdout"] == "true"]
        if not targets:
            return False, "Neither the own LB nor HAProxy is running.", ""
        conns = str(50 // len(targets))
        started = [run(["docker", "exec", "-d", PREFIX + node, "hey", "-z", "60s", "-c", conns,
                        f"http://{ip}:{LB_PORT}/"], timeout=10)
                   for node in ("c1", "c2", "c3", "c4") for _, ip in targets]
        ok = all(item["ok"] for item in started)
        names = " and ".join(name for name, _ in targets)
        return ok, (f"Load started on {names}: 4 clients x {conns} connections each for 60 s." if ok else "Could not start load."), ""
    if path == "stop-load":
        for node in ("c1", "c2", "c3", "c4"):
            docker_exec(node, "pkill", "-x", "hey", timeout=10)      # exit code 1 only means "nothing was running"
        return True, "Load stopped.", ""
    if path == "rtt":
        value = str(body.get("rtt_ms", ""))
        if value not in RTT_VALUES:
            raise ValueError("RTT must be 0, 20, 100 or 200")
        result = run(["bash", str(REPO / "topo" / "set_rtt.sh"), value], timeout=20)
        return result["ok"], (f"Round-trip delay set to {value} ms." if result["ok"] else "set_rtt failed: " + result["stderr"]), result["stdout"]
    if path == "algorithm":
        algorithm = str(body.get("algorithm", ""))
        if algorithm not in ALGORITHMS:
            raise ValueError("invalid algorithm")
        return switch_own_algorithm(algorithm)
    if path == "haproxy-algorithm":
        algorithm = str(body.get("algorithm", ""))
        if algorithm not in ALGORITHMS:
            raise ValueError("invalid algorithm")
        rendered = run(["bash", str(REPO / "haproxy" / "render.sh"), algorithm], timeout=60)
        restarted = run(["docker", "restart", "-t", "1", PREFIX + "haproxy"], timeout=40)
        ready = rendered["ok"] and restarted["ok"] and wait_for_http(f"http://{HAPROXY_IP}:{LB_PORT}/")
        return ready, (f"HAProxy now uses {algorithm}." if ready else "HAProxy change failed: " + (
            rendered["stderr"] or restarted["stderr"] or "not answering")), ""
    if path == "active-system":
        system = str(body.get("system", ""))
        if system not in ACTIVE_SYSTEMS:
            raise ValueError("system must be own or haproxy")
        if system == "both":
            # Live side-by-side demo: both run, different addresses (10.0.2.10 and .11). Not for measurements:
            # they then share the host CPU and the load, so experiments always run one system at a time.
            first = run(["docker", "restart", "-t", "1", PREFIX + "lb"], timeout=40)
            second = run(["docker", "restart", "-t", "1", PREFIX + "haproxy"], timeout=40)
            ok = first["ok"] and second["ok"] and wait_for_http(f"http://{LB_IP}:{LB_PORT}/") \
                and wait_for_http(f"http://{HAPROXY_IP}:{LB_PORT}/")
            return ok, ("Both systems are running (demo mode: shared CPU, do not use for measurements)." if ok
                        else "Could not start both: " + (first["stderr"] or second["stderr"] or "not answering")), ""
        start, stop = ("lb", "haproxy") if system == "own" else ("haproxy", "lb")
        started = run(["docker", "restart", "-t", "1", PREFIX + start], timeout=40)
        stopped = run(["docker", "stop", "-t", "1", PREFIX + stop], timeout=40)
        ok = started["ok"] and stopped["ok"]
        label = "Own LB" if system == "own" else "HAProxy"
        return ok, (f"{label} is the active system; the other is stopped." if ok
                    else "Switch failed: " + (started["stderr"] or stopped["stderr"])), ""
    raise ValueError("unknown control")


# --------------------------------------------------------------------------- HTTP server

def _logs_payload() -> dict:
    path = REPO / "logs" / "lb.jsonl"
    return {"ok": path.exists(), "text": tail_text(path) if path.exists() else "", "timestamp": utc_now()}


GET_ROUTES = {
    "/api/status": lambda: {"ok": True, "timestamp": utc_now(), "host": "127.0.0.1"},
    "/api/topology": lambda: cached("topology", 1.5, topology),
    "/api/routes": lambda: cached("routes", 4, route_data),
    "/api/netem": lambda: cached("netem", 2, netem_data),
    "/api/request": request_walkthrough,
    "/api/stats": stats_data,
    "/api/haproxy": lambda: cached("haproxy", 2, haproxy_data),
    "/api/failover": lambda: cached("failover", 2, failover_data),
    "/api/backends": lambda: cached("backends", 3, backend_capacity),
    "/api/logs": _logs_payload,
    "/api/results": load_results,
    "/api/environment": environment_data,
    "/api/state": state_data,
    "/api/annotations": lambda: json.loads((REPO / "ui" / "annotations.json").read_text(encoding="utf-8")),
}
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
                 ".svg": "image/svg+xml"}
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"),
                    ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'"))


class RelayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Project5UIRelay/2.0"

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def refuse(self, method: str) -> bool:
        ok, reason = check_request(method, self.headers)
        if not ok:
            self.send_json(403, {"ok": False, "error": reason})
        return not ok

    def do_HEAD(self) -> None:
        if self.refuse("HEAD"):
            return
        found = self.path.split("?", 1)[0] in ("/", "/index.html")
        self.send_response(200 if found else 404)
        if found:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str((STATIC / "index.html").stat().st_size))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.refuse("GET"):
            return
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self.serve_static("index.html")
            elif path.startswith("/static/"):
                self.serve_static(path[len("/static/"):])
            elif path == "/api/captures/ui_live.pcap":
                pcap = REPO / "captures" / "ui_live.pcap"
                if not pcap.exists():
                    self.send_json(404, {"ok": False, "error": "capture not found"})
                    return
                body = pcap.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.tcpdump.pcap")
                self.send_header("Content-Disposition", 'attachment; filename="ui_live.pcap"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in GET_ROUTES:
                self.send_json(200, GET_ROUTES[path]())
            else:
                self.send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:                                # one bad panel must not stop the relay
            traceback.print_exc()
            self.send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        if self.refuse("POST"):
            return
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        try:
            if path == "/api/capture":
                self.send_json(200, packet_capture())
            elif path.startswith("/api/control/"):
                try:
                    with _control_lock:                          # one change at a time
                        ok, message, detail = control(path[len("/api/control/"):], body if isinstance(body, dict) else {})
                except ValueError as exc:                        # bad input from the browser
                    self.send_json(400, {"ok": False, "message": str(exc), "timestamp": utc_now()})
                    return
                with _cache_lock:                                # the lab just changed: drop cached answers
                    _cache.clear()
                self.send_json(200 if ok else 500, {"ok": ok, "message": message, "detail": detail,
                                                    "timestamp": utc_now()})
            else:
                self.send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}", "timestamp": utc_now()})

    def serve_static(self, relative: str) -> None:
        target = (STATIC / (relative or "index.html")).resolve()
        if STATIC.resolve() not in target.parents:
            self.send_json(403, {"ok": False, "error": "forbidden"})
            return
        if not target.is_file():
            self.send_json(404, {"ok": False, "error": "file not found"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream") + "; charset=utf-8")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    os.chdir(REPO)
    UI_RUN.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=sampler_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), RelayHandler)
    server.daemon_threads = True
    print(f"Project 5 UI relay listening at http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
