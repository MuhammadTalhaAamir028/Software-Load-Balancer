#!/usr/bin/env python3
"""Localhost-only JSON relay for the Project 5 UI."""

from __future__ import annotations

import csv
import http.server
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.request


REPO = pathlib.Path(__file__).resolve().parent.parent
STATIC = REPO / "ui" / "static"
PREFIX = "clab-lb-"
NODES = {"c1", "c2", "c3", "c4", "r1", "r2", "lb", "haproxy", "b1", "b2", "b3", "b4"}
RTT_VALUES = {"0", "20", "100", "200"}
ALGORITHMS = {"round_robin", "least_conn", "ip_hash"}
ACTIVE_SYSTEMS = {"own", "haproxy"}

NODE_INFO = {
    "c1": ("10.0.1.11", "client"),
    "c2": ("10.0.1.12", "client"),
    "c3": ("10.0.1.13", "client"),
    "c4": ("10.0.1.14", "client"),
    "r1": ("10.0.1.1 / 10.0.2.1", "router"),
    "r2": ("10.0.2.2 / 10.0.3.1", "router"),
    "lb": ("10.0.2.10", "load balancer"),
    "haproxy": ("10.0.2.11", "reference LB"),
    "b1": ("10.0.3.11", "backend"),
    "b2": ("10.0.3.12", "backend"),
    "b3": ("10.0.3.13", "backend"),
    "b4": ("10.0.3.14", "backend"),
}


def utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(args: list[str], timeout: int = 15, env_extra: dict | None = None) -> dict:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            args,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "timestamp": utc_now(),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "timestamp": utc_now()}


def fetch_json(url: str, timeout: float = 2.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return {"ok": True, "data": json.load(response), "timestamp": utc_now()}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "data": None, "error": str(exc), "timestamp": utc_now()}


def read_text(path: pathlib.Path) -> dict:
    try:
        return {"ok": True, "text": path.read_text(encoding="utf-8"), "timestamp": utc_now()}
    except OSError as exc:
        return {"ok": False, "text": "", "error": str(exc), "timestamp": utc_now()}


def topology() -> dict:
    nodes = []
    for node, (address, kind) in NODE_INFO.items():
        result = run(["docker", "inspect", "-f", "{{.State.Running}}", PREFIX + node])
        nodes.append({
            "id": node,
            "address": address,
            "kind": kind,
            "running": result["ok"] and result["stdout"] == "true",
            "timestamp": result["timestamp"],
        })
    return {"ok": True, "nodes": nodes, "links": [
        ["c1", "r1"], ["c2", "r1"], ["c3", "r1"], ["c4", "r1"],
        ["r1", "r2"], ["r1", "lb"], ["r1", "haproxy"],
        ["r2", "b1"], ["r2", "b2"], ["r2", "b3"], ["r2", "b4"],
    ], "timestamp": utc_now()}


def route_data() -> dict:
    routes = {}
    for node in ("r1", "r2", "lb", "c1", "b1"):
        result = run(["docker", "exec", PREFIX + node, "ip", "route"])
        routes[node] = result
    traceroute = run(["docker", "exec", PREFIX + "c1", "traceroute", "-n", "-w", "2", "-q", "1", "10.0.3.11"])
    return {"ok": all(item["ok"] for item in routes.values()) and traceroute["ok"],
            "routes": routes, "traceroute": traceroute, "timestamp": utc_now()}


def netem_data() -> dict:
    r1 = run(["docker", "exec", PREFIX + "r1", "tc", "qdisc", "show"])
    ping = run(["docker", "exec", PREFIX + "c1", "ping", "-c", "3", "10.0.2.10"])
    state = read_text(REPO / "topo" / ".netem_state")
    configured = {"RTT_MS": 0, "LOSS_PCT": 0}
    if state["ok"]:
        for line in state["text"].splitlines():
            key, separator, value = line.partition("=")
            if separator:
                try:
                    configured[key] = int(float(value))
                except ValueError:
                    pass
    return {"ok": r1["ok"] and ping["ok"], "qdisc": r1, "ping": ping,
            "configured": configured, "timestamp": utc_now()}


def request_walkthrough() -> dict:
    result = run(["docker", "exec", PREFIX + "c1", "curl", "-s", "-m", "3", "-i",
                  "http://10.0.2.10:8080/"], timeout=5)
    return {"ok": result["ok"], "curl": result, "timestamp": utc_now()}


def packet_capture() -> dict:
    result = run(["bash", str(REPO / "topo" / "capture.sh"), "ui_live",
                  "lb", "eth1", "tcp port 8080", "5"], timeout=20)
    pcap = REPO / "captures" / "ui_live.pcap"
    decoded = {"ok": False, "stdout": "", "stderr": "tshark unavailable", "timestamp": utc_now()}
    if pcap.exists():
        decoded = run([
            "tshark", "-n", "-r", str(pcap), "-Y",
            "tcp or http", "-T", "fields",
            "-E", "header=n", "-E", "separator=|",
            "-e", "frame.number", "-e", "frame.time_relative",
            "-e", "ip.src", "-e", "ip.dst",
            "-e", "tcp.flags", "-e", "tcp.stream",
            "-e", "http.request.method", "-e", "http.response.code",
        ], timeout=15)
    return {"ok": result["ok"] and pcap.exists(), "capture": result,
            "pcap": "/api/captures/ui_live.pcap" if pcap.exists() else None,
            "packets": decoded,
            "timestamp": utc_now()}


