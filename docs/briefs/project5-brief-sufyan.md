# BRIEF FOR SUFYAN'S AI (v3, expanded): Balancing, Pool, Health Checks, Config
Version v3, 2026-10-09. Replaces v2. Same scope, more detail and a few corrections. Changes are in section 0.

## 0. How to use this brief
- Read sections 1-4 first. They explain what you are building and where it sits.
- Build in the order in section 15.
- Section 18 (Rules) and section 19 (Not to build) are mandatory.
- If something is unclear, write the question in your report-back. Do not guess silently.

**What changed from v2:**
1. Fixed a contradiction. The **pool** writes the `backend_down`/`backend_up` log events and calls `metrics.health_transition`. The **health checker** only calls `pool.set_health`. (v2 section 3 showed the checker logging too, which would double-log.)
2. New failure reason `unreachable` (host or network unreachable). Reason: the benchmark kills a container with `docker kill`, and the network may answer with "no route to host" or with nothing, not with "connection refused".
3. New reserved config key `retry_on_connect_error` (bool, default false). The core does not use it yet. It exists so that a later group decision does not break the strict config loader.
4. Code skeletons for the pool and the health checker (section 8 and 9). Follow the logic, you may restructure.
5. Real IP-hash numbers for our four client IPs (section 7). Add them to the tests and the notes.
6. Probe cleanup rule: never wait on `writer.wait_closed()` without a timeout.
7. Extra tests (section 11) and a worked timeline for the kill (section 9).

## 1. Context
Group project for a Computer Networks course (CS-3001, Project 5). We are building a Python asyncio HTTP/1.1 **load balancer** (L7 reverse proxy) that spreads client requests across 3-4 backend servers of unequal capacity. It has three algorithms (round robin, least connections, IP-hash) and active health checks. It is benchmarked against HAProxy on a routed topology. Every result must be a measured number backed by packet traces.

Three people work in parallel behind a **frozen interface file** (`lb/interfaces.py`, tag `lb-contract-v1`). You own: the three algorithms, the backend pool, the health checker, and the config loader.

Your code decides two things the instructor will grade:
1. **Load distribution** (requests per backend). It comes from your algorithms.
2. **Failure detection time**. It comes from your health checker.
Be precise. Small timing bugs become wrong numbers in the final report.

## 2. Load balancer primer (only what you need)
- **Reverse proxy.** Clients connect to the LB. The LB picks one backend per request and relays the response.
- **Healthy set.** The backends currently believed alive. Algorithms only ever see this set.
- **Active count.** `Backend.active` = requests currently in flight to that backend. The core increments it with `pool.acquire()` and decrements it with `pool.release()`.
- **Why three algorithms.** Our backends have unequal capacity (artificial delay 5 / 10 / 25 / 50 ms and CPU limits 1.0 / 1.0 / 0.5 / 0.25).

| Algorithm | Rule | Strength | Weakness |
|---|---|---|---|
| Round robin | Next backend in turn | Simple, even request counts | Ignores capacity. The slow backend gets the same share, so p95 latency suffers |
| Least connections | Backend with fewest in-flight requests | Adapts to unequal capacity. Fast backends finish sooner, stay low, get more | Needs accurate `active` counts |
| IP-hash | Hash of client IP picks the backend | Same client always lands on the same backend (stickiness) | Uneven with few clients. Mapping reshuffles when the healthy set changes |

- **Active health check.** The LB probes each backend itself (`GET /health`) instead of waiting for a user request to fail.
- **Thresholds.** DOWN after **3** consecutive failures. UP again after **2** consecutive successes. Different numbers on purpose: they stop a flapping backend from bouncing in and out (hysteresis).
- **Detection time.** About `fail_threshold x interval`. With a 1 s interval and 3 failures, expect 2 to 3.5 s. The probe timeout (0.5 s) must be shorter than the interval (1 s), so probe rounds never overlap.

