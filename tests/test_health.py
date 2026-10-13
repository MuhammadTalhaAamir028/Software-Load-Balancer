"""Unit tests for the active health checker."""
from __future__ import annotations

import asyncio
import errno
import gc
import os
import time
import unittest
import warnings
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from unittest import mock

from lb.balancing import make_health_checker, make_pool
from lb.balancing.health import ActiveHealthChecker, reason_from_exception
from lb.config import validate_config
from lb.interfaces import Backend
from tests.fakes import FakeBackend, FakeLogger, FakeMetrics


def config_for(
    backends: Sequence[Tuple[str, Any]],
    interval_s: float = 0.1,
    timeout_s: float = 0.05,
    fail_threshold: int = 3,
    success_threshold: int = 2,
    path: str = "/health",
) -> dict:
    """Build a fresh validated health-check configuration."""
    return validate_config(
        {
            "backends": [
                {"id": backend_id, "host": backend.host, "port": backend.port}
                for backend_id, backend in backends
            ],
            "health": {
                "path": path,
                "interval_s": interval_s,
                "timeout_s": timeout_s,
                "fail_threshold": fail_threshold,
                "success_threshold": success_threshold,
            },
        }
    )


def build(
    backends: Sequence[Tuple[str, FakeBackend]],
    interval_s: float = 0.1,
    timeout_s: float = 0.05,
    fail_threshold: int = 3,
    success_threshold: int = 2,
    path: str = "/health",
    logger: Optional[FakeLogger] = None,
    metrics: Optional[FakeMetrics] = None,
) -> Tuple[Any, ActiveHealthChecker, FakeLogger, FakeMetrics]:
    """Build a pool and checker sharing one logger."""
    actual_logger = logger if logger is not None else FakeLogger()
    actual_metrics = metrics if metrics is not None else FakeMetrics()
    cfg = config_for(
        backends,
        interval_s,
        timeout_s,
        fail_threshold,
        success_threshold,
        path,
    )
    pool = make_pool(cfg, actual_logger, actual_metrics)
    checker = make_health_checker(cfg, pool, actual_logger)
    return pool, checker, actual_logger, actual_metrics


class HarnessLogger(FakeLogger):
    """Logger with event timestamps and optional mode scripts."""

    def __init__(
        self,
        raises: Optional[BaseException] = None,
        scripts: Optional[Dict[str, Tuple[FakeBackend, List[str]]]] = None,
        on_event: Optional[Dict[str, Callable[[Dict[str, Any]], None]]] = None,
    ) -> None:
        super().__init__(raises)
        self.times: List[float] = []
        self.scripts = scripts if scripts is not None else {}
        self.on_event = on_event if on_event is not None else {}
        self._script_indices: Dict[str, int] = {}
        for backend_id, (backend, modes) in self.scripts.items():
            if modes:
                backend.mode = modes[0]
            self._script_indices[backend_id] = 0

    def event(self, name: str, **fields: Any) -> None:
        try:
            timestamp = asyncio.get_running_loop().time()
        except RuntimeError:
            timestamp = time.monotonic()
        self.times.append(timestamp)
        super().event(name, **fields)
        backend_id = fields.get("backend")
        if name == "health_probe" and backend_id in self.scripts:
            backend, modes = self.scripts[backend_id]
            if modes:
                index = self._script_indices[backend_id] + 1
                self._script_indices[backend_id] = index
                backend.mode = modes[min(index, len(modes) - 1)]
        callback = self.on_event.get(name)
        if callback is not None:
            callback(dict(fields))


def events_for(
    logger: FakeLogger,
    backend_id: str,
    names: Iterable[str] = ("health_probe", "backend_down", "backend_up"),
) -> List[str]:
    """Return selected event names for one backend."""
    wanted = set(names)
    return [
        name
        for name, fields in logger.events
        if name in wanted and fields.get("backend") == backend_id
    ]


def probe_events(logger: FakeLogger, backend_id: str) -> List[dict]:
    """Return health-probe fields for one backend."""
    return [
        fields
        for name, fields in logger.events
        if name == "health_probe" and fields.get("backend") == backend_id
    ]


