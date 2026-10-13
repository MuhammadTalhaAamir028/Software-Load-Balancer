"""Asyncio test doubles for pool, balancing, and health-checker tests.

The valid backend modes are ``ok``, ``fail``, ``garbage``, ``hang``, ``stall``,
``close``, and ``big``. Simulate a refused connection with ``stop()``; call
``start()`` again to recover on the same port.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


VALID_MODES = ("ok", "fail", "garbage", "hang", "stall", "close", "big")


class FakeLogger:
    """Record event names and field mappings for tests."""

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._raises = raises

    def event(self, name: str, **fields: Any) -> None:
        """Record an event and optionally raise the configured exception."""
        self.events.append((name, dict(fields)))
        if self._raises is not None:
            raise self._raises

    def names(self) -> list[str]:
        """Return recorded event names in order."""
        return [name for name, _ in self.events]

    def named(self, name: str) -> list[dict[str, Any]]:
        """Return field mappings for events with the requested name."""
        return [dict(fields) for event_name, fields in self.events if event_name == name]


class FakeMetrics:
    """Record health transitions for tests."""

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.transitions: list[tuple[str, bool, str]] = []
        self._raises = raises

    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None:
        """Record a health transition and optionally raise the configured exception."""
        self.transitions.append((backend_id, healthy, reason))
        if self._raises is not None:
            raise self._raises


class FakeBackend:
    """Serve controlled HTTP responses from a local asyncio backend."""

    def __init__(self, mode: str = "ok", port: int = 0) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"unknown fake backend mode: {mode!r}")
        self.mode = mode
        self.hits = 0
        self.host = "127.0.0.1"
        self.port: Optional[int] = None
        self.server: Optional[asyncio.AbstractServer] = None
        self._requested_port = port
        self._handler_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Start listening, reusing the previously assigned port on restart."""
        if self.server is not None:
            return
        requested_port = self._requested_port if self.port is None else self.port
        self.server = await asyncio.start_server(self._handle, self.host, requested_port)
        socket = self.server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        """Stop accepting connections and cancel all active handlers."""
        server = self.server
        if server is None:
            return

        self.server = None
        server.close()
        tasks = list(self._handler_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        self.hits += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            mode = self.mode
            if mode not in VALID_MODES:
                raise ValueError(f"unknown fake backend mode: {mode!r}")
            if mode == "ok":
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\n"
                    b"ok"
                )
                await writer.drain()
            elif mode == "fail":
                writer.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
            elif mode == "garbage":
                writer.write(b"\x00\x01not http")
                await writer.drain()
            elif mode == "hang":
                await asyncio.sleep(3600)
            elif mode == "stall":
                writer.write(b"HTTP/1.1 200 OK\r\n")
                await writer.drain()
                await asyncio.sleep(3600)
            elif mode == "close":
                return
            elif mode == "big":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nX-Pad: "
                    + b"a" * 20000
                    + b"\r\n\r\n"
                )
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
            OSError,
        ):
            pass
        finally:
            writer.close()
            if task is not None:
                self._handler_tasks.discard(task)