## 3. Where your code sits (one request, one probe)
```
client --> [Talha: serve()]
              1. pool.healthy()                        --> [your Pool]
              2. balancer.select(client_ip, healthy)   --> [your Balancer]
              3. pool.acquire(b) ... forward ... pool.release(b)

[your HealthChecker] --every 1 s--> GET /health on each backend
        --> pool.set_health(id, healthy, reason)       (the checker's ONLY output)
              [your Pool], on a real state change:
                  --> log.event("backend_down"/"backend_up", ...)   (Abdur's logger)
                  --> metrics.health_transition(...)                (Abdur's metrics)
        --> log.event("health_probe", ...)  (DEBUG, from the checker, per probe)
```
- Everything runs on **one asyncio event loop thread**. No locks needed. Never block: no `time.sleep`, no synchronous sockets.
- `select()` is on the hot path. It runs once per request, possibly thousands per second. Keep it O(n) with n at most 8. No logging, no allocation beyond what is needed, no I/O.

## 4. Your files
```
lb/config.py
lb/balancing/__init__.py      # exports exactly: make_balancer, make_pool, make_health_checker
lb/balancing/round_robin.py
lb/balancing/least_conn.py
lb/balancing/ip_hash.py
lb/balancing/pool.py
lb/balancing/health.py
config.example.json
tests/test_config.py
tests/test_balancing.py
tests/test_pool.py
tests/test_health.py
tests/demo_distribution.py    # evidence script, section 11
tests/fakes.py                # FakeLogger, FakeMetrics, FakeBackend, section 11
docs/notes_balancing.md
docs/test_results_balancing.txt
```

## 5. Frozen interface (do not edit; conform to it)
```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Sequence, Optional, Mapping, Any

@dataclass
class Backend:
    id: str
    host: str
    port: int
    healthy: bool = True
    active: int = 0

class Balancer(Protocol):
    name: str
    def select(self, client_ip: str, backends: Sequence[Backend]) -> Optional[Backend]: ...
    # backends = HEALTHY only. None if empty. Must not block. No I/O.

class BackendPool(Protocol):
    def healthy(self) -> list[Backend]: ...
    def all(self) -> list[Backend]: ...
    def acquire(self, b: Backend) -> None: ...     # active += 1
    def release(self, b: Backend) -> None: ...     # active -= 1
    def set_health(self, backend_id: str, healthy: bool, reason: str) -> None: ...

class HealthChecker(Protocol):
    async def run(self) -> None: ...   # loops until stop()
    def stop(self) -> None: ...

class EventLogger(Protocol):
    def event(self, name: str, **fields: Any) -> None: ...

class Metrics(Protocol):
    def health_transition(self, backend_id: str, healthy: bool, reason: str) -> None: ...
    # (other methods exist; you only call this one)
```
Factories you implement:
- `make_balancer(name: str) -> Balancer`. Names: `"round_robin"`, `"least_conn"`, `"ip_hash"`. Unknown name raises `ValueError`. Returns a **new instance every call** (no shared state, no singleton).
- `make_pool(cfg: Mapping, log: EventLogger, metrics: Metrics) -> BackendPool`
- `make_health_checker(cfg: Mapping, pool: BackendPool, log: EventLogger) -> HealthChecker`
- `load_config(path: str) -> dict` in `lb/config.py`, plus `class ConfigError(ValueError)`.

## 6. Config loader spec (you own config for the whole project)
Other modules receive a plain `dict`. They trust that your loader validated it. Format: **JSON is canonical** (standard library). YAML (`.yaml`/`.yml`) is accepted only if `import yaml` succeeds; otherwise raise `ConfigError("install PyYAML or use a .json config")`. `lb/` must stay standard-library only.