async def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 5.0,
    step: float = 0.01,
) -> None:
    """Poll an event-driven condition without relying on fixed sleeps."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(step)


class RawServer:
    """Small configurable HTTP server for exact probe parsing tests."""

    def __init__(self, reply: bytes, then: str = "close") -> None:
        self.reply = reply
        self.then = then
        self.host = "127.0.0.1"
        self.port: Optional[int] = None
        self.server: Optional[asyncio.AbstractServer] = None
        self.requests: List[bytes] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        if self.server is not None:
            return
        self.server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is None:
            return
        server = self.server
        self.server = None
        server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), 1.0)
        except Exception:
            pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            writer.write(self.reply)
            await writer.drain()
            if self.then == "stall":
                await asyncio.sleep(3600)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            if task is not None:
                self._tasks.discard(task)


async def start_checker(
    checker: ActiveHealthChecker,
    test_case: unittest.IsolatedAsyncioTestCase,
) -> asyncio.Task[Any]:
    """Start a checker and register cancellation-safe cleanup."""
    task = asyncio.ensure_future(checker.run())

    async def cleanup() -> None:
        checker.stop()
        try:
            await asyncio.wait_for(task, 2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    test_case.addAsyncCleanup(cleanup)
    return task


class StubPool:
    """Pool double that deliberately does not log or call metrics."""

    def __init__(self, backends: Sequence[Backend], raises: bool = False) -> None:
        self.backends = list(backends)
        self.calls: List[Tuple[str, bool, str]] = []
        self.raises = raises

    def all(self) -> List[Backend]:
        return list(self.backends)

    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None:
        self.calls.append((backend_id, healthy, reason))
        if self.raises:
            raise RuntimeError("set_health failed")
        for backend in self.backends:
            if backend.id == backend_id:
                backend.healthy = healthy


class TestReasonFromException(unittest.TestCase):
    def test_fixed_exception_reasons(self) -> None:
        cases = [
            (ConnectionRefusedError(), "refused"),
            (ConnectionResetError(), "reset"),
            (asyncio.TimeoutError(), "timeout"),
            (TimeoutError(), "timeout"),
            (ValueError("x"), "error:ValueError"),
            (BrokenPipeError(), "error:BrokenPipeError"),
            (ConnectionAbortedError(), "error:ConnectionAbortedError"),
            (OSError(errno.EIO, "x"), "error:OSError"),
        ]
        for exception, expected in cases:
            with self.subTest(exception=type(exception).__name__):
                actual = reason_from_exception(exception)
                self.assertEqual(actual, expected)
                self.assertIsInstance(actual, str)
                self.assertTrue(actual)

    def test_unreachable_errno_reasons(self) -> None:
        for name in ("EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN"):
            value = getattr(errno, name, None)
            if value is not None:
                with self.subTest(name=name):
                    self.assertEqual(
                        reason_from_exception(OSError(value, "No route to host")),
                        "unreachable",
                    )


class HealthTestCase(unittest.IsolatedAsyncioTestCase):
    async def raw_probe(
        self,
        reply: bytes,
        then: str = "close",
        path: str = "/health",
    ) -> Tuple[RawServer, FakeLogger, List[dict]]:
        server = RawServer(reply, then)
        await server.start()
        self.addAsyncCleanup(server.stop)
        backend = FakeBackend("ok")
        backend.host = server.host
        backend.port = server.port
        logger = FakeLogger()
        checker = ActiveHealthChecker(
            config_for([("raw", backend)], path=path, interval_s=1.0, timeout_s=0.5),
            StubPool([Backend("raw", server.host, int(server.port))]),
            logger,
        )
        await checker._probe(checker._pool.backends[0])
        return server, logger, probe_events(logger, "raw")

    async def one_mode_probe(self, mode: str) -> dict:
        backend = FakeBackend(mode)
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        pool, checker, logger, _ = build([("b1", backend)])
        await checker._probe(pool.all()[0])
        return probe_events(logger, "b1")[-1]

    async def start_one(
        self,
        mode: str = "ok",
        **kwargs: Any,
    ) -> Tuple[FakeBackend, Any, ActiveHealthChecker, FakeLogger, FakeMetrics, asyncio.Task[Any]]:
        backend = FakeBackend(mode)
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        pool, checker, logger, metrics = build([("b1", backend)], **kwargs)
        task = await start_checker(checker, self)
        return backend, pool, checker, logger, metrics, task

    async def test_request_format_and_configured_path(self) -> None:
        server, logger, events = await self.raw_probe(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
            path="/ping",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(
            server.requests,
            [
                (
                    f"GET /ping HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                    "User-Agent: lb-health/1.0\r\nConnection: close\r\n\r\n"
                ).encode()
            ],
        )
        self.assertEqual(set(events[0]), {"backend", "ok", "status", "rtt_ms", "error"})
        self.assertIsInstance(events[0]["rtt_ms"], float)
        self.assertGreaterEqual(events[0]["rtt_ms"], 0.0)

    async def test_success_status_versions_and_reasonless_status(self) -> None:
        for reply in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.0 200 OK\r\n\r\n",
            b"HTTP/1.1 200\r\n\r\n",
        ):
            with self.subTest(reply=reply):
                _, _, events = await self.raw_probe(reply)
                self.assertEqual(events[-1], {
                    "backend": "raw",
                    "ok": True,
                    "status": 200,
                    "rtt_ms": events[-1]["rtt_ms"],
                    "error": None,
                })

    async def test_non_200_statuses(self) -> None:
        for status in (503, 204, 301, 404):
            with self.subTest(status=status):
                _, _, events = await self.raw_probe(
                    f"HTTP/1.1 {status} Status\r\n\r\n".encode()
                )
                self.assertFalse(events[-1]["ok"])
                self.assertEqual(events[-1]["status"], status)
                self.assertEqual(events[-1]["error"], f"bad_status:{status}")

    async def test_malformed_and_empty_responses(self) -> None:
        replies = (
            b"FOO/1.1 200 OK\r\n\r\n",
            b"HTTP/1.1 20 OK\r\n\r\n",
            b"HTTP/1.1 abc OK\r\n\r\n",
            b"\x00\x01not http",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                _, _, events = await self.raw_probe(reply)
                self.assertEqual(events[-1]["error"], "malformed_response")
                self.assertIsNone(events[-1]["status"])
        _, _, events = await self.raw_probe(b"")
        self.assertEqual(events[-1]["error"], "reset")
        self.assertIsNone(events[-1]["status"])

    async def test_large_head_parses_first_line_without_terminator(self) -> None:
        reply = b"HTTP/1.1 200 OK\r\nX-Pad: " + (b"a" * 50000)
        _, _, events = await self.raw_probe(reply, then="stall")
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["status"], 200)
        event = await self.one_mode_probe("big")
        self.assertTrue(event["ok"])
        self.assertEqual(event["status"], 200)

    async def test_fake_backend_mode_reasons(self) -> None:
        expected = {
            "ok": (True, 200, None),
            "fail": (False, 503, "bad_status:503"),
            "garbage": (False, None, "malformed_response"),
            "close": (False, None, "reset"),
            "hang": (False, None, "timeout"),
            "stall": (False, None, "timeout"),
        }
        for mode, result in expected.items():
            with self.subTest(mode=mode):
                event = await self.one_mode_probe(mode)
                self.assertEqual(
                    (event["ok"], event["status"], event["error"]), result
                )

    async def test_exception_mapping_in_probe(self) -> None:
        backend = FakeBackend("ok")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        pool, checker, logger, _ = build([("b1", backend)])
        for exception, expected in (
            (ConnectionRefusedError(), "refused"),
            (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
            (ConnectionResetError(), "reset"),
        ):
            with self.subTest(expected=expected):
                with mock.patch(
                    "asyncio.open_connection",
                    side_effect=exception,
                ):
                    await checker._probe(pool.all()[0])
                self.assertEqual(probe_events(logger, "b1")[-1]["error"], expected)

    async def test_stopped_backend_has_platform_allowed_reason(self) -> None:
        backend = FakeBackend("ok")
        await backend.start()
        pool, checker, logger, _ = build([("b1", backend)])
        await backend.stop()
        event = await checker._probe(pool.all()[0])
        self.assertIn(event[1], {"refused", "timeout", "unreachable"})

    async def test_failure_rtt_reaches_timeout_but_success_is_fast(self) -> None:
        failed = await self.one_mode_probe("hang")
        succeeded = await self.one_mode_probe("ok")
        self.assertGreaterEqual(failed["rtt_ms"], 0.8 * 50.0)
        self.assertLess(succeeded["rtt_ms"], failed["rtt_ms"])

    async def test_down_after_exactly_three_failures(self) -> None:
        backend, pool, checker, logger, metrics, task = await self.start_one("fail")
        await wait_until(lambda: bool(logger.named("backend_down")))
        self.assertEqual(events_for(logger, "b1"), [
            "health_probe", "health_probe", "health_probe", "backend_down"
        ])
        reason = "3 consecutive failures: bad_status:503"
        self.assertEqual(logger.named("backend_down")[0]["reason"], reason)
        self.assertEqual(metrics.transitions, [("b1", False, reason)])
        self.assertNotIn(backend, pool.healthy())
        self.assertFalse(task.done())
        self.assertFalse(pool.all()[0].healthy)

    async def test_recovery_after_exactly_two_successes(self) -> None:
        backend = FakeBackend("fail")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        switched = {"done": False}

        def switch(_: Dict[str, Any]) -> None:
            if not switched["done"]:
                backend.mode = "ok"
                switched["done"] = True

        logger = HarnessLogger(on_event={"backend_down": switch})
        pool, checker, _, _ = build([("b1", backend)], logger=logger)
        await start_checker(checker, self)
        await wait_until(lambda: bool(logger.named("backend_up")))
        names = events_for(logger, "b1")
        down = names.index("backend_down")
        self.assertEqual(names[down + 1:], ["health_probe", "health_probe", "backend_up"])
        self.assertTrue(all(event["ok"] for event in probe_events(logger, "b1")[-2:]))
        self.assertEqual(logger.named("backend_up")[0]["reason"], "recovered")
        self.assertEqual([b.id for b in pool.healthy()], ["b1"])

    async def test_custom_thresholds_are_used(self) -> None:
        backend, pool, checker, logger, _, _ = await self.start_one(
            "fail", fail_threshold=2, success_threshold=1
        )
        await wait_until(lambda: bool(logger.named("backend_down")))
        self.assertEqual(len(probe_events(logger, "b1")), 2)
        self.assertTrue(logger.named("backend_down")[0]["reason"].startswith(
            "2 consecutive failures: "
        ))
        backend.mode = "ok"
        await wait_until(lambda: bool(logger.named("backend_up")))
        self.assertEqual(len(probe_events(logger, "b1")), 3)
        self.assertTrue(pool.all()[0].healthy)

    async def test_flapping_never_drops(self) -> None:
        backend = FakeBackend("fail")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        logger = HarnessLogger(scripts={"b1": (backend, ["fail", "ok"] * 4)})
        pool, checker, _, _ = build([("b1", backend)], logger=logger)
        task = await start_checker(checker, self)
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 8)
        checker.stop()
        await asyncio.wait_for(task, 2.0)
        self.assertEqual(logger.named("backend_down"), [])
        self.assertEqual([b.id for b in pool.healthy()], ["b1"])

    async def test_success_then_failure_resets_recovery_counter(self) -> None:
        backend = FakeBackend("fail")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        logger = HarnessLogger(
            scripts={"b1": (backend, ["fail", "fail", "fail", "ok", "fail", "ok", "ok"])}
        )
        pool, checker, _, _ = build([("b1", backend)], logger=logger)
        await start_checker(checker, self)
        await wait_until(lambda: bool(logger.named("backend_up")))
        self.assertEqual(events_for(logger, "b1"), [
            "health_probe", "health_probe", "health_probe", "backend_down",
            "health_probe", "health_probe", "health_probe", "health_probe",
            "backend_up",
        ])
        self.assertEqual(logger.named("backend_up")[0]["reason"], "recovered")
        self.assertTrue(pool.all()[0].healthy)

    async def test_down_backend_keeps_failing_without_second_transition(self) -> None:
        _, pool, checker, logger, _, _ = await self.start_one("fail")
        await wait_until(lambda: bool(logger.named("backend_down")))
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 6)
        self.assertEqual(len(logger.named("backend_down")), 1)
        self.assertNotIn(pool.all()[0], pool.healthy())

    async def test_counters_are_per_backend_and_order_is_preserved(self) -> None:
        backends = [FakeBackend("ok") for _ in range(4)]
        backends[2].mode = "fail"
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        pool, checker, logger, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends, 1)]
        )
        await start_checker(checker, self)
        await wait_until(lambda: bool(logger.named("backend_down")))
        self.assertEqual([b.id for b in pool.healthy()], ["b1", "b2", "b4"])
        self.assertEqual([b.id for b in pool.all()], ["b1", "b2", "b3", "b4"])

    async def test_checker_only_calls_pool_set_health(self) -> None:
        backend = Backend("b1", "127.0.0.1", 1)
        stub = StubPool([backend])
        logger = FakeLogger()
        cfg = config_for([("b1", backend)])
        checker = ActiveHealthChecker(cfg, stub, logger)
        with mock.patch(
            "asyncio.open_connection", side_effect=ConnectionRefusedError()
        ):
            for _ in range(3):
                await checker._run_round()
        self.assertEqual(
            stub.calls,
            [("b1", False, "3 consecutive failures: refused")],
        )
        self.assertEqual([name for name, _ in logger.events], [
            "health_probe", "health_probe", "health_probe"
        ])

    async def test_stopped_backend_recovers_on_same_port(self) -> None:
        backend, pool, checker, logger, _, _ = await self.start_one("ok")
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 1)
        await backend.stop()
        await wait_until(lambda: bool(logger.named("backend_down")))
        await backend.start()
        await wait_until(lambda: bool(logger.named("backend_up")))
        reason = logger.named("backend_down")[0]["reason"]
        self.assertTrue(reason.startswith("3 consecutive failures: "))
        self.assertIn(reason.rsplit(": ", 1)[1], {"refused", "timeout", "unreachable"})
        self.assertEqual(logger.named("backend_up")[0]["reason"], "recovered")
        self.assertIn(pool.all()[0], pool.healthy())

    async def test_first_round_is_immediate(self) -> None:
        backend = FakeBackend("ok")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        pool, checker, logger, _ = build(
            [("b1", backend)], interval_s=5.0, timeout_s=0.5
        )
        await start_checker(checker, self)
        await wait_until(lambda: bool(probe_events(logger, "b1")), timeout=0.5)
        self.assertEqual(len(pool.all()), 1)

    async def test_hung_backends_are_probed_concurrently(self) -> None:
        backends = [FakeBackend("hang") for _ in range(4)]
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        logger = HarnessLogger()
        pool, checker, _, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends)],
            logger=logger,
        )
        await start_checker(checker, self)
        await wait_until(lambda: len(logger.named("health_probe")) >= 4)
        timestamps = [
            logger.times[index]
            for index, (name, fields) in enumerate(logger.events)
            if name == "health_probe" and fields["backend"] in {"b0", "b1", "b2", "b3"}
        ][:4]
        self.assertLess(max(timestamps) - min(timestamps), 0.04)
        self.assertEqual(len(pool.all()), 4)

    async def test_ok_backend_continues_while_other_backends_hang(self) -> None:
        backends = [FakeBackend("hang") for _ in range(3)] + [FakeBackend("ok")]
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        logger = FakeLogger()
        _, checker, _, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends)],
            logger=logger,
        )
        await start_checker(checker, self)
        await wait_until(lambda: len(probe_events(logger, "b3")) >= 5)
        events = probe_events(logger, "b3")
        self.assertTrue(all(
            (right["rtt_ms"] - left["rtt_ms"]) < 300.0
            for left, right in zip(events, events[1:])
        ))

    async def test_round_gaps_are_approximately_one_interval(self) -> None:
        _, _, checker, logger, _, _ = await self.start_one(
            "ok", logger=HarnessLogger()
        )
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 6)
        timestamped_logger = logger
        times = [
            timestamped_logger.times[index]
            for index, (name, fields) in enumerate(timestamped_logger.events)
            if name == "health_probe" and fields["backend"] == "b1"
        ]
        gaps = [right - left for left, right in zip(times, times[1:])]
        gaps.sort()
        self.assertGreaterEqual(gaps[len(gaps) // 2], 0.05)
        self.assertLessEqual(gaps[len(gaps) // 2], 0.2)
        checker.stop()

    async def test_late_round_resync_skips_missed_ticks(self) -> None:
        backend = FakeBackend("ok")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        logger = HarnessLogger()
        _, checker, _, _ = build([("b1", backend)], logger=logger)
        loop = asyncio.get_running_loop()
        loop.call_later(0.25, time.sleep, 0.35)
        await start_checker(checker, self)
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 8, timeout=2.0)
        times = [
            logger.times[index]
            for index, (name, fields) in enumerate(logger.events)
            if name == "health_probe" and fields["backend"] == "b1"
        ]
        # A missed tick must be discarded, so no catch-up probes occur back-to-back.
        self.assertTrue(all(right - left >= 0.05 for left, right in zip(times, times[1:])))

    async def test_failures_do_not_stop_the_loop(self) -> None:
        _, _, checker, logger, _, task = await self.start_one("fail")
        await wait_until(lambda: bool(logger.named("backend_down")))
        count = len(probe_events(logger, "b1"))
        await wait_until(lambda: len(probe_events(logger, "b1")) >= count + 3)
        self.assertFalse(task.done())

    async def test_stop_interrupts_inflight_probe_without_applying_result(self) -> None:
        backend, pool, checker, logger, _, task = await self.start_one(
            "hang", interval_s=1.0, timeout_s=0.5
        )
        await wait_until(lambda: backend.hits == 1)
        started = time.monotonic()
        checker.stop()
        await asyncio.wait_for(task, 0.3)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(logger.named("backend_down"), [])
        self.assertEqual(pool.all()[0].healthy, True)

    async def test_stop_while_sleeping_is_prompt(self) -> None:
        _, _, checker, logger, _, task = await self.start_one(
            "ok", interval_s=5.0, timeout_s=0.5
        )
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 1)
        checker.stop()
        await asyncio.wait_for(task, 0.3)

    async def test_stop_before_run_and_repeated_stop(self) -> None:
        backend = FakeBackend("ok")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        pool, checker, logger, _ = build([("b1", backend)])
        checker.stop()
        checker.stop()
        await asyncio.wait_for(checker.run(), 1.0)
        checker.stop()
        self.assertEqual(backend.hits, 0)
        self.assertEqual(logger.events, [])
        self.assertEqual([b.id for b in pool.all()], ["b1"])

    async def test_external_cancellation_cleans_checker_tasks(self) -> None:
        _, _, checker, _, _, task = await self.start_one(
            "hang", interval_s=1.0, timeout_s=0.5
        )
        await wait_until(lambda: True, timeout=0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertIsNone(checker._stop_event)

    async def test_no_probes_after_stop(self) -> None:
        backend, _, checker, logger, _, task = await self.start_one("ok")
        await wait_until(lambda: len(probe_events(logger, "b1")) >= 1)
        checker.stop()
        await task
        hits = backend.hits
        await asyncio.sleep(0.3)
        self.assertEqual(backend.hits, hits)

    async def test_unexpected_probe_exception_isolated(self) -> None:
        first = FakeBackend("ok")
        second = FakeBackend("ok")
        await first.start()
        await second.start()
        self.addAsyncCleanup(first.stop)
        self.addAsyncCleanup(second.stop)
        logger = FakeLogger()
        _, checker, _, _ = build([("bad", first), ("good", second)], logger=logger)
        real_open = asyncio.open_connection

        async def selective(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
            if port == first.port:
                raise RuntimeError("boom")
            return await real_open(host, port, *args, **kwargs)

        with mock.patch("asyncio.open_connection", new=selective):
            await start_checker(checker, self)
            await wait_until(lambda: len(probe_events(logger, "good")) >= 4)
        bad_events = probe_events(logger, "bad")
        self.assertTrue(bad_events)
        self.assertTrue(all(event["error"] == "error:RuntimeError" for event in bad_events))
        await wait_until(lambda: bool(logger.named("backend_down")))
        self.assertTrue(logger.named("backend_down")[0]["reason"].endswith("error:RuntimeError"))

    async def test_raising_logger_does_not_kill_checker(self) -> None:
        backend = FakeBackend("fail")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        logger = FakeLogger(RuntimeError("x"))
        pool, checker, _, _ = build([("b1", backend)], logger=logger)
        task = await start_checker(checker, self)
        await wait_until(lambda: not pool.all()[0].healthy)
        count = len(logger.named("health_probe"))
        await wait_until(lambda: len(logger.named("health_probe")) >= count + 3)
        self.assertFalse(task.done())

    async def test_raising_metrics_does_not_kill_recovery(self) -> None:
        backend = FakeBackend("fail")
        await backend.start()
        self.addAsyncCleanup(backend.stop)
        switched = {"done": False}

        def switch(_: Dict[str, Any]) -> None:
            if not switched["done"]:
                backend.mode = "ok"
                switched["done"] = True

        logger = HarnessLogger(on_event={"backend_down": switch})
        metrics = FakeMetrics(RuntimeError("x"))
        pool, checker, _, _ = build(
            [("b1", backend)], logger=logger, metrics=metrics
        )
        task = await start_checker(checker, self)
        await wait_until(lambda: bool(logger.named("backend_up")))
        self.assertFalse(task.done())
        self.assertEqual(logger.named("backend_up")[0]["reason"], "recovered")
        self.assertTrue(pool.all()[0].healthy)

    async def test_raising_stub_pool_does_not_kill_checker(self) -> None:
        backend = Backend("b1", "127.0.0.1", 1)
        stub = StubPool([backend], raises=True)
        logger = FakeLogger()
        checker = ActiveHealthChecker(config_for([("b1", backend)]), stub, logger)
        with mock.patch("asyncio.open_connection", side_effect=ConnectionRefusedError()):
            task = await start_checker(checker, self)
            await wait_until(lambda: len(probe_events(logger, "b1")) >= 6)
        self.assertFalse(task.done())

    async def test_no_resource_warnings_after_many_rounds(self) -> None:
        backends = [FakeBackend(mode) for mode in ("ok", "fail", "hang", "ok")]
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        await backends[-1].stop()
        pool, checker, logger, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends)]
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = await start_checker(checker, self)
            await wait_until(lambda: len(probe_events(logger, "b0")) >= 15)
            checker.stop()
            await task
            gc.collect()
            gc.collect()
        self.assertFalse([warning for warning in caught if warning.category is ResourceWarning])
        self.assertEqual(len(pool.all()), 4)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "Linux /proc fd directory required")
    async def test_no_file_descriptor_growth(self) -> None:
        backends = [FakeBackend(mode) for mode in ("ok", "fail", "hang", "ok")]
        before = len(os.listdir("/proc/self/fd"))
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        await backends[-1].stop()
        _, checker, logger, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends)],
            interval_s=0.02,
            timeout_s=0.01,
        )
        task = await start_checker(checker, self)
        await wait_until(lambda: len(probe_events(logger, "b0")) >= 40, timeout=3.0)
        checker.stop()
        await task
        for backend in backends:
            await backend.stop()
        # Hung server-side connections live until FakeBackend.stop(); measure after teardown.
        await asyncio.sleep(0.2)
        after = len(os.listdir("/proc/self/fd"))
        self.assertLessEqual(after, before + 2)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "Linux /proc fd directory required")
    async def test_no_client_side_fd_growth_while_running(self) -> None:
        def client_socket_count(ports: Sequence[int]) -> int:
            remote_ports = set(ports)
            entries: Dict[str, int] = {}
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table, encoding="ascii") as stream:
                    for line in stream.readlines()[1:]:
                        fields = line.split()
                        entries[fields[9]] = int(fields[2].rsplit(":", 1)[1], 16)
            count = 0
            for fd_name in os.listdir("/proc/self/fd"):
                try:
                    target = os.readlink(os.path.join("/proc/self/fd", fd_name))
                except OSError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    inode = target[8:-1]
                    if entries.get(inode) in remote_ports:
                        count += 1
            return count

        backends = [FakeBackend(mode) for mode in ("ok", "fail", "ok")]
        for backend in backends:
            await backend.start()
            self.addAsyncCleanup(backend.stop)
        await backends[-1].stop()
        before = client_socket_count([backend.port for backend in backends])
        _, checker, logger, _ = build(
            [(f"b{i}", backend) for i, backend in enumerate(backends)],
            interval_s=0.02,
            timeout_s=0.01,
        )
        task = await start_checker(checker, self)
        await wait_until(lambda: len(probe_events(logger, "b0")) >= 40, timeout=3.0)
        await asyncio.sleep(0.1)
        after = client_socket_count([backend.port for backend in backends])
        checker.stop()
        await task
        self.assertLessEqual(after, before + 2)


if __name__ == "__main__":
    unittest.main()
