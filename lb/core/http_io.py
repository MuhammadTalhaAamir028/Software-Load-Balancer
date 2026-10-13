"""HTTP/1.1 subset: parse heads, decide body framing, build error responses.

Pure functions and data only. No sockets here, so it is easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

HEAD_LIMIT = 16 * 1024  # bytes; a bigger request head gets 431

Headers = List[Tuple[str, str]]

# Headers that describe ONE hop (client<->LB or LB<->backend), not the whole path.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
}
SUPPORTED_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE"}
KNOWN_UNSUPPORTED = {"OPTIONS", "PATCH", "TRACE", "CONNECT"}  # -> 405, anything else -> 501

REASONS = {
    200: "OK", 400: "Bad Request", 405: "Method Not Allowed", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout", 505: "HTTP Version Not Supported",
}


class HttpError(Exception):
    """The client sent something we reject. `status` is what we answer with."""

    def __init__(self, status: int, msg: str = "") -> None:
        super().__init__(msg or REASONS.get(status, str(status)))
        self.status = status


@dataclass
class Request:
    method: str
    target: str
    version: str                       # "HTTP/1.0" or "HTTP/1.1"
    headers: Headers = field(default_factory=list)
    content_length: int = 0


def header_get(headers: Headers, name: str) -> Optional[str]:
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def connection_tokens(headers: Headers) -> Set[str]:
    out: Set[str] = set()
    for k, v in headers:
        if k.lower() == "connection":
            out.update(t.strip().lower() for t in v.split(",") if t.strip())
    return out


def strip_hop_by_hop(headers: Headers) -> Headers:
    """Drop the fixed hop-by-hop list plus anything the Connection header names."""
    drop = HOP_BY_HOP | connection_tokens(headers)
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _parse_header_lines(lines: List[str]) -> Headers:
    out: Headers = []
    for line in lines:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():  # no colon, or space before colon
            raise HttpError(400, "malformed header line")
        out.append((name, value.strip()))
    return out


def parse_request_head(head: bytes) -> Request:
    """head = bytes up to and including the blank line. Raises HttpError."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise HttpError(400, "malformed request line")
    method, target, version = parts
    if not method.isalpha() or not method.isupper():
        raise HttpError(400, "malformed method")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpError(505 if version.startswith("HTTP/") else 400)
    headers = _parse_header_lines([ln for ln in lines[1:] if ln])

    if header_get(headers, "transfer-encoding") is not None:
        raise HttpError(501, "chunked request bodies are not supported")
    lengths = {v for k, v in headers if k.lower() == "content-length"}
    if len(lengths) > 1:
        raise HttpError(400, "conflicting Content-Length")
    length = 0
    if lengths:
        raw = lengths.pop()
        if not raw.isdigit():
            raise HttpError(400, "bad Content-Length")
        length = int(raw)

    if method not in SUPPORTED_METHODS:
        raise HttpError(405 if method in KNOWN_UNSUPPORTED else 501)
    return Request(method, target, version, headers, length)


def client_wants_keep_alive(req: Request) -> bool:
    """HTTP/1.1 defaults to keep-alive. HTTP/1.0 clients are always closed (design choice)."""
    return req.version == "HTTP/1.1" and "close" not in connection_tokens(req.headers)


def build_request_head(req: Request, client_ip: str) -> bytes:
    """Request head we send to the backend: hop-by-hop gone, X-Forwarded-For added, Connection: close."""
    xff = [v for k, v in req.headers if k.lower() == "x-forwarded-for"]
    xff.append(client_ip)
    lines = [f"{req.method} {req.target} HTTP/1.1"]
    for k, v in strip_hop_by_hop(req.headers):
        if k.lower() not in ("x-forwarded-for", "expect"):  # Expect: we send the body right away
            lines.append(f"{k}: {v}")
    lines.append("X-Forwarded-For: " + ", ".join(xff))
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def parse_response_head(head: bytes) -> Tuple[int, str, Headers]:
    """Returns (status, reason, headers). Raises ValueError if the backend sent junk."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise ValueError("bad status line")
    try:
        headers = _parse_header_lines([ln for ln in lines[1:] if ln])
    except HttpError as e:
        raise ValueError(str(e)) from None
    return int(parts[1]), parts[2] if len(parts) > 2 else "", headers


def response_body_mode(method: str, status: int, headers: Headers) -> Tuple[str, int]:
    """How the response body is framed: ("none"|"chunked"|"length"|"close", length)."""
    if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
        return "none", 0
    te = header_get(headers, "transfer-encoding")
    if te is not None and "chunked" in te.lower():
        return "chunked", 0
    cl = header_get(headers, "content-length")
    if cl is not None:
        if not cl.isdigit():
            raise ValueError("bad Content-Length")
        return "length", int(cl)
    return "close", 0  # body ends when the backend closes


def error_response(status: int, keep_alive: bool = False) -> bytes:
    reason = REASONS.get(status, "Error")
    body = f"{status} {reason}\n".encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: {'keep-alive' if keep_alive else 'close'}\r\n\r\n"
    )
    return head.encode("latin-1") + body