| Key | Type | Required | Default | Rule |
|---|---|---|---|---|
| `listen.host` | str | no | `"0.0.0.0"` | non-empty |
| `listen.port` | int | no | `8080` | 1-65535 |
| `algorithm` | str | no | `"round_robin"` | one of the three names |
| `retry_on_connect_error` | bool | no | `false` | must be a real bool. Reserved: the core ignores it unless the group decides to build it |
| `backends` | list | yes | none | 1 to 8 entries |
| `backends[].id` | str | yes | none | unique, regex `^[A-Za-z0-9_-]{1,16}$` |
| `backends[].host` | str | yes | none | non-empty |
| `backends[].port` | int | yes | none | 1-65535 |
| `health.path` | str | no | `"/health"` | starts with `/` |
| `health.interval_s` | float | no | `1.0` | > 0 |
| `health.timeout_s` | float | no | `0.5` | > 0 and < `interval_s` |
| `health.fail_threshold` | int | no | `3` | >= 1 |
| `health.success_threshold` | int | no | `2` | >= 1 |
| `timeouts.connect_s` | float | no | `2.0` | > 0 |
| `timeouts.read_s` | float | no | `10.0` | > 0 |
| `timeouts.keepalive_s` | float | no | `15.0` | > 0 |
| `logging.path` | str | no | `"logs/lb.jsonl"` | non-empty |
| `logging.level` | str | no | `"INFO"` | DEBUG, INFO, WARNING or ERROR |
| `stats.host` | str | no | `"0.0.0.0"` | non-empty |
| `stats.port` | int | no | `9090` | 1-65535 and not equal to `listen.port` |

Rules:
- Unknown keys at any level raise an error (catches typos like `fail_treshold`).
- Reject booleans where an int is expected (`True` is an `int` in Python). Accept ints where a float is expected. Reject a bool where a float is expected.
- Error messages name the path and the problem: `health.timeout_s: must be less than interval_s (got 1.5 >= 1.0)`.
- Return a **deep copy** with all defaults filled in. Never mutate the parsed input.
- Missing file, empty file, invalid JSON: all raise `ConfigError` with a clear message, not a raw traceback.
- Collect and report **all** errors in one message when practical (one per line), not only the first.
- Abdur's benchmark scripts generate one config per algorithm from a template. They rely on your strictness. Do not loosen it.

Valid example (`config.example.json`):
```json
{
  "listen": {"host": "0.0.0.0", "port": 8080},
  "algorithm": "round_robin",
  "backends": [
    {"id": "b1", "host": "10.0.3.11", "port": 8000},
    {"id": "b2", "host": "10.0.3.12", "port": 8000},
    {"id": "b3", "host": "10.0.3.13", "port": 8000},
    {"id": "b4", "host": "10.0.3.14", "port": 8000}
  ],
  "health": {"path": "/health", "interval_s": 1.0, "timeout_s": 0.5, "fail_threshold": 3, "success_threshold": 2},
  "timeouts": {"connect_s": 2.0, "read_s": 10.0, "keepalive_s": 15.0},
  "logging": {"path": "logs/lb.jsonl", "level": "INFO"},
  "stats": {"host": "0.0.0.0", "port": 9090}
}
```

## 7. Algorithm specs

**Common:** `select()` is pure with respect to its inputs. It never mutates a `Backend`. It returns `None` for an empty list. No logging, no I/O. The input list is in stable config order (decision D13).

**Round robin** (`name = "round_robin"`)
```python
class RoundRobin:
    name = "round_robin"
    def __init__(self) -> None:
        self._i = 0
    def select(self, client_ip, backends):
        if not backends:
            return None
        b = backends[self._i % len(backends)]
        self._i += 1
        return b
```
- Example, 4 healthy: b1, b2, b3, b4, b1, b2 ...
- After b3 is removed (list is b1, b2, b4) the counter keeps going. Over 3000 further selects each backend gets 1000 (plus or minus 1).

**Least connections** (`name = "least_conn"`)
```python
return min(backends, key=lambda b: b.active)   # min() returns the first minimum, so ties follow list order
```
- Example: active = [3, 1, 1, 2] for [b1, b2, b3, b4]. Result is b2 (tie with b3 broken by order).
- The core uses one backend connection per request, so `active` equals open backend connections.

**IP-hash** (`name = "ip_hash"`)
```python
import hashlib, ipaddress

def _norm(ip: str) -> str:
    try:
        return ipaddress.ip_address(ip).compressed
    except ValueError:
        return ip

def _h(ip: str) -> int:
    return int.from_bytes(hashlib.sha256(_norm(ip).encode()).digest()[:8], "big")

# select: backends[_h(client_ip) % len(backends)]
```
- Use `hashlib`, never built-in `hash()` (it changes per process, which breaks stickiness across restarts).
- Same IP gives the same backend while the healthy set is unchanged.
- **Known behaviour, document it, do not fix it:** when the healthy set changes size, the mapping reshuffles. (Consistent hashing would fix that. It is an official optional feature. Do not build it.)
- **Known behaviour, document it, do not fix it:** our four client IPs and four backends collide. With the code above (SHA-256, first 8 bytes, modulo list length) the mapping is:

| Client | 4 healthy (b1,b2,b3,b4) | b3 down (b1,b2,b4) |
|---|---|---|
| 10.0.1.11 | b3 | b1 |
| 10.0.1.12 | b2 | b1 |
| 10.0.1.13 | b2 | b2 |
| 10.0.1.14 | b2 | b2 |

So with 4 healthy backends, b2 receives three of the four clients, b3 one, and b1 and b4 none. Add a unit test that asserts this exact table (it also guards against anyone changing the hash by accident). Do **not** tune the hash to look even. We report it as a finding. Note for the report: after the b3 kill, c1's traffic (the clients that were on b3) moves to b1, and c2 moves from b2 to b1 (reshuffle).

## 8. Pool spec and skeleton
`Pool(cfg, log, metrics)`:
- Builds one `Backend` object per `cfg["backends"]` entry, in config order, `healthy=True`, `active=0`.
- The `Backend` objects are **shared references**. The core and your health checker see the same objects. Mutate in place. Never replace them.
- `all()`: new list, all backends, config order.
- `healthy()`: new list, `[b for b in all if b.healthy]`, config order. Always a `list`, not a generator.
- `acquire(b)`: `b.active += 1`.
- `release(b)`: `b.active -= 1`, clamped at 0. If it would go below 0, clamp and call `log.event("backend_error", backend=b.id, error="release_underflow")`.
- `set_health(id, healthy, reason)`:
  - Unknown id: raise `KeyError`.
  - Same state as now: do nothing. No log, no metrics call.
  - Real change: set `b.healthy`, then `log.event("backend_down" or "backend_up", backend=id, reason=reason)`, then `metrics.health_transition(id, healthy, reason)`.
- A backend that just went DOWN may still get `acquire()` from a request selected a moment earlier. That is allowed. In-flight requests finish normally. Never cancel them.

Skeleton:
```python
class Pool:
    def __init__(self, cfg, log, metrics):
        self._log, self._metrics = log, metrics
        self._backends = [Backend(e["id"], e["host"], e["port"]) for e in cfg["backends"]]
        self._by_id = {b.id: b for b in self._backends}

    def all(self):     return list(self._backends)
    def healthy(self): return [b for b in self._backends if b.healthy]

    def acquire(self, b): b.active += 1
    def release(self, b):
        if b.active <= 0:
            b.active = 0
            self._log.event("backend_error", backend=b.id, error="release_underflow")
        else:
            b.active -= 1

    def set_health(self, backend_id, healthy, reason):
        b = self._by_id[backend_id]          # KeyError on unknown id
        if b.healthy == healthy:
            return
        b.healthy = healthy
        self._log.event("backend_up" if healthy else "backend_down", backend=backend_id, reason=reason)
        self._metrics.health_transition(backend_id, healthy, reason)
```

## 9. Health checker spec and skeleton

**State:** per backend, two integer counters: `fails` and `successes`. The pool (`b.healthy`) is the source of truth for the current state.

| Current | Probe result | Action |
|---|---|---|
| healthy | ok | `fails = 0` |
| healthy | fail | `fails += 1`. If `fails >= fail_threshold`: `set_health(id, False, reason)`, `fails = 0` |
| down | ok | `successes += 1`. If `successes >= success_threshold`: `set_health(id, True, "recovered")`, `successes = 0` |
| down | fail | `successes = 0` |

**Probe (standard library only, `asyncio.open_connection`):**
```
GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: lb-health/1.0\r\nConnection: close\r\n\r\n
```
- Read until `\r\n\r\n` or EOF, at most 8 KB. Parse the first line only (`HTTP/1.x <code> ...`).
- Success = status 200. Everything else is failure.
- The **whole probe** (connect + send + read) runs under `asyncio.wait_for(..., timeout_s)`.
- Always close the writer in a `finally`. Do **not** `await writer.wait_closed()` without its own short timeout (for example 0.1 s), because a half-dead peer can make it hang and would delay the next round.
- Measure `rtt_ms` with `loop.time()`.

**Failure reasons (fixed vocabulary, used in logs and the report):**
| Reason | When |
|---|---|
| `refused` | `ConnectionRefusedError` (TCP reset on connect) |
| `unreachable` | `OSError` with errno `EHOSTUNREACH`, `ENETUNREACH` or `EHOSTDOWN` |
| `reset` | `ConnectionResetError` or EOF before the first line |
| `timeout` | `asyncio.TimeoutError` (whole probe exceeded `timeout_s`) |
| `bad_status:<code>` | HTTP status not 200 |
| `malformed_response` | first line is not `HTTP/1.x <3 digits>` |
| `error:<ExceptionName>` | anything else |

The DOWN transition reason is `f"{fail_threshold} consecutive failures: {last_reason}"`.

**Probe log line (DEBUG):** `log.event("health_probe", backend=id, ok=bool, status=int|None, rtt_ms=float, error=str|None)`. For failures, `rtt_ms` is the time until the failure.

**Scheduling:**
- First round runs immediately at start, then every `interval_s`.
- Compute ticks from `loop.time()` (monotonic): `next_tick += interval_s`. Sleep `max(0, next_tick - now)`.
- If a round ever runs late by more than one interval, resync to now. Skip missed ticks. Never fire a burst to catch up.
- All backends are probed **concurrently** each round (`asyncio.gather(..., return_exceptions=True)`). A hung backend must not delay probing the others.
- One failing or crashing probe must never stop the loop. Catch everything per probe.
- `stop()` sets an event. `run()` returns promptly, cancelling in-flight probes.

Skeleton:
```python
class Checker:
    def __init__(self, cfg, pool, log):
        h = cfg["health"]
        self._path, self._iv, self._to = h["path"], h["interval_s"], h["timeout_s"]
        self._ft, self._st = h["fail_threshold"], h["success_threshold"]
        self._pool, self._log = pool, log
        self._fails, self._succ = {}, {}
        self._stop = None                     # create asyncio.Event inside run() (Python 3.9: bound to the running loop)
        self._stopping = False

    def stop(self):
        self._stopping = True
        if self._stop is not None:
            self._stop.set()

    async def run(self):
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        if self._stopping: self._stop.set()
        nxt = loop.time()
        while not self._stop.is_set():
            await self._round()
            nxt += self._iv
            now = loop.time()
            if nxt < now - self._iv:          # ran more than one interval late: resync, no burst
                nxt = now + self._iv
            delay = max(0.0, nxt - now)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _round(self):
        backends = self._pool.all()
        results = await asyncio.gather(*(self._probe(b) for b in backends), return_exceptions=True)
        for b, r in zip(backends, results):
            ok, reason = (False, "error:" + type(r).__name__) if isinstance(r, BaseException) else r
            self._apply(b, ok, reason)

    # _probe(b) -> (ok: bool, reason: str|None): open_connection under wait_for, send request,
    # read head (<= 8 KB), parse status; map exceptions to the reason table; always close writer.
    # _apply(b, ok, reason): the state table above; call pool.set_health only on a threshold crossing.
```
(`stop()` before `run()` must not crash and must make `run()` return at once.)

**Worked timeline for the benchmark kill (why detection takes 2 to 3.5 s, and what to expect):**
- Probe ticks at 0, 1, 2, 3 s. The kill lands at a random moment inside one tick interval.
- If the failure is instant (`refused`, `unreachable`): first failed probe 0 to 1 s after the kill, third failure 2 s after that. DOWN at **2 to 3 s** after the kill.
- If the failure is a hang (`timeout`): each failed probe also costs 0.5 s, and the third result arrives at up to **3.5 s**.
- **Important for the benchmark:** `docker kill` removes the container's network namespace. The network may then give "no route to host" after ARP fails, or silence. Expect `unreachable` or `timeout` more than `refused`. Your reason vocabulary covers all of them. Do not assume `refused`. Abdur will record what actually happens at Checkpoint 2.
- Recovery: backend restarts, first successful probe within 1 s, second 1 s later. UP about 1 to 2 s after the restart.

**Startup state:** backends begin `healthy=True` (contract default). A backend dead at startup keeps receiving requests until 3 probes fail (about 3 s). Document this. Do not change it.

## 10. Edge cases you must handle
1. Empty healthy list: all three algorithms return `None`.
2. One healthy backend: all three return it.
3. All backends down: pool `healthy()` returns `[]`. No exceptions.
4. Backend flaps (fail, ok, fail, ok): never reaches DOWN, because success resets `fails`.
5. Backend down, then one success, then a failure: `successes` resets to 0.
6. `set_health` called twice with the same state: second call is a silent no-op.
7. Probe to a closed port (refused): reason `refused`, instant failure.
8. Probe to a backend that accepts and never replies: reason `timeout` after `timeout_s`, other probes unaffected.
9. Probe gets `HTTP/1.1 503`: reason `bad_status:503`.
10. Probe gets garbage bytes or a closed socket mid-response: `malformed_response` or `reset`.
11. `release()` more times than `acquire()`: clamp at 0, log `release_underflow`.
12. `acquire()` on a DOWN backend: allowed.
13. Client IP is IPv6, has odd formatting (`::ffff:10.0.1.11`), or is empty: IP-hash must not crash.
14. Duplicate backend ids, bad ports, missing `backends`: `ConfigError`.
15. `stop()` called before `run()` starts or twice: no crash.
16. 10,000 probe rounds: no growth in open file descriptors.
17. A backend that accepts, sends the status line, then stalls: still ends in `timeout` (the whole probe is under one timeout).
18. A very large response to `/health` (more than 8 KB): read stops at 8 KB, status still parsed from the first line.
19. The log or metrics object raises inside `set_health`: the exception must not kill the checker loop (catch around the call, count nothing, continue). The state change still stands.

## 11. Tests, fixtures and evidence

**Fixtures** (`tests/fakes.py`):
```python
import asyncio

class FakeLogger:
    def __init__(self): self.events = []
    def event(self, name, **fields): self.events.append((name, fields))

class FakeMetrics:
    def __init__(self): self.transitions = []
    def health_transition(self, backend_id, healthy, reason):
        self.transitions.append((backend_id, healthy, reason))

class FakeBackend:
    """Local asyncio server. mode: ok | fail | hang | garbage | stall. 'refused' = call stop()."""
    def __init__(self, mode="ok"):
        self.mode, self.hits, self.server, self.port = mode, 0, None, None
    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
    async def _handle(self, r, w):
        self.hits += 1
        try:
            await r.readuntil(b"\r\n\r\n")
            if self.mode == "hang":
                await asyncio.sleep(3600)
            elif self.mode == "stall":
                w.write(b"HTTP/1.1 200 OK\r\n"); await w.drain(); await asyncio.sleep(3600)
            elif self.mode == "ok":
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            elif self.mode == "fail":
                w.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            elif self.mode == "garbage":
                w.write(b"\x00\x01not http")
            await w.drain()
        finally:
            w.close()
    async def stop(self):
        self.server.close(); await self.server.wait_closed()
```
- Use short timings in tests: `interval_s=0.1`, `timeout_s=0.05`. The whole suite should run in under about 15 s.
- Use `unittest.IsolatedAsyncioTestCase` (stdlib) or pytest. Both fine.
- Use `hang` and `stall` handlers carefully: cancel their sleeps in `stop()` so the test ends cleanly.

