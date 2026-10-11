"""Linux smoke measurements for the Project 5 balancing module."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import os
import platform
import random
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lb.balancing import make_balancer, make_health_checker, make_pool
from lb.config import validate_config
from tests.fakes import FakeBackend, FakeLogger, FakeMetrics


class TimedLogger(FakeLogger):
    """Record monotonic timestamps alongside logger events."""

    def __init__(self) -> None:
        super().__init__()
        self.times: List[float] = []

    def event(self, name: str, **fields: Any) -> None:
        self.times.append(asyncio.get_running_loop().time())
        super().event(name, **fields)


def make_config(
    backends: Sequence[Tuple[str, FakeBackend]],
    interval_s: float,
    timeout_s: float,
) -> dict:
    """Build a validated configuration for fake backends."""
    return validate_config(
        {
            "backends": [
                {"id": backend_id, "host": backend.host, "port": backend.port}
                for backend_id, backend in backends
            ],
            "health": {
                "path": "/health",
                "interval_s": interval_s,
                "timeout_s": timeout_s,
                "fail_threshold": 3,
                "success_threshold": 2,
            },
        }
    )


async def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 10.0,
    step: float = 0.01,
) -> None:
    """Wait for a state or event condition."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("smoke condition timed out")
        await asyncio.sleep(step)


def event_fields(logger: TimedLogger, name: str, backend_id: str) -> List[dict]:
    """Return fields for matching backend events."""
    return [
        fields
        for event_name, fields in logger.events
        if event_name == name and fields.get("backend") == backend_id
    ]


def event_time(logger: TimedLogger, name: str, backend_id: str) -> float:
    """Return the timestamp corresponding to a matching event."""
    for index, (event_name, fields) in enumerate(logger.events):
        if event_name == name and fields.get("backend") == backend_id:
            return logger.times[index]
    raise AssertionError(f"missing {name} event for {backend_id}")


async def stop_checker(checker: Any, task: asyncio.Task[Any]) -> None:
    """Stop and await a checker task."""
    checker.stop()
    try:
        await asyncio.wait_for(task, 2.0)
    except asyncio.CancelledError:
        pass


async def run_trial(
    interval_s: float,
    timeout_s: float,
    rng: random.Random,
    silent: bool,
) -> Tuple[float, float, str]:
    """Measure one detection and recovery trial."""
    backends = [FakeBackend("ok"), FakeBackend("ok")]
    for backend in backends:
        await backend.start()
    logger = TimedLogger()
    metrics = FakeMetrics()
    pairs = [("b1", backends[0]), ("b2", backends[1])]
    cfg = make_config(pairs, interval_s, timeout_s)
    pool = make_pool(cfg, logger, metrics)
    checker = make_health_checker(cfg, pool, logger)
    task = asyncio.create_task(checker.run())
    try:
        await wait_until(
            lambda: len(event_fields(logger, "health_probe", "b1")) >= 2
            and len(event_fields(logger, "health_probe", "b2")) >= 2
        )
        await asyncio.sleep(rng.uniform(0.0, interval_s))
        t_kill = asyncio.get_running_loop().time()
        if silent:
            backends[0].mode = "hang"
        else:
            await backends[0].stop()
        await wait_until(lambda: bool(event_fields(logger, "backend_down", "b1")))
        down_time = event_time(logger, "backend_down", "b1")
        down_reason = event_fields(logger, "backend_down", "b1")[0]["reason"]
        if event_fields(logger, "backend_down", "b2"):
            raise AssertionError("healthy b2 went down")
        # Randomize restart phase so recovery measures the full scheduling interval.
        await asyncio.sleep(rng.uniform(0.0, interval_s))
        t_restart = asyncio.get_running_loop().time()
        if silent:
            backends[0].mode = "ok"
        else:
            await backends[0].start()
        await wait_until(lambda: bool(event_fields(logger, "backend_up", "b1")))
        recovery = event_time(logger, "backend_up", "b1") - t_restart
        return down_time - t_kill, recovery, down_reason
    finally:
        await stop_checker(checker, task)
        for backend in backends:
            await backend.stop()


