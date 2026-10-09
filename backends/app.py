"""Backend HTTP server used by the load-balancer experiments.

A deliberately small HTTP/1.1 server built only on asyncio streams, so that:
  * no third-party packages are required (the Docker image stays minimal), and
  * the artificial delay is non-blocking: while one request is "waiting",
    the same process can still answer other requests, including /health.

Environment variables:
  BACKEND_ID  (required)  short name of this backend, for example "b3".
  DELAY_MS    (optional)  artificial delay for GET / in milliseconds. Default 0.
  PORT        (optional)  TCP port to listen on. Default 8000.

Endpoints:
  GET  /        waits DELAY_MS, then returns a small JSON body.
  POST /        same as GET /, the request body is read and discarded.
  GET  /health  returns "ok" immediately, with no delay (used by health checks).
"""

# This import makes type hints lazy strings, so the file stays compatible with
# Python 3.9 even if newer hint syntax is used in annotations.
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Tunable limits. They protect the server from misbehaving or hostile clients.
# ---------------------------------------------------------------------------

# A connection that sends nothing for this long is closed. Without it, idle
# keep-alive connections would accumulate and eventually exhaust file descriptors.
IDLE_TIMEOUT_S = 30.0

# Maximum length of one header line or of the request line, in bytes.
# The asyncio reader raises an error when a line exceeds this limit.
MAX_LINE_BYTES = 8192

# Maximum number of header lines accepted in a single request.
MAX_HEADERS = 100

# Maximum request body that will be read and discarded (1 MiB).
MAX_BODY_BYTES = 1024 * 1024

# Human-readable reason phrases for the status codes this server can send.
REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
}


class BadRequest(Exception):
    """Raised when the client sends something that is not a valid HTTP request."""


def read_config() -> Tuple[str, int, int]:
    """Read and validate BACKEND_ID, DELAY_MS and PORT from the environment.

    The process stops immediately with a clear message when a value is invalid,
    because a backend running with a wrong delay would silently ruin an experiment.
    """
    backend_id = os.environ.get("BACKEND_ID", "").strip()
    if not backend_id:
        sys.exit("error: environment variable BACKEND_ID is required (for example b3)")

    raw_delay = os.environ.get("DELAY_MS", "0").strip()
    raw_port = os.environ.get("PORT", "8000").strip()

    try:
        delay_ms = int(raw_delay)
    except ValueError:
        sys.exit("error: DELAY_MS must be a whole number of milliseconds, got %r" % raw_delay)
    if delay_ms < 0 or delay_ms > 60000:
        sys.exit("error: DELAY_MS must be between 0 and 60000, got %d" % delay_ms)

    try:
        port = int(raw_port)
    except ValueError:
        sys.exit("error: PORT must be a whole number, got %r" % raw_port)
    if port < 1 or port > 65535:
        sys.exit("error: PORT must be between 1 and 65535, got %d" % port)

    return backend_id, delay_ms, port


def build_response(status: int, body: bytes, content_type: str,
                   backend_id: str, keep_alive: bool) -> bytes:
    """Assemble a complete HTTP/1.1 response (status line, headers, body) as bytes."""
    head = (
        "HTTP/1.1 %d %s\r\n" % (status, REASONS.get(status, "Unknown"))
        + "Content-Type: %s\r\n" % content_type
        # Content-Length must equal the body size in BYTES, not characters.
        # With a correct length the client knows where the response ends,
        # which is what makes keep-alive possible.
        + "Content-Length: %d\r\n" % len(body)
        # Identifies the backend in packet captures and in curl output.
        + "X-Backend-Id: %s\r\n" % backend_id
        + "Connection: %s\r\n" % ("keep-alive" if keep_alive else "close")
        + "\r\n"
    )
    # HTTP headers are defined as ASCII/Latin-1, the body is already bytes.
    return head.encode("latin-1") + body