**Required tests (names are suggestions):**
- `test_config`: valid loads; defaults filled; each bad case in section 10 item 14; unknown key at top level and nested; bool for int; bool for float; `timeout_s >= interval_s`; same port for listen and stats; `retry_on_connect_error` accepts `true` and `false` and rejects `"yes"`; YAML without PyYAML gives a clear error; input dict not mutated.
- `test_balancing`: RR even split over 1000 selects; RR fair after removal; LC picks minimum; LC tie deterministic; IP-hash stable over 100 calls; **IP-hash exact table from section 7 (both columns)**; IP-hash spreads 1000 random IPs across 4 backends (each between 15% and 35%); IP-hash stable across processes (spawn a subprocess and compare); empty list returns `None` (all three); IPv6 and IPv4-mapped input; `make_balancer("nope")` raises `ValueError`; two `make_balancer` calls return independent instances; `select` does not mutate backends.
- `test_pool`: build from config in order; acquire/release balance; clamp at 0 and log; `set_health` event only on change; unknown id `KeyError`; `healthy()` returns a copy; shared object identity; DOWN then UP logs `backend_down` then `backend_up` with the reason.
- `test_health`: DOWN after exactly 3 failures, not 2; UP after exactly 2 successes, not 1; flapping never drops; reasons for refused, 503, garbage, hang, stall; `unreachable` mapping (unit-test the exception-to-reason function with a fake `OSError(errno.EHOSTUNREACH, ...)`); hung backend does not delay probing the others (measure round time); `stop()` ends `run()` quickly; `stop()` before `run()`; one probe exception does not kill the loop; no fd leak over many rounds (count `/proc/self/fd` on Linux, skip elsewhere); late round resyncs without a burst of probes.

**Evidence for the report (`tests/demo_distribution.py`):** prints
1. Round robin split over 10,000 selects across 4 backends.
2. Least connections with sample `active` vectors, for example [3,1,1,2] and [0,0,5,9].
3. IP-hash mapping for the real client IPs 10.0.1.11 to 10.0.1.14 with 4 and with 3 healthy backends, and the spread for 10,000 random IPs.
Save the output of the full test run to `docs/test_results_balancing.txt`.

## 12. What you need from others, and what you give
| From | What | Until it lands |
|---|---|---|
| Abdur | `EventLogger.event(name, **fields)` and `Metrics.health_transition` (real versions) | use `FakeLogger` and `FakeMetrics` |
| Talha | calls `pool.healthy()`, `balancer.select()`, `pool.acquire()`, `pool.release()` per request | test with direct calls |
| Abdur | reads `pool.all()` (`.id`, `.healthy`, `.active`) in the stats server; generates configs that must pass `load_config` | keep `Backend` attributes exactly as in the contract |

You give: `load_config`, `ConfigError`, `make_balancer`, `make_pool`, `make_health_checker`, `config.example.json`. Real integration (all three modules together) happens in Phase 3. Fix integration bugs in your module on your branch.