def summarize(
    name: str,
    rows: Sequence[Tuple[float, float, str]],
    detection_expected: Tuple[float, float],
    recovery_expected: Tuple[float, float],
) -> None:
    """Print trial values, statistics, and range verdicts."""
    detections = [row[0] for row in rows]
    recoveries = [row[1] for row in rows]
    detection_low, detection_high = detection_expected
    recovery_low, recovery_high = recovery_expected
    detection_tolerance = 0.05 if detection_high < 1.0 else 0.10
    recovery_tolerance = 0.05 if recovery_high < 1.0 else 0.10
    detection_verdict = (
        "WITHIN"
        if min(detections) >= detection_low - detection_tolerance
        and max(detections) <= detection_high + detection_tolerance
        else "OUTSIDE"
    )
    recovery_verdict = (
        "WITHIN"
        if min(recoveries) >= recovery_low - recovery_tolerance
        and max(recoveries) <= recovery_high + recovery_tolerance
        else "OUTSIDE"
    )
    print(f"\n{name}")
    print("trial  detection_s  recovery_s  reason")
    print("-----  -----------  ----------  ------")
    for index, (detection, recovery, reason) in enumerate(rows, 1):
        print(f"{index:>5}  {detection:>11.3f}  {recovery:>10.3f}  {reason}")
    print(
        f"detection min/mean/max: {min(detections):.3f} / "
        f"{mean(detections):.3f} / {max(detections):.3f} s; "
        f"stdev: {stdev(detections):.3f} s"
    )
    print(
        f"recovery min/mean/max: {min(recoveries):.3f} / "
        f"{mean(recoveries):.3f} / {max(recoveries):.3f} s; "
        f"stdev: {stdev(recoveries):.3f} s"
    )
    print(
        f"detection expected range: {detection_low:.2f} to {detection_high:.2f} s; "
        f"verdict: {detection_verdict}"
    )
    print(
        f"recovery expected range: {recovery_low:.2f} to {recovery_high:.2f} s; "
        f"verdict: {recovery_verdict}"
    )


async def run_trials(
    name: str,
    count: int,
    interval_s: float,
    timeout_s: float,
    rng: random.Random,
    silent: bool,
    detection_expected: Tuple[float, float],
    recovery_expected: Tuple[float, float],
) -> List[Tuple[float, float, str]]:
    """Run and print a group of detection trials."""
    rows = [
        await run_trial(interval_s, timeout_s, rng, silent)
        for index in range(count)
    ]
    summarize(name, rows, detection_expected, recovery_expected)
    return rows


def simulate(
    pool: Any,
    algorithm_name: str,
    healthy_ids: Sequence[str],
) -> Tuple[Counter, Dict[str, str]]:
    """Run the deterministic 400-round, eight-request simulation."""
    balancer = make_balancer(algorithm_name)
    clients = [f"10.0.1.{i}" for i in range(11, 15)]
    counts: Counter = Counter()
    for round_number in range(400):
        selected = []
        for request_number in range(8):
            client_ip = clients[(round_number * 8 + request_number) % len(clients)]
            backend = balancer.select(client_ip, pool.healthy())
            if backend is None:
                raise AssertionError("balancer returned None with healthy backends")
            selected.append(backend)
            counts[backend.id] += 1
            pool.acquire(backend)
        for backend in selected:
            pool.release(backend)
    mapping = {
        client: make_balancer("ip_hash").select(client, pool.healthy()).id
        for client in clients
    }
    if [backend.id for backend in pool.healthy()] != list(healthy_ids):
        raise AssertionError("pool healthy order changed")
    if any(backend.active != 0 for backend in pool.all()):
        raise AssertionError("active count did not return to zero")
    return counts, mapping


async def run_s4() -> None:
    """Run the four-backend end-to-end module smoke."""
    backends = [FakeBackend("ok") for _ in range(4)]
    for backend in backends:
        await backend.start()
    logger = TimedLogger()
    metrics = FakeMetrics()
    pairs = [(f"b{i}", backend) for i, backend in enumerate(backends, 1)]
    cfg = make_config(pairs, 0.1, 0.05)
    pool = make_pool(cfg, logger, metrics)
    checker = make_health_checker(cfg, pool, logger)
    task = asyncio.create_task(checker.run())
    expected_full = ("b1", "b2", "b3", "b4")
    expected_down = ("b1", "b2", "b4")
    expected_maps = {
        "full": {"10.0.1.11": "b3", "10.0.1.12": "b2", "10.0.1.13": "b2", "10.0.1.14": "b2"},
        "down": {"10.0.1.11": "b1", "10.0.1.12": "b1", "10.0.1.13": "b2", "10.0.1.14": "b2"},
    }
    try:
        await wait_until(lambda: len(event_fields(logger, "health_probe", "b3")) >= 2)
        for phase, healthy_ids in (("all healthy", expected_full),):
            print(f"\nS4 phase: {phase}")
            for algorithm in ("round_robin", "least_conn", "ip_hash"):
                counts, mapping = simulate(pool, algorithm, healthy_ids)
                print(f"{algorithm}: counts={dict(sorted(counts.items()))}")
                print(f"  healthy={list(healthy_ids)}")
                if algorithm == "ip_hash":
                    print(f"  mapping={mapping}; table_match={mapping == expected_maps['full']}")

        await backends[2].stop()
        await wait_until(lambda: bool(event_fields(logger, "backend_down", "b3")))
        print("\nS4 phase: b3 down")
        for algorithm in ("round_robin", "least_conn", "ip_hash"):
            counts, mapping = simulate(pool, algorithm, expected_down)
            print(f"{algorithm}: counts={dict(sorted(counts.items()))}")
            print(f"  healthy={list(expected_down)}")
            if algorithm == "ip_hash":
                print(f"  mapping={mapping}; table_match={mapping == expected_maps['down']}")
            if counts["b3"] != 0:
                raise AssertionError("b3 received selections while down")

        await backends[2].start()
        await wait_until(lambda: bool(event_fields(logger, "backend_up", "b3")))
        print("\nS4 phase: recovered")
        for algorithm in ("round_robin", "least_conn", "ip_hash"):
            counts, mapping = simulate(pool, algorithm, expected_full)
            print(f"{algorithm}: counts={dict(sorted(counts.items()))}")
            print(f"  healthy={list(expected_full)}")
            if algorithm == "ip_hash":
                print(f"  mapping={mapping}; table_match={mapping == expected_maps['full']}")

        down_events = event_fields(logger, "backend_down", "b3")
        up_events = event_fields(logger, "backend_up", "b3")
        transitions = metrics.transitions
        active_zero = all(backend.active == 0 for backend in pool.all())
        print("\nS4 verification:")
        print(f"b3 selections while down: 0; verdict={'PASS' if active_zero else 'FAIL'}")
        print(f"backend_down events for b3: {len(down_events)}; reason={down_events[0]['reason']}")
        print(f"backend_up events for b3: {len(up_events)}; reason={up_events[0]['reason']}")
        print(f"metrics transitions: {transitions}")
        if len(down_events) != 1 or len(up_events) != 1 or len(transitions) != 2:
            raise AssertionError("unexpected S4 transition count")
        print("S4 verdict: PASS")
    finally:
        await stop_checker(checker, task)
        for backend in backends:
            await backend.stop()


