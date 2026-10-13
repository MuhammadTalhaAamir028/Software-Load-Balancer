# Notes: core (Talha)

Files: `lb/core/http_io.py`, `lb/core/forward.py`, `lb/core/server.py`, `lb/__main__.py`. Topology notes are in `docs/notes_topo.md`.

## One request, from accept to close
1. `serve()` opens a listening socket. Every client connection gets its own coroutine (`_Proxy.handle`).
2. The coroutine waits for a request head (everything up to the blank line). If nothing arrives in `keepalive_s`, the connection is closed.
3. The latency clock starts the moment the head is in. The head is parsed; bad input gets 400/405/431/501 and the connection closes.
4. `pool.healthy()` gives the live backends, `balancer.select()` picks one. `None` means 503.
5. `pool.acquire(b)` bumps `active`. `forward()` runs. `pool.release(b)` is in a `finally`.
6. The response is relayed, `metrics.request_done()` is called exactly once, and a `request` log line is written.
7. If the client may keep the connection, the loop goes back to step 2. Otherwise the socket closes.

## http_io.py (parsing, no sockets)
- Reads the request head into a `Request` (method, target, version, headers, Content-Length).
- Head limit is 16 KB. Only GET, HEAD, POST, PUT, DELETE. OPTIONS, PATCH, TRACE and CONNECT get 405, anything else 501.
- Request bodies are by Content-Length only. A `Transfer-Encoding` request body (chunked) gets 501.
- Two different Content-Length values, or a space before the colon in a header, get 400. This stops request smuggling tricks.
- `strip_hop_by_hop` removes headers that only describe one hop (see below).
- `response_body_mode` decides how the body ends: none (HEAD, 204, 304), chunked, Content-Length, or "until the backend closes".

## forward.py (one request to one backend)
- Opens a NEW TCP connection to the backend for every request (decision D2, no pooling) and sends `Connection: close`.
- Adds `X-Forwarded-For` (appends our client IP to any existing value) and keeps `Host`.
- Streams the request body in 64 KB pieces so a large upload does not sit in memory.
- Relays the response with its own framing, so the client connection can be reused when it is safe.
- Chunked responses are relayed unchanged (documented choice). An HTTP/1.0 client cannot read chunked, so for that client the chunk framing is removed.
- It never raises for network problems. It returns a `Result` and has already written 502/504 to the client if it still could.

## Why hop-by-hop headers are stripped
`Connection`, `Keep-Alive`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade` and `Proxy-*` belong to ONE TCP connection. The client-to-LB connection and the LB-to-backend connection are different connections with different rules (the backend side always closes after one response). Forwarding them would tell the backend to do something meant for the client, or the reverse.

## Why acquire/release sit in a `finally`
`active` is the in-flight count that least-connections reads. If an error or a cancelled task skipped `release`, the count would stay too high forever and that backend would look busier than it is. `finally` runs on success, error and cancellation, so the count always comes back.

## What keep-alive changes
Without it, every request pays a TCP handshake to the LB. With it, one client connection carries many requests, so the server loops. The loop ends on idle timeout, `Connection: close`, an error, or an HTTP/1.0 client (we always close those). The backend side is NOT kept alive.

## Status codes
- **502 Bad Gateway**: we could not get a valid answer from the backend (connection refused, reset, garbage reply).
- **503 Service Unavailable**: the pool has no healthy backend, so nothing was tried.
- **504 Gateway Timeout**: the backend accepted or was slow to answer, and `connect_s` or `read_s` ran out first.
- **499** (internal, never sent): counted in metrics when the client left before we finished.
- If the backend dies halfway through a response, we cannot send a clean error page because the status line is already out. The client connection is closed and it is counted as a failure.

## Shutdown
SIGINT/SIGTERM (Linux) stop the listener, close idle keep-alive connections at once, give in-flight requests `shutdown_grace_s` (default 2 s), then cancel the rest. Windows has no `add_signal_handler`, so Ctrl+C is caught in `__main__` instead.

## Assumptions to confirm with teammates
- `backend_selected` is logged with `level="DEBUG"` as a normal field. Abdur's logger decides whether that filters it.
- `make_balancer` is expected in `lb/balancing/__init__.py`. The contract only says "lb/balancing".
- `serve()` has one extra optional argument `stop` (an `asyncio.Event`). The contract call still works.
- `retry_on_connect_error` is ignored (no retry in v1).

## One failure case
A backend that closes its socket without reading the request body (for example, replying 413 early) makes our body upload hit a reset. The client sees 502 even though the backend did answer. It is rare for our backends, and retrying would hide detection time, so we leave it.