## 13. Interaction notes (what the other modules assume about you)
- Talha's core calls `pool.release()` in a `finally`. Your `release` must therefore never raise.
- Abdur's detection-time metric reads the `backend_down` timestamp from the log. That event must be written **once**, by the pool, at the moment of the state change, and never at level-filtered.
- Abdur's config generator will write many configs. Error messages must be precise enough to fix a bad one in seconds.
- If the group later turns on `retry_on_connect_error`, Talha will need `select()` to be callable more than once per request with a shrinking candidate list. That already works with your pure functions. No change needed. Do not build retry yourself.

## 14. Tests are not enough: manual smoke checks (record results in the PR)
1. Run the health checker against two local fake backends. Kill one. Print the time from kill to DOWN. With `interval_s=0.1, fail_threshold=3` expect about 0.2 to 0.35 s (the real-settings numbers divided by 10).
2. Run the same with the real settings (1 s interval, 3 failures) once. It should be 2 to 3.5 s. Paste the measured number.
3. Restart the fake backend. Print the time to UP. Expect 1 to 2 intervals.

## 15. Build order and rough effort
1. `lb/config.py` + `tests/test_config.py` (2-3 h)
2. `pool.py` + `tests/test_pool.py` (1-2 h)
3. Three algorithms + `tests/test_balancing.py` (2 h)
4. `health.py` + `tests/test_health.py` (3-4 h, the hardest piece)
5. `lb/balancing/__init__.py`, `demo_distribution.py`, saved test output, smoke checks (1-2 h)
6. `docs/notes_balancing.md` (1 h)
Commit after each step. Open the PR when all tests pass.

## 16. Git
- Branch `feat/balancing`. PR to `main`. No direct pushes to `main`.
- Commit messages: short, present tense, say what changed (`add health checker state machine`).
- PR description: what is included, tests passing, assumptions, open questions for Talha or Abdur.
- **AI use.** The group reports AI use manually in one general disclosure. Nothing to log in the repo.

## 17. Definition of done
- [ ] All tests pass locally. Output saved in `docs/test_results_balancing.txt`.
- [ ] No third-party imports under `lb/`.
- [ ] Python 3.9 compatible (no `match`, no `X | Y` types evaluated at runtime).
- [ ] No globals, singletons, hard-coded IPs or ports.
- [ ] Type hints and short docstrings on public functions.
- [ ] `lb/interfaces.py` untouched.
- [ ] `config.example.json` loads cleanly.
- [ ] `docs/notes_balancing.md` written (section 20).
- [ ] PR opened with the report-back (section 21).

## 18. Rules
- Python 3.9+. Standard library only under `lb/` (tests may use pytest).
- No globals, no singletons, no hard-coded IPs or ports. Everything comes from config or arguments.
- Do not change `lb/interfaces.py`. If it blocks you, write the problem down and tell the group.
- Never block the event loop.
- Stay inside this brief. Scope comes from the official course text. Extra features are not allowed.

## 19. Not to build (out of scope)
Weights, consistent hashing, retry or fail-over to another backend inside `select()`, connection draining, connection pooling, TLS, caching, authentication, rate limiting, threads, third-party HTTP libraries, config hot-reload, environment-variable overrides, a probe-result history API, slow-start after recovery. If you think one is needed, ask first.

## 20. Explain as you go (important)
The group must understand what each block does. Write `docs/notes_balancing.md`: about 10 plain-English lines per file (`config`, `round_robin`, `least_conn`, `ip_hash`, `pool`, `health`). Each file covers:
- what it does,
- why it works that way,
- one failure case it handles.
Cover load-balancer ideas only: health check thresholds and hysteresis, why least-conn beats round robin on unequal backends, why IP-hash gives stickiness and when it breaks (include the real 4-client table), why detection time is 2 to 3.5 s and not 3 s, why the pool (not the checker) writes the DOWN/UP events. Skip basic networking.

## 21. Report back
List: files created, tests passing (count), assumptions you made, deviations from this brief and why, the measured detection time from section 14, open questions for Talha (core) and Abdur (logger/metrics), and any place the frozen interface made your job harder.