async def read_request(reader: asyncio.StreamReader
                       ) -> Optional[Tuple[str, str, str, Dict[str, str]]]:
    """Read one HTTP request (line, headers and body) from the connection.

    Returns (method, path, version, headers) or None if the client closed the
    connection cleanly before sending a new request. Raises BadRequest when the
    request is malformed. Header names in the result are lower-cased.
    """
    # Wait for the request line. wait_for enforces the idle timeout and raises
    # asyncio.TimeoutError when the client stays silent for too long.
    try:
        raw_line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
    except ValueError:
        # asyncio raises ValueError when a line is longer than the reader limit.
        raise BadRequest("request line too long")

    # An empty result means end-of-file: the client closed the connection.
    if not raw_line:
        return None

    parts = raw_line.decode("latin-1").strip().split()
    # A valid request line has exactly three parts: METHOD TARGET VERSION.
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest("malformed request line")
    method, target, version = parts

    # Read header lines until the blank line that ends the header section.
    headers: Dict[str, str] = {}
    while True:
        try:
            raw_header = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
        except ValueError:
            raise BadRequest("header line too long")
        if not raw_header:
            # The connection ended in the middle of a request.
            raise BadRequest("unexpected end of headers")
        line = raw_header.decode("latin-1").rstrip("\r\n")
        if line == "":
            break
        if len(headers) >= MAX_HEADERS:
            raise BadRequest("too many headers")
        # Each header is "Name: value". partition splits only at the first colon,
        # so values that contain colons (for example URLs) stay intact.
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise BadRequest("malformed header")
        headers[name.strip().lower()] = value.strip()

    # Chunked request bodies are outside the supported subset (see the PRD).
    if "transfer-encoding" in headers:
        raise BadRequest("transfer-encoding is not supported")

    # Read and discard the body, if any. It must be consumed completely,
    # otherwise its bytes would be mistaken for the next request on a
    # keep-alive connection.
    length_text = headers.get("content-length", "0")
    if not length_text.isdigit():
        raise BadRequest("invalid content-length")
    length = int(length_text)
    if length > MAX_BODY_BYTES:
        raise BadRequest("request body too large")
    if length:
        # readexactly raises IncompleteReadError if the client disconnects early;
        # the caller treats that as a normal disconnect.
        await asyncio.wait_for(reader.readexactly(length), IDLE_TIMEOUT_S)

    # Remove the query string: "/?x=1" must be routed the same as "/".
    path = target.split("?", 1)[0]
    return method, path, version, headers


async def handle_connection(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter,
                            backend_id: str, delay_ms: int) -> None:
    """Serve one client connection, handling requests in a loop (keep-alive)."""
    try:
        while True:
            try:
                request = await read_request(reader)
            except BadRequest as exc:
                # Answer 400 and close: after a parse error the position in the
                # byte stream is unknown, so the connection cannot be reused.
                body = ("bad request: %s\n" % exc).encode("utf-8")
                writer.write(build_response(400, body, "text/plain", backend_id, False))
                await writer.drain()
                return

            if request is None:
                return  # Client closed the connection normally.

            method, path, version, headers = request

            # Decide whether the connection stays open after this response.
            # HTTP/1.1 defaults to keep-alive unless the client sends
            # "Connection: close". HTTP/1.0 defaults to close unless the
            # client explicitly asks for keep-alive.
            connection_header = headers.get("connection", "").lower()
            if version == "HTTP/1.1":
                keep_alive = "close" not in connection_header
            else:
                keep_alive = "keep-alive" in connection_header

            if path == "/health" and method == "GET":
                # Health checks must stay fast, so there is deliberately no sleep.
                status, body, ctype = 200, b"ok", "text/plain"
            elif path == "/" and method in ("GET", "POST"):
                # asyncio.sleep suspends only THIS request. The event loop keeps
                # serving other connections, so /health stays responsive while
                # many requests are waiting. time.sleep would block everything.
                await asyncio.sleep(delay_ms / 1000.0)
                payload = {
                    "backend": backend_id,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "delay_ms": delay_ms,
                }
                status = 200
                body = json.dumps(payload).encode("utf-8")
                ctype = "application/json"
            elif path in ("/", "/health"):
                # The path exists but the method is not allowed for it.
                status, body, ctype = 405, b"method not allowed\n", "text/plain"
            else:
                status, body, ctype = 404, b"not found\n", "text/plain"

            writer.write(build_response(status, body, ctype, backend_id, keep_alive))
            # drain waits until the data has been handed to the operating system;
            # it raises a ConnectionError if the client has already gone away.
            await writer.drain()

            if not keep_alive:
                return
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        # Client disconnected (also during the artificial delay) or went idle.
        # These are normal events and need no log line (per-request logging
        # would distort the CPU-limited backends).
        pass
    except Exception:
        # Last line of defence: one bad client must never crash the server.
        pass
    finally:
        # Always release the socket. A second error here (client already gone)
        # is ignored on purpose.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    """Start the server and run until the process receives SIGINT or SIGTERM."""
    backend_id, delay_ms, port = read_config()

    # A wrapper function binds the per-process settings to the callback that
    # asyncio calls for every new connection with (reader, writer).
    async def on_connect(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, backend_id, delay_ms)

    # "0.0.0.0" means "listen on every network interface of the container",
    # which is required so that other containers can reach this backend.
    # limit sets the maximum line length; backlog is the queue of connections
    # waiting to be accepted (raised because benchmarks open 500 at once).
    server = await asyncio.start_server(
        on_connect, host="0.0.0.0", port=port, limit=MAX_LINE_BYTES, backlog=1024
    )
    print("backend %s listening on :%d delay=%dms" % (backend_id, port, delay_ms), flush=True)

    # "docker stop" sends SIGTERM. A Python process running as PID 1 in a
    # container ignores it by default, so Docker would wait 10 s and then kill
    # it. Registering a handler lets the server exit immediately instead.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support loop signal handlers; Ctrl+C is handled below.
            pass

    async with server:
        await stop.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass