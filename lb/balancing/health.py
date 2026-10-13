"""Concurrent active HTTP health checks with hysteresis.

The checker probes every backend immediately and then on monotonic interval
ticks. Three consecutive failures mark a backend down, while two consecutive
successes bring it back up; this hysteresis prevents flapping. The timeout
must be shorter than the interval so probe rounds do not overlap. Detection
normally takes 2 to 3.5 seconds rather than exactly 3 seconds because a kill
can happen anywhere inside a tick interval and a failed probe can consume the
0.5-second timeout. The pool, not this checker, writes ``backend_down`` and
``backend_up`` events and records metrics. Backends start healthy by contract,
so startup failures are admitted until the failure threshold is reached.
For example, ``docker kill`` may produce ``unreachable`` or ``timeout`` rather
than ``refused``; the reason vocabulary covers all three.
"""
from __future__ import annotations

import asyncio
import errno
import re
import socket
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:
    from lb.interfaces import Backend, BackendPool, EventLogger

__all__ = ["ActiveHealthChecker", "reason_from_exception"]

_STATUS_LINE = re.compile(rb"HTTP/1\.\d (\d{3})(?: |$)")
_UNREACHABLE_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTDOWN", None),
    )
    if value is not None
)
_WINDOWS_UNREACHABLE_ERRNOS = frozenset(
    value
    for value in (
        getattr(socket, "WSAEHOSTUNREACH", None),
        getattr(socket, "WSAENETUNREACH", None),
    )
    if value is not None
)
_UNREACHABLE_ERRNOS = _UNREACHABLE_ERRNOS | _WINDOWS_UNREACHABLE_ERRNOS


def reason_from_exception(exc: BaseException) -> str:
    """Map a probe exception to the health-checker's fixed reason vocabulary."""
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, ConnectionResetError):
        return "reset"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _UNREACHABLE_ERRNOS:
        return "unreachable"
    return "error:" + type(exc).__name__


class ActiveHealthChecker:
    """Run concurrent HTTP health probes and apply threshold transitions."""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        pool: BackendPool,
        log: EventLogger,
    ) -> None:
        health = cfg["health"]
        self._path = health["path"]
        self._interval = health["interval_s"]
        self._timeout = health["timeout_s"]
        self._fail_threshold = health["fail_threshold"]
        self._success_threshold = health["success_threshold"]
        self._pool = pool
        self._log = log
        self._fails: dict[str, int] = {}
        self._successes: dict[str, int] = {}
        self._stop_event: Optional[asyncio.Event] = None
        self._stopping = False

    def stop(self) -> None:
        """Request prompt shutdown, including when no run has started."""
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self) -> None:
        """Probe until :meth:`stop` is called or this task is cancelled."""
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        if self._stopping:
            stop_event.set()

        round_task: Optional[asyncio.Task[Any]] = None
        stop_waiter: Optional[asyncio.Task[Any]] = None
        next_tick = loop.time()
        try:
            while not stop_event.is_set():
                round_task = asyncio.create_task(self._run_round())
                stop_waiter = asyncio.create_task(stop_event.wait())
                done, _ = await asyncio.wait(
                    (round_task, stop_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_event.is_set() or stop_waiter in done:
                    await self._cancel_task(round_task)
                    await self._cancel_task(stop_waiter)
                    return

                await self._cancel_task(stop_waiter)
                try:
                    await round_task
                except Exception:
                    pass
                round_task = None
                stop_waiter = None

                next_tick += self._interval
                now = loop.time()
                if next_tick < now - self._interval:
                    next_tick = now + self._interval
                delay = max(0.0, next_tick - now)
                if delay == 0.0:
                    continue
                try:
                    await asyncio.wait_for(stop_event.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._cancel_task(round_task)
            await self._cancel_task(stop_waiter)
            if self._stop_event is stop_event:
                self._stop_event = None

    async def _run_round(self) -> None:
        backends = self._pool.all()
        results = await asyncio.gather(
            *(self._probe(backend) for backend in backends),
            return_exceptions=True,
        )
        for backend, result in zip(backends, results):
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                self._apply(backend, False, "error:" + type(result).__name__)
                continue
            ok, reason, _status, _rtt_ms = result
            self._apply(backend, ok, reason)

    async def _probe(
        self, backend: Backend
    ) -> tuple[bool, Optional[str], Optional[int], float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        writer_holder: list[Optional[asyncio.StreamWriter]] = [None]
        try:
            result = await asyncio.wait_for(
                self._probe_inner(backend, writer_holder),
                self._timeout,
            )
        except Exception as exc:
            reason = reason_from_exception(exc)
            result = (False, reason, None)
        finally:
            writer = writer_holder[0]
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 0.1)
                except Exception:
                    pass

        ok, reason, status = result
        rtt_ms = round((loop.time() - started) * 1000.0, 3)
        self._log_probe(backend.id, ok, status, rtt_ms, reason)
        return ok, reason, status, rtt_ms

    async def _probe_inner(
        self,
        backend: Backend,
        writer_holder: list[Optional[asyncio.StreamWriter]],
    ) -> tuple[bool, Optional[str], Optional[int]]:
        reader, writer = await asyncio.open_connection(backend.host, backend.port)
        writer_holder[0] = writer
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {backend.host}:{backend.port}\r\n"
            "User-Agent: lb-health/1.0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()

        head = bytearray()
        while len(head) < 8192 and b"\r\n\r\n" not in head:
            chunk = await reader.read(min(1024, 8192 - len(head)))
            if not chunk:
                break
            head.extend(chunk)

        if not head:
            return False, "reset", None
        first_line = bytes(head).split(b"\r\n", 1)[0]
        match = _STATUS_LINE.match(first_line)
        if match is None:
            return False, "malformed_response", None
        status = int(match.group(1))
        if status != 200:
            return False, f"bad_status:{status}", status
        return True, None, status

    def _apply(
        self,
        backend: Backend,
        ok: bool,
        reason: Optional[str],
    ) -> None:
        backend_id = backend.id
        if backend.healthy:
            self._successes[backend_id] = 0
            if ok:
                self._fails[backend_id] = 0
                return
            failures = self._fails.get(backend_id, 0) + 1
            self._fails[backend_id] = failures
            if failures >= self._fail_threshold:
                self._fails[backend_id] = 0
                self._set_health(
                    backend_id,
                    False,
                    f"{self._fail_threshold} consecutive failures: {reason}",
                )
            return

        self._fails[backend_id] = 0
        if not ok:
            self._successes[backend_id] = 0
            return
        successes = self._successes.get(backend_id, 0) + 1
        self._successes[backend_id] = successes
        if successes >= self._success_threshold:
            self._successes[backend_id] = 0
            self._set_health(backend_id, True, "recovered")

    def _set_health(self, backend_id: str, healthy: bool, reason: str) -> None:
        try:
            self._pool.set_health(backend_id, healthy, reason)
        except Exception:
            pass

    def _log_probe(
        self,
        backend_id: str,
        ok: bool,
        status: Optional[int],
        rtt_ms: float,
        reason: Optional[str],
    ) -> None:
        try:
            self._log.event(
                "health_probe",
                backend=backend_id,
                ok=ok,
                status=status,
                rtt_ms=rtt_ms,
                error=reason,
            )
        except Exception:
            pass

    @staticmethod
    async def _cancel_task(task: Optional[asyncio.Task[Any]]) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
