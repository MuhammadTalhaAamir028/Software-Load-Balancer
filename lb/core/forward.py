"""Forward ONE request to ONE backend and relay the response to the client.

A new backend TCP connection is opened per request (design decision D2, no pooling).
`forward` never raises for network problems: it returns a Result and, when it
can still do so, has already written a 502/504 to the client.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from lb.core.http_io import (
    HEAD_LIMIT, Request, build_request_head, client_wants_keep_alive, error_response,
    parse_response_head, response_body_mode, strip_hop_by_hop,
)

CHUNK = 64 * 1024


@dataclass
class Result:
    status: int = 0                  # what we count: backend status, or 502/503/504, or 499 if the client left
    bytes_up: int = 0                # bytes written to the backend
    bytes_down: int = 0              # bytes written to the client
    keep_alive: bool = False         # may the client connection be reused?
    error: Optional[str] = None


class _BackendError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


class _ClientGone(Exception):
    """The client side broke (closed, reset, or stopped sending its body)."""


async def _to_client(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        writer.write(data)
        await writer.drain()
    except OSError as e:  # ConnectionError is a subclass
        raise _ClientGone(str(e)) from e


async def _backend_io(coro: Any, timeout: float) -> Any:
    """Await a backend read/write; turn every failure into a _BackendError."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        raise _BackendError(504, "backend timed out") from None
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, OSError) as e:
        raise _BackendError(502, f"backend error: {e!r}") from None


async def _relay_chunked(src: asyncio.StreamReader, dst: asyncio.StreamWriter, timeout: float) -> int:
    """Copy a chunked body to the client unchanged, up to and including the last chunk and trailers."""
    sent = 0
    while True:
        line = await _backend_io(src.readline(), timeout)
        if not line.endswith(b"\n"):
            raise _BackendError(502, "backend closed inside chunked body")
        try:
            size = int(line.split(b";")[0].strip(), 16)
        except ValueError:
            raise _BackendError(502, "bad chunk size") from None
        await _to_client(dst, line)
        sent += len(line)
        if size == 0:
            while True:  # trailers, up to the blank line
                trailer = await _backend_io(src.readline(), timeout)
                if trailer:
                    await _to_client(dst, trailer)
                    sent += len(trailer)
                if trailer in (b"\r\n", b"\n", b""):
                    return sent
        data = await _backend_io(src.readexactly(size + 2), timeout)  # chunk + its CRLF
        await _to_client(dst, data)
        sent += len(data)


async def _relay_length(src: asyncio.StreamReader, dst: asyncio.StreamWriter, n: int, timeout: float) -> int:
    left = n
    while left:
        chunk = await _backend_io(src.read(min(left, CHUNK)), timeout)
        if not chunk:
            raise _BackendError(502, "backend closed before Content-Length bytes")
        await _to_client(dst, chunk)
        left -= len(chunk)
    return n


async def _relay_until_close(src: asyncio.StreamReader, dst: asyncio.StreamWriter, timeout: float) -> int:
    sent = 0
    while True:
        chunk = await _backend_io(src.read(CHUNK), timeout)
        if not chunk:
            return sent
        await _to_client(dst, chunk)
        sent += len(chunk)


async def forward(req: Request, backend: Any, client_reader: asyncio.StreamReader,
                  client_writer: asyncio.StreamWriter, client_ip: str,
                  timeouts: Mapping[str, float]) -> Result:
    res = Result()
    t_connect, t_read = timeouts["connect_s"], timeouts["read_s"]
    client_ka = client_wants_keep_alive(req)
    body_left = req.content_length          # request body bytes still unread from the client
    head_sent = False
    bwriter: Optional[asyncio.StreamWriter] = None
    try:
        # 1. connect (refused/unreachable -> 502, connect timeout -> 504)
        try:
            breader, bwriter = await asyncio.wait_for(
                asyncio.open_connection(backend.host, backend.port, limit=HEAD_LIMIT), t_connect)
        except asyncio.TimeoutError:
            raise _BackendError(504, "backend connect timed out") from None
        except OSError as e:
            raise _BackendError(502, f"backend connect failed: {e!r}") from None

        # 2. send request head, then stream the body
        head = build_request_head(req, client_ip)
        bwriter.write(head)
        res.bytes_up += len(head)
        while body_left:
            try:
                chunk = await asyncio.wait_for(client_reader.read(min(body_left, CHUNK)), t_read)
            except (asyncio.TimeoutError, OSError) as e:
                raise _ClientGone(f"client stopped sending body: {e!r}") from None
            if not chunk:
                raise _ClientGone("client closed during request body")
            body_left -= len(chunk)
            bwriter.write(chunk)
            res.bytes_up += len(chunk)
            await _backend_io(bwriter.drain(), t_read)
        await _backend_io(bwriter.drain(), t_read)

        # 3. response head (no 1xx handling: we never send Expect, so the backend has no reason to)
        raw = await _backend_io(breader.readuntil(b"\r\n\r\n"), t_read)
        try:
            status, reason, headers = parse_response_head(raw)
            mode, length = response_body_mode(req.method, status, headers)
        except ValueError as e:
            raise _BackendError(502, f"bad backend response: {e}") from None

        # 4. send head to the client, then relay the body with the right framing
        keep = client_ka and mode != "close"
        out = [f"HTTP/1.1 {status} {reason}"]
        out += [f"{k}: {v}" for k, v in strip_hop_by_hop(headers)]
        if mode == "chunked":
            out.append("Transfer-Encoding: chunked")
        out.append(f"Connection: {'keep-alive' if keep else 'close'}")
        data = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")
        await _to_client(client_writer, data)
        head_sent = True
        res.status = status
        res.bytes_down += len(data)

        if mode == "length":
            res.bytes_down += await _relay_length(breader, client_writer, length, t_read)
        elif mode == "chunked":
            res.bytes_down += await _relay_chunked(breader, client_writer, t_read)
        elif mode == "close":
            res.bytes_down += await _relay_until_close(breader, client_writer, t_read)
        res.keep_alive = keep and body_left == 0

    except _BackendError as e:
        res.status, res.error = e.status, str(e)
        if not head_sent:  # a half-sent response cannot be fixed with an error page; we just close
            keep = client_ka and body_left == 0
            try:
                body = error_response(e.status, keep)
                await _to_client(client_writer, body)
                res.bytes_down += len(body)
                res.keep_alive = keep
            except _ClientGone:
                pass
    except _ClientGone as e:
        res.status, res.error = 499, str(e)  # 499 = client left; server.py logs it as client_error
    finally:
        if bwriter is not None:
            bwriter.close()
    return res
