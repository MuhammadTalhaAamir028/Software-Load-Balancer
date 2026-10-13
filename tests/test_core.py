"""Tests for lb/core. Fakes only: no teammate modules are imported.

Run: python -m unittest tests.test_core -v   (or pytest tests/test_core.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sys
import unittest
from typing import Any, List, Optional, Tuple

from lb.core.http_io import HttpError, parse_request_head, strip_hop_by_hop
from lb.core.server import serve
from lb.interfaces import Backend

try:
    import resource  # Linux/macOS only
except ImportError:
    resource = None


# ---------- fakes ----------
class FakePool:
    def __init__(self, backends: List[Backend]) -> None:
        self.backends = backends

    def healthy(self) -> List[Backend]:
        return [b for b in self.backends if b.healthy]

    def all(self) -> List[Backend]:
        return list(self.backends)

    def acquire(self, b: Backend) -> None:
        b.active += 1

    def release(self, b: Backend) -> None:
        b.active -= 1

    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None:
        pass


class FirstBalancer:
    name = "first"

    def select(self, client_ip: str, backends: Any) -> Optional[Backend]:
        return backends[0] if backends else None


class FakeLog:
    def __init__(self) -> None:
        self.events: List[Tuple[str, dict]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.events.append((name, fields))


class FakeMetrics:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def request_done(self, *args: Any) -> None:
        self.calls.append(args)

    def health_transition(self, *a: Any) -> None:
        pass

    def snapshot(self) -> dict:
        return {}


# ---------- helpers ----------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def read_response(reader: asyncio.StreamReader, method: str = "GET", timeout: float = 5):
    """Parse one response from the LB: returns (status, headers dict lower-cased, body)."""
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
    body = b""
    if method != "HEAD" and "content-length" in headers:
        body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 5)
    elif headers.get("transfer-encoding") == "chunked":
        while True:
            size = int((await reader.readline()).split(b";")[0], 16)
            if size == 0:
                await reader.readline()
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
    return status, headers, body


async def backend_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Replies with JSON describing exactly what it received."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
    body = await reader.readexactly(int(headers.get("content-length", 0)))
    out = json.dumps({"request_line": lines[0], "headers": headers,
                      "body_len": len(body), "sha": hashlib.sha256(body).hexdigest()}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nKeep-Alive: timeout=5\r\n"
                 b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(out) + out)
    await writer.drain()
    writer.close()


class CoreCase(unittest.IsolatedAsyncioTestCase):
    """Starts the LB on a free port in front of one fake backend (set self.backend_handler)."""

    timeouts = {"connect_s": 1.0, "read_s": 1.0, "keepalive_s": 2.0}

    async def asyncSetUp(self) -> None:
        self.servers: List[asyncio.AbstractServer] = []
        self.accepted = 0
        self.log, self.metrics = FakeLog(), FakeMetrics()
        self.lb_task: Optional[asyncio.Task] = None
        self.stop = asyncio.Event()

    async def asyncTearDown(self) -> None:
        self.stop.set()
        if self.lb_task:
            await asyncio.wait_for(self.lb_task, 10)
        for s in self.servers:
            s.close()
            await s.wait_closed()

    async def start_backend(self, handler) -> Backend:
        async def counted(r, w):
            self.accepted += 1
            try:
                await handler(r, w)
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                w.close()  # py3.12 wait_closed() waits for every connection to end

        srv = await asyncio.start_server(counted, "127.0.0.1", 0)
        self.servers.append(srv)
        return Backend("b1", "127.0.0.1", srv.sockets[0].getsockname()[1])

    async def start_lb(self, backends: List[Backend], timeouts: Optional[dict] = None) -> int:
        self.pool = FakePool(backends)
        self.port = free_port()
        cfg = {"listen": {"host": "127.0.0.1", "port": self.port},
               "timeouts": timeouts or self.timeouts, "shutdown_grace_s": 1.0}
        self.lb_task = asyncio.create_task(
            serve(cfg, FirstBalancer(), self.pool, self.log, self.metrics, stop=self.stop))
        for _ in range(100):  # wait until it accepts
            try:
                _, w = await asyncio.open_connection("127.0.0.1", self.port)
                w.close()
                break
            except OSError:
                await asyncio.sleep(0.02)
        return self.port

    async def client(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def assertIdle(self) -> None:
        """Test 8: pool.active is back to 0 for every backend (allow a moment for cleanup)."""
        for _ in range(100):
            if all(b.active == 0 for b in self.pool.all()):
                return
            await asyncio.sleep(0.02)
        self.fail(f"active leaked: {[(b.id, b.active) for b in self.pool.all()]}")

    def statuses(self) -> List[int]:
        return [c[1] for c in self.metrics.calls]


# ---------- 1. forwarding ----------
class TestForwarding(CoreCase):
    async def test_get_and_post_forwarded(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"GET /hello?x=1 HTTP/1.1\r\nHost: lb\r\nConnection: keep-alive\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n")
        status, _, body = await read_response(r)
        got = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(got["request_line"], "GET /hello?x=1 HTTP/1.1")
        self.assertEqual(got["headers"]["x-forwarded-for"], "1.2.3.4, 127.0.0.1")  # appended
        self.assertEqual(got["headers"]["host"], "lb")                              # kept
        self.assertEqual(got["headers"]["connection"], "close")                     # new conn per request
        w.close()

        r, w = await self.client()
        payload = b"hello body" * 10
        w.write(b"POST /submit HTTP/1.1\r\nHost: lb\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)
        status, headers, body = await read_response(r)
        got = json.loads(body)
        self.assertEqual((status, got["body_len"], got["sha"]),
                         (200, len(payload), hashlib.sha256(payload).hexdigest()))
        self.assertNotIn("keep-alive", {k for k in headers if k == "keep-alive"})  # hop-by-hop stripped
        w.close()
        await self.assertIdle()

    async def test_keep_alive_three_requests_one_connection(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b])
        r, w = await self.client()
        for i in range(3):
            w.write(b"GET /%d HTTP/1.1\r\nHost: lb\r\n\r\n" % i)
            status, headers, body = await read_response(r)
            self.assertEqual(status, 200)
            self.assertEqual(headers["connection"], "keep-alive")
            self.assertEqual(json.loads(body)["request_line"], f"GET /{i} HTTP/1.1")
        self.assertEqual(self.accepted, 3)  # one client connection, three backend connections (no pooling)
        w.close()
        await self.assertIdle()

    async def test_http10_client_is_closed(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"GET / HTTP/1.0\r\n\r\n")
        status, headers, _ = await read_response(r)
        self.assertEqual((status, headers["connection"]), (200, "close"))
        self.assertEqual(await r.read(), b"")  # server closed
        w.close()

    async def test_large_body(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b], {"connect_s": 1.0, "read_s": 5.0, "keepalive_s": 5.0})
        payload = bytes(range(256)) * 20_000  # ~5 MB
        r, w = await self.client()
        w.write(b"POST /big HTTP/1.1\r\nHost: lb\r\nContent-Length: %d\r\n\r\n" % len(payload))
        w.write(payload)
        await w.drain()
        status, _, body = await read_response(r)
        got = json.loads(body)
        self.assertEqual((status, got["body_len"], got["sha"]),
                         (200, len(payload), hashlib.sha256(payload).hexdigest()))
        w.close()
        await self.assertIdle()

    async def test_chunked_response_head_and_close_delimited(self):
        async def chunked(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
            await w.drain()
            w.close()

        async def until_close(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\n\r\nno length here")
            await w.drain()
            w.close()

        async def head_reply(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n")  # HEAD: length but no body
            await w.drain()
            w.close()

        b = await self.start_backend(chunked)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, headers, body = await read_response(r)
        self.assertEqual((status, body, headers["connection"]), (200, b"hello world", "keep-alive"))
        w.close()

        # HTTP/1.0 client gets the chunk framing removed
        r, w = await self.client()
        w.write(b"GET / HTTP/1.0\r\n\r\n")
        raw = await asyncio.wait_for(r.read(), 5)
        self.assertTrue(raw.endswith(b"\r\n\r\nhello world"), raw)
        w.close()

        b.port = (await self.start_backend(until_close)).port
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, headers, _ = await read_response(r)
        rest = await asyncio.wait_for(r.read(), 5)
        self.assertEqual((status, headers["connection"], rest), (200, "close", b"no length here"))
        w.close()

        b.port = (await self.start_backend(head_reply)).port
        r, w = await self.client()
        w.write(b"HEAD / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, headers, body = await read_response(r, "HEAD")
        self.assertEqual((status, body, headers["content-length"], headers["connection"]),
                         (200, b"", "999", "keep-alive"))
        w.close()
        await self.assertIdle()


# ---------- 3, 5. error paths ----------
class TestBackendErrors(CoreCase):
    async def test_502_connection_refused(self):
        dead = Backend("b1", "127.0.0.1", free_port())  # nothing listens here
        # generous connect_s: Windows takes ~1.5 s to report 'refused' on loopback
        await self.start_lb([dead], {"connect_s": 5.0, "read_s": 1.0, "keepalive_s": 2.0})
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, headers, _ = await read_response(r)
        self.assertEqual(status, 502)
        self.assertEqual(headers["connection"], "keep-alive")  # request fully read: connection reusable
        w.close()
        await self.assertIdle()
        self.assertIn("backend_error", [n for n, _ in self.log.events])

    async def test_503_no_healthy_backend(self):
        b = Backend("b1", "127.0.0.1", free_port(), healthy=False)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, _, _ = await read_response(r)
        self.assertEqual(status, 503)
        w.close()
        await self.assertIdle()
        self.assertEqual(self.metrics.calls[0][0], None)  # no backend id

    async def test_504_slow_backend_never_answers(self):
        async def silent(r, w):
            await r.readuntil(b"\r\n\r\n")
            await asyncio.sleep(5)

        b = await self.start_backend(silent)
        await self.start_lb([b], {"connect_s": 1.0, "read_s": 0.3, "keepalive_s": 2.0})
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        t = asyncio.get_running_loop().time()
        status, _, _ = await read_response(r)
        self.assertEqual(status, 504)
        self.assertLess(asyncio.get_running_loop().time() - t, 2.0)
        w.close()
        await self.assertIdle()

    async def test_backend_stalls_mid_body_closes_client(self):
        async def stall(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")
            await w.drain()
            await asyncio.sleep(5)

        b = await self.start_backend(stall)
        await self.start_lb([b], {"connect_s": 1.0, "read_s": 0.3, "keepalive_s": 2.0})
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        data = await asyncio.wait_for(r.read(), 3)  # LB must close; no 5 s hang
        self.assertTrue(data.endswith(b"partial"))
        w.close()
        await self.assertIdle()
        self.assertEqual(self.statuses(), [504])  # counted as a failure even though the head said 200

    async def test_backend_garbage_is_502(self):
        async def junk(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"this is not http\r\n\r\n")
            await w.drain()
            w.close()

        b = await self.start_backend(junk)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        status, _, _ = await read_response(r)
        self.assertEqual(status, 502)
        w.close()
        await self.assertIdle()


# ---------- 6. bad client input ----------
class TestBadRequests(CoreCase):
    async def _raw(self, data: bytes) -> int:
        b = Backend("b1", "127.0.0.1", free_port())
        await self.start_lb([b])
        r, w = await self.client()
        w.write(data)
        status, _, _ = await read_response(r)
        self.assertEqual(await asyncio.wait_for(r.read(), 3), b"")  # then closed
        w.close()
        await self.assertIdle()
        return status

    async def test_malformed_request_line(self):
        self.assertEqual(await self._raw(b"NONSENSE\r\n\r\n"), 400)

    async def test_oversized_header_431(self):
        self.assertEqual(await self._raw(b"GET / HTTP/1.1\r\nX-Big: " + b"a" * 20_000 + b"\r\n\r\n"), 431)

    async def test_chunked_request_body_501(self):
        self.assertEqual(await self._raw(
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"), 501)

    async def test_unsupported_methods(self):
        self.assertEqual(await self._raw(b"PATCH / HTTP/1.1\r\nHost: x\r\n\r\n"), 405)

    async def test_every_rejected_request_is_counted_once(self):
        await self._raw(b"NONSENSE\r\n\r\n")
        self.assertEqual(len(self.metrics.calls), 1)

    def test_parser_units(self):
        with self.assertRaises(HttpError):
            parse_request_head(b"GET / HTTP/1.1\r\nBad Header : x\r\n\r\n")
        with self.assertRaises(HttpError):
            parse_request_head(b"POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n")
        req = parse_request_head(b"POST /a HTTP/1.1\r\nContent-Length: 7\r\n\r\n")
        self.assertEqual((req.method, req.content_length), ("POST", 7))
        kept = strip_hop_by_hop([("Connection", "X-Foo, close"), ("X-Foo", "1"), ("Host", "h"),
                                 ("Keep-Alive", "5"), ("Upgrade", "websocket")])
        self.assertEqual(kept, [("Host", "h")])


# ---------- 7. client disconnects ----------
class TestClientGone(CoreCase):
    async def test_client_disconnect_mid_response(self):
        async def firehose(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 200000000\r\n\r\n")
            try:
                for _ in range(20000):
                    w.write(b"x" * 65536)
                    await w.drain()
            finally:
                w.close()

        b = await self.start_backend(firehose)
        await self.start_lb([b], {"connect_s": 1.0, "read_s": 5.0, "keepalive_s": 5.0})
        r, w = await self.client()
        w.write(b"GET / HTTP/1.1\r\nHost: lb\r\n\r\n")
        await r.readexactly(100)
        w.transport.abort()  # RST mid-response
        await self.assertIdle()
        for _ in range(100):
            if self.metrics.calls:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.metrics.calls), 1)
        self.assertEqual(self.metrics.calls[0][1], 499)

    async def test_client_disconnects_mid_request_body(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b])
        r, w = await self.client()
        w.write(b"POST / HTTP/1.1\r\nHost: lb\r\nContent-Length: 1000\r\n\r\nonly a little")
        await w.drain()
        await asyncio.sleep(0.1)
        w.close()
        await self.assertIdle()
        for _ in range(100):
            if self.metrics.calls:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.metrics.calls), 1)

    async def test_idle_keepalive_connection_times_out(self):
        b = await self.start_backend(backend_echo)
        await self.start_lb([b], {"connect_s": 1.0, "read_s": 1.0, "keepalive_s": 0.3})
        r, w = await self.client()
        self.assertEqual(await asyncio.wait_for(r.read(), 3), b"")  # LB closed the idle connection
        w.close()


# ---------- 9. concurrency ----------
@unittest.skipIf(
    resource is not None and resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 4096,
    "needs `ulimit -n 65535` (500 clients + 500 proxy + 500 backend sockets in one process)")
class TestConcurrency(CoreCase):
    async def test_500_concurrent_connections(self):
        async def slow_ok(r, w):
            await r.readuntil(b"\r\n\r\n")
            await asyncio.sleep(8.0)  # debug-mode Windows accepts ~70 conn/s; hold long enough to overlap
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await w.drain()
            w.close()

        b = await self.start_backend(slow_ok)
        peak = 0

        class CountingPool(FakePool):  # samples the LB's own in-flight counter
            def acquire(self, backend: Backend) -> None:
                nonlocal peak
                super().acquire(backend)
                peak = max(peak, backend.active)

        self.pool = CountingPool([b])
        self.port = free_port()
        cfg = {"listen": {"host": "127.0.0.1", "port": self.port},
               "timeouts": {"connect_s": 10.0, "read_s": 30.0, "keepalive_s": 30.0}}
        self.lb_task = asyncio.create_task(
            serve(cfg, FirstBalancer(), self.pool, self.log, self.metrics, stop=self.stop))
        await asyncio.sleep(0.3)

        # IsolatedAsyncioTestCase runs the loop in debug mode; on Windows that is slow enough that a burst
        # of SYNs overflows the accept queue and is refused (outside debug mode all 500 connect at once).
        # Linux would retransmit the SYN, so the client retries a refused connect.
        ramp = asyncio.Semaphore(50)

        async def connect():
            for _ in range(100):
                try:
                    return await asyncio.open_connection("127.0.0.1", self.port)
                except ConnectionRefusedError:
                    await asyncio.sleep(0.05)
            return await asyncio.open_connection("127.0.0.1", self.port)

        async def one() -> int:
            async with ramp:
                r, w = await connect()
            w.write(b"GET / HTTP/1.1\r\nHost: lb\r\nConnection: close\r\n\r\n")
            status, _, body = await read_response(r, timeout=30)
            w.close()
            return status if body == b"ok" else -1

        results = await asyncio.gather(*(one() for _ in range(500)))
        self.assertEqual(results.count(200), 500)
        self.assertGreaterEqual(peak, 400, "requests did not really overlap")
        await self.assertIdle()
        self.assertEqual(len(self.metrics.calls), 500)


if __name__ == "__main__":
    unittest.main()
