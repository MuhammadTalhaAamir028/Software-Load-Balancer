"""Unit tests for lb.obs (logger and metrics).

Run from the repository root:  pytest -q tests/test_obs.py
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lb.obs import logger as logger_module
from lb.obs.logger import make_logger, utc_iso_ms
from lb.obs.metrics import MetricsCollector, make_metrics, percentile

TS_PATTERN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


# ---------------------------------------------------------------- helpers

def read_lines(path: Path):
    """Return the log file as a list of parsed JSON objects."""
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def make_test_logger(tmp_path: Path, level: str = "INFO"):
    path = tmp_path / "logs" / "lb.jsonl"
    log = make_logger({"logging": {"path": str(path), "level": level}})
    return log, path


# ----------------------------------------------------------------- logger

def test_timestamp_format():
    assert TS_PATTERN.match(utc_iso_ms())
    fixed = datetime(2026, 10, 9, 14, 3, 21, 482999, tzinfo=timezone.utc)
    # Milliseconds are truncated, not rounded.
    assert utc_iso_ms(fixed) == "2026-10-09T14:03:21.482Z"


def test_writes_one_json_object_per_line(tmp_path):
    log, path = make_test_logger(tmp_path)
    log.event("request", client="10.0.1.11", status=200, latency_ms=12.5)
    log.event("startup", algorithm="round_robin")
    log.close()
    rows = read_lines(path)
    assert len(rows) == 2
    assert rows[0]["event"] == "request"
    assert rows[0]["client"] == "10.0.1.11"
    assert rows[0]["latency_ms"] == 12.5
    assert TS_PATTERN.match(rows[0]["ts"])
    # ts and event come first, then the caller's fields.
    assert list(rows[0].keys())[:2] == ["ts", "event"]


def test_caller_fields_cannot_overwrite_reserved_keys(tmp_path):
    log, path = make_test_logger(tmp_path)
    log.event("request", ts="evil", event="evil")
    log.close()
    row = read_lines(path)[0]
    assert row["event"] == "request"
    assert TS_PATTERN.match(row["ts"])
    assert row["ts_"] == "evil" and row["event_"] == "evil"


def test_creates_directory_and_appends(tmp_path):
    log, path = make_test_logger(tmp_path)  # logs/ does not exist yet
    log.event("startup")
    log.close()
    log2, _ = make_test_logger(tmp_path)    # second run: same file
    log2.event("shutdown", reason="test")
    log2.close()
    assert [r["event"] for r in read_lines(path)] == ["startup", "shutdown"]


def test_level_info_filters_debug_events(tmp_path):
    log, path = make_test_logger(tmp_path, "INFO")
    log.event("health_probe", backend="b1", ok=True)       # DEBUG
    log.event("backend_selected", backend="b1")             # DEBUG
    log.event("request", status=200)                        # INFO
    log.event("backend_error", backend="b1", error="x")     # WARNING
    log.close()
    assert [r["event"] for r in read_lines(path)] == ["request", "backend_error"]


def test_level_debug_writes_everything(tmp_path):
    log, path = make_test_logger(tmp_path, "DEBUG")
    log.event("health_probe", backend="b1", ok=True)
    log.event("request", status=200)
    log.close()
    assert len(read_lines(path)) == 2


def test_level_warning_keeps_only_warnings_and_always_events(tmp_path):
    """Benchmark mode: no request lines, but backend_down/up are still written."""
    log, path = make_test_logger(tmp_path, "WARNING")
    log.event("request", status=200)
    log.event("client_error", client="c", error="x")
    log.event("backend_error", backend="b3", error="refused")
    log.event("backend_down", backend="b3", reason="3 failures")
    log.event("backend_up", backend="b3", reason="2 successes")
    log.close()
    assert [r["event"] for r in read_lines(path)] == [
        "backend_error", "backend_down", "backend_up"]


def test_level_error_still_writes_always_events(tmp_path):
    log, path = make_test_logger(tmp_path, "ERROR")
    log.event("backend_error", backend="b1", error="x")     # WARNING < ERROR
    log.event("backend_down", backend="b1", reason="r")
    log.close()
    assert [r["event"] for r in read_lines(path)] == ["backend_down"]


def test_unknown_level_falls_back_to_info(tmp_path):
    log, path = make_test_logger(tmp_path, "LOUD")
    log.event("health_probe", backend="b1")
    log.event("request", status=200)
    log.close()
    assert [r["event"] for r in read_lines(path)] == ["request"]


def test_non_serialisable_values_are_converted_with_str(tmp_path):
    log, path = make_test_logger(tmp_path)

    class Odd:
        def __str__(self):
            return "odd-object"

    class Broken:
        def __str__(self):
            raise RuntimeError("no string for you")

    log.event("request", a=Odd(), b={1, 2}, c=Path("x/y"), d=float("nan"), e=Broken())
    log.event("request", status=200)  # the logger must still work afterwards
    log.close()
    rows = read_lines(path)
    assert rows[0]["a"] == "odd-object"
    assert rows[0]["c"] == str(Path("x/y"))
    assert rows[0]["d"] == "nan"            # NaN is not valid JSON, so it is a string
    assert rows[0]["e"].startswith("<unprintable")
    assert rows[1]["status"] == 200
    assert log.dropped == 0


def test_failed_write_is_counted_and_never_raises(tmp_path):
    log, path = make_test_logger(tmp_path)
    log._file.close()               # simulate a broken disk or a closed file
    log.event("request", status=200)  # must not raise
    log.event("request", status=200)
    assert log.dropped == 2
    log.close()                      # must not raise either


def test_event_after_close_is_ignored(tmp_path):
    log, path = make_test_logger(tmp_path)
    log.event("startup")
    log.close()
    log.close()                      # idempotent
    log.event("request", status=200)
    assert [r["event"] for r in read_lines(path)] == ["startup"]


def test_buffered_until_timer_when_loop_is_running(tmp_path, monkeypatch):
    """With a running loop, normal events are flushed by the 1 s timer, not at once."""
    monkeypatch.setattr(logger_module, "FLUSH_INTERVAL_S", 0.2)
    log, path = make_test_logger(tmp_path)

    async def scenario():
        log.event("request", status=200)
        assert path.read_text() == ""          # still only in memory
        await asyncio.sleep(0.4)
        assert len(read_lines(path)) == 1      # the timer wrote it
        log.close()

    asyncio.run(scenario())


def test_backend_down_is_written_immediately(tmp_path):
    log, path = make_test_logger(tmp_path)

    async def scenario():
        log.event("backend_down", backend="b3", reason="3 failures")
        assert [r["event"] for r in read_lines(path)] == ["backend_down"]
        log.close()

    asyncio.run(scenario())


def test_timestamp_is_taken_at_call_time_not_flush_time(tmp_path, monkeypatch):
    log, path = make_test_logger(tmp_path)
    first = datetime(2026, 10, 9, 14, 3, 21, 482000, tzinfo=timezone.utc)
    later = datetime(2026, 10, 9, 14, 3, 25, 0, tzinfo=timezone.utc)

    async def scenario():
        monkeypatch.setattr(logger_module, "_utc_now", lambda: first)
        log.event("request", status=200)         # buffered (loop is running)
        monkeypatch.setattr(logger_module, "_utc_now", lambda: later)
        log.close()                               # flushed 3.5 "seconds" later

    asyncio.run(scenario())
    assert read_lines(path)[0]["ts"] == "2026-10-09T14:03:21.482Z"


def test_burst_triggers_early_flush(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "MAX_BUFFERED_LINES", 10)
    log, path = make_test_logger(tmp_path)

    async def scenario():
        for i in range(10):
            log.event("request", i=i)
        assert len(read_lines(path)) == 10       # buffer limit reached, no timer needed
        log.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------- metrics

def test_percentile_nearest_rank():
    values = list(range(1, 101))                  # 1..100
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert percentile([], 50) == 0.0
    assert percentile([7.0], 99) == 7.0
    assert percentile([1, 2, 3, 4], 50) == 2      # ceil(0.5 * 4) = rank 2


def test_error_and_backend_counters():
    m = MetricsCollector(percentile_cache_s=0)
    m.request_done("b1", 200, 5.0, 100, 1000, "10.0.1.11")
    m.request_done("b1", 502, 6.0, 100, 50, "10.0.1.11")
    m.request_done("b2", 200, 7.0, 10, 20, "10.0.1.12")
    m.request_done(None, 503, 0.5, 5, 30, "10.0.1.13")   # no healthy backend
    snap = m.snapshot()
    assert snap["total_requests"] == 4
    assert snap["errors"] == 2                            # the 502 and the None-backend one
    assert snap["per_backend"]["b1"] == {
        "requests": 2, "errors": 1, "bytes_up": 200, "bytes_down": 1050}
    assert snap["per_backend"]["b2"]["errors"] == 0
    assert None not in snap["per_backend"]               # unrouted requests have no backend row


def test_status_below_500_is_not_an_error():
    m = MetricsCollector(percentile_cache_s=0)
    m.request_done("b1", 404, 1.0, 0, 0, "c")
    m.request_done("b1", 499, 1.0, 0, 0, "c")
    assert m.snapshot()["errors"] == 0


def test_latency_percentiles_in_snapshot():
    m = MetricsCollector(percentile_cache_s=0)
    for v in range(1, 101):
        m.request_done("b1", 200, float(v), 0, 0, "c")
    lat = m.snapshot()["latency_ms"]
    assert lat == {"p50": 50, "p95": 95, "p99": 99}


def test_latency_samples_are_bounded():
    m = MetricsCollector(max_samples=10, percentile_cache_s=0)
    for v in range(1, 26):                                # 25 requests, only last 10 kept
        m.request_done("b1", 200, float(v), 0, 0, "c")
    # Stored samples are 16..25, so the median is the 5th of them: 20.
    assert m.snapshot()["latency_ms"]["p50"] == 20
    assert m.snapshot()["total_requests"] == 25           # counters are not bounded


def test_health_events_are_bounded_and_ordered():
    m = MetricsCollector()
    for i in range(150):
        m.health_transition("b3", i % 2 == 0, "reason %d" % i)
    events = m.snapshot()["health_events"]
    assert len(events) == 100
    assert events[0]["reason"] == "reason 50"             # oldest kept
    assert events[-1]["reason"] == "reason 149"           # newest last
    assert set(events[0]) == {"ts", "backend", "healthy", "reason"}
    assert TS_PATTERN.match(events[0]["ts"])


def test_snapshot_keys_and_json_serialisable():
    m = MetricsCollector()
    m.request_done("b1", 200, 1.23456, 1, 2, "c")
    m.health_transition("b1", False, "r")
    snap = m.snapshot()
    assert set(snap) == {"uptime_s", "total_requests", "errors", "latency_ms",
                         "per_backend", "health_events"}
    assert set(snap["latency_ms"]) == {"p50", "p95", "p99"}
    json.dumps(snap)                                      # must not raise


def test_floats_are_rounded_to_three_decimals():
    m = MetricsCollector(percentile_cache_s=0)
    m.request_done("b1", 200, 1.23456789, 0, 0, "c")
    assert m.snapshot()["latency_ms"]["p50"] == 1.235


def test_percentile_cache_reuses_sorted_copy():
    m = MetricsCollector(percentile_cache_s=60)
    m.request_done("b1", 200, 10.0, 0, 0, "c")
    first = m.snapshot()["latency_ms"]["p50"]
    m.request_done("b1", 200, 1000.0, 0, 0, "c")
    assert m.snapshot()["latency_ms"]["p50"] == first     # served from the cache
    fresh = MetricsCollector(percentile_cache_s=0)
    fresh.request_done("b1", 200, 10.0, 0, 0, "c")
    fresh.request_done("b1", 200, 1000.0, 0, 0, "c")
    assert fresh.snapshot()["latency_ms"]["p99"] == 1000.0


def test_request_done_never_raises_and_ignores_bad_calls():
    m = MetricsCollector(percentile_cache_s=0)
    m.request_done("b1", "oops", 1.0, 0, 0, "c")          # status is not a number
    m.request_done("b1", 200, None, 0, 0, "c")            # latency missing
    m.request_done("b1", 200, 1.0, None, 0, "c")          # bytes missing
    assert m.snapshot()["total_requests"] == 0            # nothing half-recorded
    assert m.internal_errors == 3
    m.request_done("b1", 200, float("nan"), 0, 0, "c")    # counted, latency skipped
    snap = m.snapshot()
    assert snap["total_requests"] == 1
    assert snap["latency_ms"]["p50"] == 0.0


def test_uptime_grows():
    m = MetricsCollector()
    first = m.snapshot()["uptime_s"]
    assert first >= 0
    assert m.snapshot()["uptime_s"] >= first


def test_factory_returns_collector():
    assert isinstance(make_metrics({}), MetricsCollector)