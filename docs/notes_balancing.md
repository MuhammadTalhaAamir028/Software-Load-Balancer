# Balancing module notes

## Overview

The balancing module loads strict configuration and supplies three backend selection algorithms.
It also owns the shared backend pool and the active health checker.
For one request, the core asks the pool for healthy backends, the balancer selects one, and the core updates its active count.
For one probe round, the checker probes every configured backend and tells the pool only when a health state changes.
The balancer rules are pure functions of the client IP and healthy backend sequence, with no I/O.
The pool is the single place where health changes, transition events, and health metrics are recorded.
These two rules keep the request path small and prevent duplicate or inaccurate health events.

## 1. config.py

**What it does**

JSON is the canonical configuration format. YAML is accepted only when PyYAML is installed, through a lazy import.
The validator supplies defaults: listen `0.0.0.0:8080`, stats `0.0.0.0:9090`, health `/health`, 1.0 second interval, 0.5 second timeout, three failures down, and two successes up.
The loader accepts only supported file extensions and returns a fresh normalized dictionary.
Unknown keys are rejected at every checked section, so a spelling mistake cannot silently select a default.
Validation collects multiple problems and reports them together with dotted paths such as `health.timeout_s`.
The required backend list is checked for valid entries, ports, IDs, and duplicate IDs.
Cross-field rules require `health.timeout_s < health.interval_s` and require `stats.port` to differ from `listen.port`.

**Why it works this way**

Strict validation moves mistakes to startup instead of allowing a wrong setting to affect a long benchmark.
The timeout rule leaves time between rounds, so a slow probe round does not overlap its successor.
A fresh result prevents callers from changing the loader's internal defaults by retaining or mutating input objects.

**Failure case it handles**

A misspelled key or a timeout equal to or greater than the interval raises `ConfigError` with a clear path and message.
The process therefore stops before running with an accidental health schedule.

## 2. round_robin.py

**What it does**

Round robin keeps a rotating integer counter and selects the next item in the healthy sequence.
The counter is reduced modulo the current healthy-list length, so each selection is O(1) after the list is supplied.
Over a stable set, repeated requests receive even counts.
The demo gives 2,500 selections to each of four backends, then 1,000 each to b1, b2, and b4 after b3 is removed.
The pool supplies the healthy sequence, so a down backend is absent before selection.
The counter belongs to each balancer instance, and factory calls return independent instances.

**Why it works this way**

Round robin is easy to explain and gives a useful baseline for the benchmark.
It ignores capacity, however. Our backends have artificial delays of 5, 10, 25, and 50 ms and CPU limits of 1.0, 1.0, 0.5, and 0.25.
The slowest backend still receives the same request share as the fastest one.

**Failure case it handles**

If the healthy list is empty, selection returns `None`.
There is no modulo-by-zero exception and no attempt to invent a backend.

## 3. least_conn.py

**What it does**

Least connections selects the backend with the smallest `Backend.active` value.
`active` is the number of requests currently in flight.
Python's `min` keeps the first backend when several values tie, which preserves configuration order.
The demo confirms this with `[5,5,5,5] -> b1` and with other tied vectors.
The core increments and decrements through the pool around each forwarded request.
The algorithm therefore depends on the pool's active values being accurate.

**Why it works this way**

This policy can adapt to unequal capacity because a fast backend finishes sooner and its active count returns to a low value.
Expectation: under overlapping benchmark requests, fast backends should be selected more often than slow ones.
That benefit is an expectation to confirm with the routed benchmark, not a measured result in this module's evidence.
When requests do not overlap, every backend can have active count zero at selection time.
In that case the tie rule repeatedly chooses the first backend, so least connections balances only when concurrency creates informative counts.

**Failure case it handles**

An empty healthy list returns `None`. The pool also clamps an over-release at zero.

## 4. ip_hash.py

**What it does**

IP hash normalizes a client address with `ipaddress.ip_address` when possible.
It hashes the normalized text with SHA-256, converts the first eight digest bytes to an integer, and takes that value modulo the healthy-backend count.
The same client therefore stays on the same backend while the healthy sequence is unchanged.
SHA-256 is used instead of Python's built-in `hash`, because the built-in string hash is randomized between processes.
IPv6, odd formatting, and an empty or unparsable string do not crash the algorithm; an invalid string is hashed as supplied.
IPv4-mapped IPv6 text is normalized differently from plain IPv4 text and may map differently, which is harmless because the listener is IPv4.

**Why it works this way**

Stickiness can keep one client's requests on one backend without storing a client table.
Plain modulo hashing is sensitive to the number and order of healthy backends.
When b3 is removed, the 10,000-client demo reports 75.17 percent changing backend.
With four to three backends, only 3 of every 12 hash residues keep their old slot, so about 75 percent move.
The hash was deliberately not tuned to hide uneven small-client behaviour.

**Failure case it handles**

An empty healthy list returns `None`.
With four clients, the real table is:

| Client | Four healthy | b3 down |
|---|---|---|
| 10.0.1.11 | b3 | b1 |
| 10.0.1.12 | b2 | b1 |
| 10.0.1.13 | b2 | b2 |
| 10.0.1.14 | b2 | b2 |