def print_header(seed: int) -> None:
    """Print environment and measurement assumptions."""
    try:
        version = Path("/proc/version").read_text(encoding="utf-8")
    except OSError:
        version = ""
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"WSL (/proc/version mentions microsoft): {'yes' if 'microsoft' in version.lower() else 'no'}")
    print(f"nproc: {os.cpu_count()}")
    print(f"seed: {seed}")
    print(
        "kill time = just before FakeBackend.stop() or the mode switch is issued, "
        "so detection times are conservative (slightly high)"
    )


def summarize_recovery_rows(
    rows_by_name: Sequence[Tuple[str, Sequence[Tuple[float, float, str]], Tuple[float, float]]],
) -> None:
    """Print the recovery summary required by the S3 check."""
    print("\nS3: recovery summary")
    print("setting      min/mean/max_s       stdev_s  verdict")
    print("-----------  -------------------  -------  -------")
    for name, rows, expected in rows_by_name:
        values = [row[1] for row in rows]
        low, high = expected
        tolerance = 0.05 if high < 1.0 else 0.10
        verdict = (
            "WITHIN"
            if min(values) >= low - tolerance and max(values) <= high + tolerance
            else "OUTSIDE"
        )
        print(
            f"{name:<11}  {min(values):.3f}/{mean(values):.3f}/{max(values):.3f}  "
            f"{stdev(values):.3f}   {verdict}"
        )
    print(
        "Recovery restart phase is randomized after backend_down so each trial "
        "samples the scheduling interval."
    )


async def async_main(
    quick: bool,
    seed: int,
    short_trials: int,
    real_trials: int,
    blackhole_trials: int,
) -> None:
    """Run requested smoke checks."""
    print_header(seed)
    short = await run_trials(
        "S1: short stop detection",
        3 if quick else short_trials,
        0.1,
        0.05,
        random.Random(seed + 1),
        False,
        (0.20, 0.35),
        (0.10, 0.20),
    )
    await run_s4()
    if quick:
        return
    real = await run_trials(
        "S2: real stop detection",
        real_trials,
        1.0,
        0.5,
        random.Random(seed + 2),
        False,
        (2.0, 3.5),
        (1.0, 2.0),
    )
    blackhole = await run_trials(
        "S2b: real silent-death detection",
        blackhole_trials,
        1.0,
        0.5,
        random.Random(seed + 3),
        True,
        (2.5, 3.5),
        (1.0, 2.0),
    )
    summarize_recovery_rows(
        (
            ("S1 short", short, (0.10, 0.20)),
            ("S2 real", real, (1.0, 2.0)),
            ("S2b blackhole", blackhole, (1.0, 2.0)),
        )
    )


def main() -> int:
    """Parse arguments and run smoke checks."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--short-trials", type=int, default=10)
    parser.add_argument("--real-trials", type=int, default=10)
    parser.add_argument("--blackhole-trials", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(
        async_main(
            args.quick,
            args.seed,
            args.short_trials,
            args.real_trials,
            args.blackhole_trials,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