def haproxy_data() -> dict:
    csv_result = run(["docker", "exec", PREFIX + "haproxy", "curl", "-s",
                      "http://127.0.0.1:8404/;csv"])
    logs = run(["docker", "logs", "--tail", "100", "--timestamps", PREFIX + "haproxy"])
    rows = []
    if csv_result["ok"] and csv_result["stdout"]:
        text = csv_result["stdout"]
        lines = [line for line in text.splitlines() if line.strip() and not line.startswith("# ")
                 and not line.startswith("pxname")]
        header_line = next((line for line in text.splitlines() if line.startswith("# ")), "")
        if header_line:
            fields = [item.strip() for item in header_line.lstrip("# ").split(",")]
            for line in lines:
                values = next(csv.reader([line]))
                if len(values) == len(fields):
                    rows.append(dict(zip(fields, values)))
    return {"ok": csv_result["ok"] or logs["ok"], "csv": rows, "logs": logs,
            "timestamp": utc_now()}


def backend_capacity() -> dict:
    backends = []
    env = {}
    env_path = REPO / "backends" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    for index in range(1, 5):
        backend_id = "b" + str(index)
        health = run([
            "docker", "exec", PREFIX + backend_id,
            "python", "-c",
            "import urllib.request,sys\n"
            "r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1)\n"
            "sys.exit(0 if r.status == 200 else 1)",
        ], timeout=5)
        backends.append({
            "id": backend_id,
            "address": "10.0.3." + str(10 + index),
            "delay_ms": env.get("B" + str(index) + "_DELAY_MS"),
            "cpu": env.get("B" + str(index) + "_CPUS"),
            "health": health,
        })
    return {"ok": True, "backends": backends, "timestamp": utc_now()}


