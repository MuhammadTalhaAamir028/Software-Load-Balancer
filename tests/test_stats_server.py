"""Tests for lb.obs.stats_server (routes, merging, robustness).

Run from the repository root:  pytest -q tests/test_stats_server.py
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from lb.obs import stats_server
from lb.obs.metrics import MetricsCollector
from lb.obs.stats_server import build_stats, start_stats_server

SCHEMA_TOP = ["uptime_s", "algorithm", "total_requests", "errors", "latency_ms",
              "per_backend", "health_events"]
SCHEMA_BACKEND = ["requests", "errors", "active", "healthy", "bytes_up", "bytes_down"]


class FakePool:
    """Stands in for BackendPool: only all() is used by the stats server."""

    def __init__(self, backends):
        self._backends = backends

    def all(self):
        return list(self._backends)


def backend(bid, healthy=True, active=0):
    return SimpleNamespace(id=bid, healthy=healthy, active=active)


def make_env():
    cfg = {"algorithm": "least_conn", "stats": {"host": "127.0.0.1", "port": 0}}
    metrics = MetricsCollector(percentile_cache_s=0)
    pool = FakePool([backend("b1", True, 2), backend("b2", True, 0),
                     backend("b3", False, 0)])
    return cfg, metrics, pool


async def raw_request(port: int, payload: bytes, wait: float = 3.0) -> bytes:
    """Send raw bytes and read until the server closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), wait)
    writer.close()
    return data


def split_response(data: bytes):
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:])}
    return status, headers, body


async def get(port: int, path: str):
    data = await raw_request(port, ("GET %s HTTP/1.1\r\nHost: x\r\n\r\n" % path).encode())
    return split_response(data)


def run_with_server(scenario, cfg=None, metrics=None, pool=None):
    """Start the server on a free port, run scenario(port), always shut down."""
    d_cfg, d_metrics, d_pool = make_env()
    cfg, metrics, pool = cfg or d_cfg, metrics or d_metrics, pool or d_pool

    async def main():
        server = await start_stats_server(cfg, metrics, pool)
        port = server.sockets[0].getsockname()[1]
        try:
            return await scenario(port)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def test_stats_schema_and_merge():
    cfg, metrics, pool = make_env()
    metrics.request_done("b1", 200, 10.0, 100, 200, "c")
    metrics.request_done("b1", 502, 20.0, 100, 0, "c")
    metrics.request_done(None, 503, 1.0, 0, 0, "c")
    metrics.health_transition("b3", False, "3 failures")

    async def scenario(port):
        return await get(port, "/stats")

    status, headers, body = run_with_server(scenario, cfg, metrics, pool)
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["connection"] == "close"
    assert int(headers["content-length"]) == len(body)

    data = json.loads(body)
    assert list(data) == SCHEMA_TOP
    assert data["algorithm"] == "least_conn"
    assert data["total_requests"] == 3 and data["errors"] == 2
    assert list(data["per_backend"]) == ["b1", "b2", "b3"]          # pool order
    assert list(data["per_backend"]["b1"]) == SCHEMA_BACKEND
    assert data["per_backend"]["b1"]["active"] == 2                  # from the pool
    assert data["per_backend"]["b1"]["errors"] == 1                  # from metrics
    assert data["per_backend"]["b3"]["healthy"] is False
    # Never-used backends appear with zeros.
    assert data["per_backend"]["b2"] == {"requests": 0, "errors": 0, "active": 0,
                                         "healthy": True, "bytes_up": 0, "bytes_down": 0}
    assert data["health_events"][0]["backend"] == "b3"


def test_backend_missing_from_pool_still_listed():
    cfg, metrics, pool = make_env()
    metrics.request_done("old-backend", 200, 1.0, 1, 1, "c")
    data = build_stats(cfg, metrics, pool)
    assert data["per_backend"]["old-backend"]["healthy"] is False
    assert list(data["per_backend"])[-1] == "old-backend"           # appended after the pool


def test_default_dashboard_path_points_to_the_real_file():
    # The brief requires the page at lb/obs/dashboard/index.html.
    assert stats_server.DASHBOARD_PATH.is_file()
    assert stats_server.DASHBOARD_PATH.parts[-4:] == ("lb", "obs", "dashboard", "index.html")
    assert b"/stats" in stats_server.DASHBOARD_PATH.read_bytes()