With all four healthy, b2 receives three clients, b3 receives one, and b1 and b4 receive none.

## 5. pool.py

**What it does**

The pool owns one shared list of `Backend` objects.
The core, checker, and stats code observe the same health and active state.
`all()` and `healthy()` return new lists in configuration order.
`healthy()` filters by the backend's current health flag, while callers cannot reorder the pool by mutating the returned list.
`acquire()` increments the in-flight count and `release()` decrements it.
`set_health()` changes a backend only when the requested state differs from the current state.
On a real change it emits either `backend_down` or `backend_up` and calls `metrics.health_transition`.

**Why it works this way**

The pool, not the checker, writes transition events because one place can guarantee one event per real state change.
The state is changed first, then the log event is written, then metrics are called.
It also makes the transition event timestamp the true detection time used by failure metrics.
The checker reports probe results; the pool records the shared state change.
Calling `set_health()` with the current state is a silent no-op, preventing duplicate transitions.

**Failure case it handles**

Releasing more times than acquiring clamps `active` at zero and logs `backend_error` with `release_underflow`.
This prevents least connections from favouring a backend because it has a negative count.
Acquiring a down backend remains allowed so requests already in flight can finish normally.
An unknown backend ID raises `KeyError`.

## 6. health.py

**What it does**

The checker probes every backend concurrently once per interval.
Each probe sends `GET /health` and runs connect, send, and response-head reading under one timeout.
Only HTTP status 200 is success. The response head is read in chunks up to 8 KB and the first status line is parsed.
Healthy backends need three consecutive failures to go down; down backends need two consecutive successes to recover.
The state machine is: healthy plus success resets failures; healthy plus failure increments failures; down plus success increments successes; down plus failure resets successes.
The checker applies results after the whole concurrent round completes, so another backend's slow probe can delay application by up to the probe timeout.
Failure reasons are `refused`, `reset`, `timeout`, `unreachable`, `bad_status:<code>`, `malformed_response`, or `error:<Name>`.
The writer is always closed, with a short timeout around `wait_closed()`.

**Why it works this way**

The two thresholds provide hysteresis: fail, success, fail, success never reaches DOWN, while a recovering backend must prove itself twice.
The timeout must be shorter than the interval, so normal rounds do not overlap.
For the real settings, interval I is 1 s and timeout T is 0.5 s. A kill occurs U seconds after a tick, where U is 0 to 1.
For an instant refusal, the third failed result arrives after U + 2 s, giving 2 to 3 s and a 2.5 s mean.
For a silent death, each failed probe can consume 0.5 s, giving U + 2.5 s, or 2.5 to 3.5 s and a 3.0 s mean.
Recovery needs two successes, so it takes U + 1 s, or 1 to 2 s with a 1.5 s mean.
During detection, round robin can send about one in four requests to the dead backend; these are the failed requests during failure.
Startup backends are healthy by contract, so a backend dead at startup remains eligible until three probes fail.

**Failure case it handles**

A backend that accepts and never answers, or sends a status line and stalls, ends in `timeout` without blocking the other probes.
One crashing probe is isolated by concurrent gathering, and a failure inside `pool.set_health` does not kill the loop because the checker catches it.
A response larger than 8 KB is not read beyond the limit.
The descriptor tests pass with the real checker; a deliberately broken no-close variant is detected by both.

## Measured numbers and evidence

| Check | Model mean | Measured min / mean / max |
|---|---:|---:|
| Short refusal, 0.1 s interval | 0.25 s | 0.206 / 0.249 / 0.272 s |
| Real refusal, 1 s interval | 2.5 s | 2.101 / 2.533 / 2.988 s |
| Silent death, 1 s interval | 3.0 s | 2.608 / 2.928 / 3.169 s |
| Short recovery | 0.15 s | 0.104 / 0.158 / 0.203 s |
| Real refusal recovery | 1.5 s | 1.143 / 1.557 / 1.943 s |
| Silent death recovery | 1.5 s | 1.021 / 1.490 / 1.878 s |

These values were measured on Ubuntu 24.04 under WSL2 with Python 3.12.3, using loopback fake backends in one process. They are not routed-topology results. A real `docker kill` can produce `unreachable` or `timeout`, so the final report must use topology measurements.
The full suite recorded 130 tests passed and zero failures. Evidence is in `docs/test_results_balancing.txt` and `docs/smoke_checks_balancing.txt`. The reproducible scripts are `tests/demo_distribution.py` and `tests/smoke_health.py`.

## Findings worth quoting in the report

- Four-client IP-hash skew is visible: b2 receives three of four clients, while b1 and b4 receive none. This is the saved demo table.
- IP hash reshuffles 75.17 percent of 10,000 clients when b3 goes down. Plain modulo hashing causes this movement.
- Least connections degenerates to the first backend when requests do not overlap. This is shown by the `[5,5,5,5] -> b1` demo row.
- The detection model agrees with measurement: short refusal averaged 0.249 s, real refusal 2.533 s, and silent death 2.928 s.
- The 3-down and 2-up hysteresis thresholds trade a slower reaction for fewer false alarms from flapping backends.
- Pool-owned transition events keep the detection timestamp exact because the event is written at the state change, not at an earlier probe start.
