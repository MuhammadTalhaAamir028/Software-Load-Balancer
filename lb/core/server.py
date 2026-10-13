"""The proxy server: accept connections, run the request loop, shut down cleanly."""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Any, Mapping, Optional, Set

from lb.core.forward import Result, forward
from lb.core.http_io import (
    HEAD_LIMIT, HttpError, client_wants_keep_alive, error_response, parse_request_head,
)

BACKLOG = 1024            # listen queue; the default 100 drops SYNs in a 500-connection burst
DEFAULT_GRACE_S = 2.0     # time given to in-flight requests at shutdown (cfg["shutdown_grace_s"] overrides)


class _Proxy:
    def __init__(self, cfg: Mapping[str, Any], balancer: Any, pool: Any, log: Any, metrics: Any) -> None:
        self.timeouts = cfg["timeouts"]
        self.balancer, self.pool, self.log, self.metrics = balancer, pool, log, metrics
        self.tasks: Set[asyncio.Task] = set()   # one task per client connection
        self.idle: Set[asyncio.Task] = set()    # subset: waiting for the next request head
        self.closing = False                    # set at shutdown: finish the current request, then close

    # ---- one connection -------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "unknown"
        try:
            while await self._one_request(reader, writer, client_ip, task):
                pass
        except asyncio.CancelledError:
            pass  # shutdown; the finally below closes the socket
        except Exception as e:  # one bad client must never take the server down
            self.log.event("client_error", client=client_ip, error=repr(e))
        finally:
            self.tasks.discard(task)
            self.idle.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _one_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           client_ip: str, task: asyncio.Task) -> bool:
        """Serve one request. Returns True if the connection may serve another."""
        self.idle.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.timeouts["keepalive_s"])
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            return False  # idle timeout, or client closed between requests
        except asyncio.LimitOverrunError:
            return await self._reject(writer, client_ip, 431, time.monotonic())
        finally:
            self.idle.discard(task)

        t0 = time.monotonic()  # latency clock starts when the head is in
        try:
            req = parse_request_head(head)
        except HttpError as e:
            return await self._reject(writer, client_ip, e.status, t0)

        backend = self.balancer.select(client_ip, self.pool.healthy())
        if backend is None:
            keep = client_wants_keep_alive(req) and req.content_length == 0  # unread body => must close
            data = error_response(503, keep)
            try:
                writer.write(data)
                await writer.drain()
            except OSError:
                keep = False
            res = Result(status=503, bytes_down=len(data), keep_alive=keep, error="no healthy backends",
                         error_kind="backend")
            backend_id = None
        else:
            backend_id = backend.id
            self.log.event("backend_selected", level="DEBUG", client=client_ip, backend=backend_id,
                           algorithm=getattr(self.balancer, "name", ""))
            self.pool.acquire(backend)
            try:
                res = await forward(req, backend, reader, writer, client_ip, self.timeouts)
            except Exception as e:  # bug guard: still count the request, then close
                res = Result(status=500, error=repr(e), error_kind="backend")
            finally:
                self.pool.release(backend)  # always: `active` must never leak

        latency_ms = (time.monotonic() - t0) * 1000.0
        self.metrics.request_done(backend_id, res.status, latency_ms, res.bytes_up, res.bytes_down, client_ip)
        self.log.event("request", client=client_ip, method=req.method, path=req.target, backend=backend_id,
                       status=res.status, latency_ms=round(latency_ms, 3),
                       bytes_up=res.bytes_up, bytes_down=res.bytes_down)
        if res.error:
            self.log.event("backend_error" if res.error_kind == "backend" else "client_error",
                           client=client_ip, backend=backend_id, status=res.status, error=res.error)
        return res.keep_alive and not self.closing

    async def _reject(self, writer: asyncio.StreamWriter, client_ip: str, status: int, t0: float) -> bool:
        """Answer a request we refuse to forward (400/405/431/501...). Always closes after."""
        data = error_response(status, keep_alive=False)
        try:
            writer.write(data)
            await writer.drain()
        except OSError:
            pass
        latency_ms = (time.monotonic() - t0) * 1000.0
        self.metrics.request_done(None, status, latency_ms, 0, len(data), client_ip)
        self.log.event("request", client=client_ip, method=None, path=None, backend=None, status=status,
                       latency_ms=round(latency_ms, 3), bytes_up=0, bytes_down=len(data))
        return False


async def serve(cfg: Mapping[str, Any], balancer: Any, pool: Any, log: Any, metrics: Any,
                stop: Optional[asyncio.Event] = None) -> None:
    """Run the proxy until `stop` is set (or SIGINT/SIGTERM on Linux), then shut down cleanly.

    `stop` is an optional extra argument (the contract signature still works) so tests
    and __main__ can shut the server down without signals.
    """
    proxy = _Proxy(cfg, balancer, pool, log, metrics)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    listen = cfg["listen"]
    server = await asyncio.start_server(proxy.handle, listen["host"], listen["port"],
                                        limit=HEAD_LIMIT, backlog=BACKLOG)
    signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            signals.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows has no add_signal_handler; Ctrl+C there raises KeyboardInterrupt instead
    log.event("startup", host=listen["host"], port=listen["port"], algorithm=getattr(balancer, "name", ""))
    try:
        await stop.wait()
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        proxy.closing = True
        server.close()                                   # 1. stop accepting
        for t in list(proxy.idle):                       # 2. idle keep-alive connections end now
            t.cancel()
        busy = proxy.tasks - proxy.idle
        if busy:                                         # 3. in-flight requests get a short grace period
            await asyncio.wait(busy, timeout=cfg.get("shutdown_grace_s", DEFAULT_GRACE_S))
        for t in list(proxy.tasks):                      # 4. whatever is left is cancelled
            t.cancel()
        if proxy.tasks:
            await asyncio.gather(*proxy.tasks, return_exceptions=True)
        await server.wait_closed()
        log.event("shutdown")