def test_package_exports_exactly_the_three_factories():
    import lb.obs
    assert sorted(lb.obs.__all__) == ["make_logger", "make_metrics", "start_stats_server"]


def test_dashboard_is_served_at_root(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><title>demo</title>", encoding="utf-8")
    monkeypatch.setattr(stats_server, "DASHBOARD_PATH", page)

    async def scenario(port):
        return await get(port, "/")

    status, headers, body = run_with_server(scenario)
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert body == page.read_bytes()


def test_dashboard_is_read_only_once(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text("first", encoding="utf-8")
    monkeypatch.setattr(stats_server, "DASHBOARD_PATH", page)

    async def scenario(port):
        page.write_text("second", encoding="utf-8")                   # changed after start
        return await get(port, "/")

    assert run_with_server(scenario)[2] == b"first"


def test_missing_dashboard_file_does_not_stop_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_server, "DASHBOARD_PATH", tmp_path / "nope.html")

    async def scenario(port):
        return await get(port, "/"), await get(port, "/stats")

    (root_status, _, root_body), (stats_status, _, _) = run_with_server(scenario)
    assert root_status == 200 and b"not found" in root_body
    assert stats_status == 200


def test_healthz_and_query_string():
    async def scenario(port):
        return await get(port, "/healthz"), await get(port, "/stats?x=1")

    (h_status, _, h_body), (s_status, _, _) = run_with_server(scenario)
    assert (h_status, h_body) == (200, b"ok")
    assert s_status == 200


def test_404_and_405():
    async def scenario(port):
        nf = await get(port, "/nope")
        post = split_response(await raw_request(
            port, b"POST /stats HTTP/1.1\r\nContent-Length: 0\r\n\r\n"))
        delete = split_response(await raw_request(
            port, b"DELETE /nope HTTP/1.1\r\n\r\n"))
        return nf, post, delete

    nf, post, delete = run_with_server(scenario)
    assert nf[0] == 404
    assert post[0] == 405 and post[1]["allow"] == "GET"
    assert delete[0] == 405                                           # non-GET wins over 404


def test_garbage_and_oversized_requests_leave_server_alive():
    async def scenario(port):
        bad = split_response(await raw_request(port, b"GARBAGE\r\n\r\n"))
        huge = split_response(await raw_request(
            port, b"GET / HTTP/1.1\r\nX-Big: " + b"a" * 20000 + b"\r\n\r\n"))
        after = await get(port, "/healthz")
        return bad, huge, after

    bad, huge, after = run_with_server(scenario)
    assert bad[0] == 400
    assert huge[0] == 431
    assert after[0] == 200


def test_stalled_client_is_dropped_and_others_still_served(monkeypatch):
    monkeypatch.setattr(stats_server, "REQUEST_TIMEOUT_S", 0.3)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stats HTTP/1.1\r\nHost: x\r\n")            # never finishes
        await writer.drain()
        other = await get(port, "/healthz")                            # served meanwhile
        stalled = await asyncio.wait_for(reader.read(), 2.0)           # server closes it
        writer.close()
        return other, stalled

    other, stalled = run_with_server(scenario)
    assert other[0] == 200
    assert stalled == b""                                              # closed without a reply


def test_client_that_disconnects_immediately_is_harmless():
    async def scenario(port):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await asyncio.sleep(0.1)
        return await get(port, "/healthz")

    assert run_with_server(scenario)[0] == 200


def test_failing_pool_gives_500_and_server_survives():
    class BrokenPool:
        def all(self):
            raise RuntimeError("pool exploded")

    cfg, metrics, _ = make_env()

    async def scenario(port):
        return await get(port, "/stats"), await get(port, "/healthz")

    broken, healthy = run_with_server(scenario, cfg, metrics, BrokenPool())
    assert broken[0] == 500
    assert json.loads(broken[2]) == {"error": "stats unavailable"}
    assert healthy[0] == 200


def test_many_concurrent_requests():
    async def scenario(port):
        return await asyncio.gather(*(get(port, "/stats") for _ in range(100)))

    results = run_with_server(scenario)
    assert all(r[0] == 200 for r in results)