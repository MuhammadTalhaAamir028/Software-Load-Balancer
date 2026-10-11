"""Statistics HTTP server and dashboard host (module lb.obs.stats_server).

A deliberately tiny HTTP/1.1 server built on asyncio streams. It does no
per-request work for the load balancer: every response is produced only when a
client (the dashboard, curl, or a benchmark script) asks for it.

Routes (GET only):
  /         the dashboard page (lb/obs/dashboard/index.html, read once at start-up)
  /stats    JSON: the metrics snapshot merged with live pool state
  /healthz  the text "ok"
Anything else is 404; any method other than GET is 405.

Every response carries "Connection: close", so each connection serves exactly
one request. This keeps the code simple and the server immune to idle clients.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Mapping

# The dashboard lives next to this file, at lb/obs/dashboard/index.html
# (the location required by the project brief).
DASHBOARD_PATH = Path(__file__).resolve().parent / "dashboard" / "index.html"

# Request line + headers may not exceed this many bytes (the brief says 8 KB).
MAX_REQUEST_BYTES = 8192

# A client that does not finish its request headers within this time is dropped,
# so a stalled connection cannot hold the server forever.
REQUEST_TIMEOUT_S = 5.0

_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}

_FALLBACK_HTML = (
    b"<!doctype html><meta charset=utf-8><title>LB stats</title>"
    b"<h1>Dashboard file not found</h1>"
    b"<p>Expected lb/obs/dashboard/index.html. "
    b"The JSON endpoint /stats still works.</p>"
)


def _load_dashboard() -> bytes:
    """Read the dashboard once. A missing file must not stop the server."""
    try:
        return DASHBOARD_PATH.read_bytes()
    except OSError:
        return _FALLBACK_HTML


def build_stats(cfg: Mapping[str, Any], metrics: Any, pool: Any) -> Dict[str, Any]:
    """Merge the metrics snapshot with the live pool state and the algorithm name.

    Metrics knows only what happened (requests, errors, latency). The pool knows
    what is true right now (active connections, health). Backends that are in the
    pool but have never served a request still appear, with zero counters.
    """
    snap = metrics.snapshot()
    counted = snap.get("per_backend", {})
    zero = {"requests": 0, "errors": 0, "bytes_up": 0, "bytes_down": 0}

    per_backend: Dict[str, Dict[str, Any]] = {}

    def entry(backend_id: str, active: int, healthy: bool) -> Dict[str, Any]:
        c = counted.get(backend_id, zero)
        # Key order follows the schema in the brief.
        return {"requests": c["requests"], "errors": c["errors"],
                "active": active, "healthy": healthy,
                "bytes_up": c["bytes_up"], "bytes_down": c["bytes_down"]}

    # Pool order first (this is the configuration order)...
    for b in pool.all():
        per_backend[b.id] = entry(b.id, int(b.active), bool(b.healthy))
    # ...then any backend that metrics has seen but the pool no longer lists.
    for backend_id in counted:
        if backend_id not in per_backend:
            per_backend[backend_id] = entry(backend_id, 0, False)

    return {
        "uptime_s": snap["uptime_s"],
        "algorithm": cfg.get("algorithm"),
        "total_requests": snap["total_requests"],
        "errors": snap["errors"],
        "latency_ms": snap["latency_ms"],
        "per_backend": per_backend,
        "health_events": snap["health_events"],
    }


def _response(status: int, body: bytes, content_type: str, extra: str = "") -> bytes:
    """Assemble a complete HTTP/1.1 response."""
    head = (
        "HTTP/1.1 %d %s\r\n" % (status, _REASONS.get(status, "Unknown"))
        + "Content-Type: %s\r\n" % content_type
        + "Content-Length: %d\r\n" % len(body)
        # The statistics change every second; browsers and proxies must not reuse them.
        + "Cache-Control: no-store\r\n"
        # One request per connection (see the module docstring).
        + "Connection: close\r\n"
        + extra
        + "\r\n"
    )
    return head.encode("latin-1") + body


async def _send(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write a response; a client that has already left is not an error."""
    try:
        writer.write(data)
        await writer.drain()
    except ConnectionError:
        pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  cfg: Mapping[str, Any], metrics: Any, pool: Any,
                  dashboard: bytes) -> None:
    """Serve one connection. Nothing may escape this function."""
    try:
        # readuntil collects the request line and all headers up to the blank
        # line. It raises LimitOverrunError when more than the stream limit
        # (MAX_REQUEST_BYTES) arrives first, which protects memory.
        try:
            block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                           REQUEST_TIMEOUT_S)
        except asyncio.LimitOverrunError:
            await _send(writer, _response(431, b"request too large\n", "text/plain"))
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return  # client left or stalled: nothing to answer

        # Only the first line matters; the headers were read just to drain them.
        parts = block.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            await _send(writer, _response(400, b"bad request\n", "text/plain"))
            return
        method, target, _version = parts

        if method != "GET":
            await _send(writer, _response(405, b"method not allowed\n", "text/plain",
                                          extra="Allow: GET\r\n"))
            return

        path = target.split("?", 1)[0]
        if path == "/stats":
            try:
                body = json.dumps(build_stats(cfg, metrics, pool)).encode("utf-8")
                await _send(writer, _response(200, body, "application/json"))
            except Exception:
                await _send(writer, _response(
                    500, b'{"error":"stats unavailable"}', "application/json"))
        elif path == "/":
            await _send(writer, _response(200, dashboard, "text/html; charset=utf-8"))
        elif path == "/healthz":
            await _send(writer, _response(200, b"ok", "text/plain"))
        else:
            await _send(writer, _response(404, b"not found\n", "text/plain"))
    except Exception:
        # Last line of defence: one bad client must never crash the stats server.
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_stats_server(cfg: Mapping[str, Any], metrics: Any,
                             pool: Any) -> asyncio.AbstractServer:
    """Start the stats server (contract: lb/interfaces.py) and return the server.

    Reads cfg["stats"]["host"] (default 0.0.0.0) and cfg["stats"]["port"]
    (default 9090). Port 0 asks the operating system for a free port, which the
    tests use.
    """
    section = cfg.get("stats") or {}
    dashboard = _load_dashboard()  # read once, as required by the brief

    async def on_connect(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, cfg, metrics, pool, dashboard)

    return await asyncio.start_server(
        on_connect,
        host=section.get("host", "0.0.0.0"),
        port=int(section.get("port", 9090)),
        limit=MAX_REQUEST_BYTES,
    )