def load_results() -> dict:
    root = REPO / "results"
    records = []
    for path in sorted(root.glob("**/summary.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["_source"] = str(path.relative_to(REPO)).replace("\\", "/")
            records.append(record)
        except (OSError, json.JSONDecodeError):
            continue
    return {"ok": True, "dry_run": True if records and any(r.get("exp") == "checkpoint2" for r in records) else False,
            "records": records, "timestamp": utc_now()}


def environment_data() -> dict:
    records = {}
    for path in sorted((REPO / "results").glob("**/environment.txt")):
        parsed = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                parsed[key.strip()] = value.strip()
        records[str(path.relative_to(REPO)).replace("\\", "/")] = parsed
    cpu_limits = {}
    for name in ("lb", "haproxy", "b1", "b2", "b3", "b4", "c1", "c2", "c3", "c4"):
        result = run([
            "docker", "inspect", "-f",
            "{{.HostConfig.NanoCpus}}", PREFIX + name,
        ], timeout=5)
        if result["ok"]:
            try:
                nano_cpus = int(result["stdout"])
                cpu_limits[name] = "unlimited" if nano_cpus == 0 else round(nano_cpus / 1e9, 3)
            except ValueError:
                cpu_limits[name] = "no data"
        else:
            cpu_limits[name] = "no data"
    return {"ok": bool(records) or bool(cpu_limits),
            "environment_files": records,
            "container_cpu_limits": cpu_limits,
            "timestamp": utc_now()}


class RelayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Project5UIRelay/1.0"

    def read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {}

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.serve_static("index.html")
        elif path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
        elif path.startswith("/api/captures/") and path.count("/") == 3:
            name = pathlib.Path(path).name
            if name != "ui_live.pcap":
                self.send_json(404, {"ok": False, "error": "capture not found"})
                return
            pcap = REPO / "captures" / name
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
        elif path == "/api/status":
            self.send_json(200, {"ok": True, "timestamp": utc_now(), "host": "127.0.0.1"})
        elif path == "/api/topology":
            self.send_json(200, topology())
        elif path == "/api/routes":
            self.send_json(200, route_data())
        elif path == "/api/netem":
            self.send_json(200, netem_data())
        elif path == "/api/request":
            self.send_json(200, request_walkthrough())
        elif path == "/api/capture":
            self.send_json(200, packet_capture())
        elif path == "/api/stats":
            self.send_json(200, fetch_json("http://10.0.2.10:9090/stats"))
        elif path == "/api/haproxy":
            self.send_json(200, haproxy_data())
        elif path == "/api/backends":
            self.send_json(200, backend_capacity())
        elif path == "/api/logs":
            self.send_json(200, read_text(REPO / "logs" / "lb.jsonl"))
        elif path == "/api/results":
            self.send_json(200, load_results())
        elif path == "/api/annotations":
            self.send_json(200, json.loads((REPO / "ui" / "annotations.json").read_text(encoding="utf-8")))
        elif path == "/api/environment":
            self.send_json(200, environment_data())
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/control/kill-b3":
            result = run(["bash", str(REPO / "backends" / "kill_backend.sh"), "b3"],
                          timeout=20,
                          env_extra={"BACKEND_PREFIX": PREFIX,
                                     "KILL_TS_FILE": str(REPO / "kill_ts.txt")})
        elif path == "/api/control/start-b3":
            result = run(["bash", str(REPO / "backends" / "start_backend.sh"), "b3"],
                          timeout=40,
                          env_extra={"BACKEND_PREFIX": PREFIX,
                                     "BACKEND_PORT": "8000"})
        elif path == "/api/control/start-load":
            processes = []
            for node in ("c1", "c2", "c3", "c4"):
                processes.append(run([
                    "docker", "exec", "-d", PREFIX + node,
                    "sh", "-c",
                    "hey -z 60s -c 50 http://10.0.2.10:8080/",
                ], timeout=10))
            result = {"ok": all(item["ok"] for item in processes),
                      "stdout": json.dumps([item["stdout"] for item in processes]),
                      "stderr": json.dumps([item["stderr"] for item in processes]),
                      "timestamp": utc_now()}
        elif path == "/api/control/stop-load":
            processes = []
            for node in ("c1", "c2", "c3", "c4"):
                processes.append(run([
                    "docker", "exec", PREFIX + node,
                    "pkill", "-x", "hey",
                ], timeout=10))
            result = {"ok": all(item["ok"] for item in processes),
                      "stdout": "", "stderr": "",
                      "timestamp": utc_now()}
        elif path == "/api/control/rtt":
            body = self.read_json_body()
            value = str(body.get("rtt_ms", ""))
            if value not in RTT_VALUES:
                self.send_json(400, {"ok": False, "error": "RTT must be 0, 20, 100, or 200"})
                return
            result = run(["bash", str(REPO / "topo" / "set_rtt.sh"), value], timeout=20)
        elif path == "/api/control/algorithm":
            body = self.read_json_body()
            algorithm = str(body.get("algorithm", ""))
            if algorithm not in ALGORITHMS:
                self.send_json(400, {"ok": False, "error": "invalid algorithm"})
                return
            configs = REPO / "results" / "configs"
            make_configs = run([
                "python3", str(REPO / "bench" / "make_configs.py"),
                "--out-dir", str(configs),
            ], timeout=20)
            source = configs / ("config." + algorithm + ".json")
            if not make_configs["ok"] or not source.exists():
                result = make_configs
            else:
                copy = run([
                    "docker", "cp", str(source), PREFIX + "lb:/etc/lb/config.json",
                ], timeout=20)
                restart = run([
                    "docker", "restart", "-t", "1", PREFIX + "lb",
                ], timeout=40)
                result = {
                    "ok": copy["ok"] and restart["ok"],
                    "stdout": algorithm,
                    "stderr": "\n".join([copy["stderr"], restart["stderr"]]),
                    "timestamp": utc_now(),
                }
        elif path == "/api/control/haproxy-algorithm":
            body = self.read_json_body()
            algorithm = str(body.get("algorithm", ""))
            if algorithm not in ALGORITHMS:
                self.send_json(400, {"ok": False, "error": "invalid algorithm"})
                return
            rendered = run([
                "bash", str(REPO / "haproxy" / "render.sh"), algorithm,
            ], timeout=40)
            restarted = run([
                "docker", "restart", "-t", "1", PREFIX + "haproxy",
            ], timeout=40)
            result = {
                "ok": rendered["ok"] and restarted["ok"],
                "stdout": algorithm,
                "stderr": "\n".join([rendered["stderr"], restarted["stderr"]]),
                "timestamp": utc_now(),
            }
        elif path == "/api/control/active-system":
            body = self.read_json_body()
            system = str(body.get("system", ""))
            if system not in ACTIVE_SYSTEMS:
                self.send_json(400, {"ok": False, "error": "system must be own or haproxy"})
                return
            own = run(["docker", "restart", "-t", "1", PREFIX + ("lb" if system == "own" else "haproxy")],
                      timeout=40)
            reference = run(["docker", "stop", "-t", "1", PREFIX + ("haproxy" if system == "own" else "lb")],
                            timeout=40)
            result = {
                "ok": own["ok"] and reference["ok"],
                "stdout": system,
                "stderr": "\n".join([own["stderr"], reference["stderr"]]),
                "timestamp": utc_now(),
            }
        else:
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        self.send_json(200 if result["ok"] else 500, {"ok": result["ok"],
                                                       "result": result,
                                                       "timestamp": utc_now()})

    def serve_static(self, relative: str) -> None:
        if relative == "":
            relative = "index.html"
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self.send_json(403, {"ok": False, "error": "forbidden"})
            return
        if not target.is_file():
            self.send_json(404, {"ok": False, "error": "file not found"})
            return
        body = target.read_bytes()
        content_type = "text/html"
        if target.suffix == ".css":
            content_type = "text/css"
        elif target.suffix == ".js":
            content_type = "text/javascript"
        elif target.suffix == ".json":
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    os.chdir(REPO)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), RelayHandler)
    print("Project 5 UI relay listening at http://127.0.0.1:8765", flush=True)
    server.serve_forever()
