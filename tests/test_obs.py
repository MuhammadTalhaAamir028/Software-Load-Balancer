"""Unit tests for lb.obs (logger and metrics).

Run from the repository root:  python -m unittest tests.test_obs -v
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lb.obs import logger as logger_module
from lb.obs.logger import make_logger, utc_iso_ms
from lb.obs.metrics import MetricsCollector, make_metrics, percentile

TS_PATTERN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def read_lines(path: Path):
    """Return the log file as a list of parsed JSON objects."""
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ----------------------------------------------------------------- logger

class TestLogger(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def make_test_logger(self, level: str = "INFO"):
        path = self.tmp / "logs" / "lb.jsonl"
        log = make_logger({"logging": {"path": str(path), "level": level}})
        return log, path

    def names(self, path: Path):
        return [r["event"] for r in read_lines(path)]

    def test_timestamp_format(self):
        self.assertRegex(utc_iso_ms(), TS_PATTERN)
        fixed = datetime(2026, 10, 9, 14, 3, 21, 482999, tzinfo=timezone.utc)
        # Milliseconds are truncated, not rounded.
        self.assertEqual(utc_iso_ms(fixed), "2026-10-09T14:03:21.482Z")

    def test_writes_one_json_object_per_line(self):
        log, path = self.make_test_logger()
        log.event("request", client="10.0.1.11", status=200, latency_ms=12.5)
        log.event("startup", algorithm="round_robin")
        log.close()
        rows = read_lines(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "request")
        self.assertEqual(rows[0]["client"], "10.0.1.11")
        self.assertEqual(rows[0]["latency_ms"], 12.5)
        self.assertRegex(rows[0]["ts"], TS_PATTERN)
        # ts and event come first, then the caller's fields.
        self.assertEqual(list(rows[0].keys())[:2], ["ts", "event"])

    def test_caller_fields_cannot_overwrite_reserved_keys(self):
        log, path = self.make_test_logger()
        log.event("request", ts="evil", event="evil")
        log.close()
        row = read_lines(path)[0]
        self.assertEqual(row["event"], "request")
        self.assertRegex(row["ts"], TS_PATTERN)
        self.assertEqual((row["ts_"], row["event_"]), ("evil", "evil"))

    def test_creates_directory_and_appends(self):
        log, path = self.make_test_logger()   # logs/ does not exist yet
        log.event("startup")
        log.close()
        log2, _ = self.make_test_logger()     # second run: same file
        log2.event("shutdown", reason="test")
        log2.close()
        self.assertEqual(self.names(path), ["startup", "shutdown"])

    def test_level_info_filters_debug_events(self):
        log, path = self.make_test_logger("INFO")
        log.event("health_probe", backend="b1", ok=True)       # DEBUG
        log.event("backend_selected", backend="b1")             # DEBUG
        log.event("request", status=200)                        # INFO
        log.event("backend_error", backend="b1", error="x")     # WARNING
        log.close()
        self.assertEqual(self.names(path), ["request", "backend_error"])

    def test_level_debug_writes_everything(self):
        log, path = self.make_test_logger("DEBUG")
        log.event("health_probe", backend="b1", ok=True)
        log.event("request", status=200)
        log.close()
        self.assertEqual(len(read_lines(path)), 2)

    def test_level_warning_keeps_only_warnings_and_always_events(self):
        """Benchmark mode: no request lines, but backend_down/up are still written."""
        log, path = self.make_test_logger("WARNING")
        log.event("request", status=200)
        log.event("client_error", client="c", error="x")
        log.event("backend_error", backend="b3", error="refused")
        log.event("backend_down", backend="b3", reason="3 failures")
        log.event("backend_up", backend="b3", reason="2 successes")
        log.close()
        self.assertEqual(self.names(path), ["backend_error", "backend_down", "backend_up"])

    def test_level_error_still_writes_always_events(self):
        log, path = self.make_test_logger("ERROR")
        log.event("backend_error", backend="b1", error="x")     # WARNING < ERROR
        log.event("backend_down", backend="b1", reason="r")
        log.close()
        self.assertEqual(self.names(path), ["backend_down"])

    def test_unknown_level_falls_back_to_info(self):
        log, path = self.make_test_logger("LOUD")
        log.event("health_probe", backend="b1")
        log.event("request", status=200)
        log.close()
        self.assertEqual(self.names(path), ["request"])

    def test_non_serialisable_values_are_converted_with_str(self):
        log, path = self.make_test_logger()

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
        self.assertEqual(rows[0]["a"], "odd-object")
        self.assertEqual(rows[0]["c"], str(Path("x/y")))
        self.assertEqual(rows[0]["d"], "nan")       # NaN is not valid JSON, so it is a string
        self.assertTrue(rows[0]["e"].startswith("<unprintable"))
        self.assertEqual(rows[1]["status"], 200)
        self.assertEqual(log.dropped, 0)

    def test_failed_write_is_counted_and_never_raises(self):
        log, path = self.make_test_logger()
        log._file.close()                 # simulate a broken disk or a closed file
        log.event("request", status=200)  # must not raise
        log.event("request", status=200)
        self.assertEqual(log.dropped, 2)
        log.close()                       # must not raise either

    def test_event_after_close_is_ignored(self):
        log, path = self.make_test_logger()
        log.event("startup")
        log.close()
        log.close()                       # idempotent
        log.event("request", status=200)
        self.assertEqual(self.names(path), ["startup"])

    def test_buffered_until_timer_when_loop_is_running(self):
        """With a running loop, normal events are flushed by the 1 s timer, not at once."""
        log, path = self.make_test_logger()

        async def scenario():
            log.event("request", status=200)
            self.assertEqual(path.read_text(), "")      # still only in memory
            await asyncio.sleep(0.4)
            self.assertEqual(len(read_lines(path)), 1)  # the timer wrote it
            log.close()

        with mock.patch.object(logger_module, "FLUSH_INTERVAL_S", 0.2):
            asyncio.run(scenario())

    def test_backend_down_is_written_immediately(self):
        log, path = self.make_test_logger()

        async def scenario():
            log.event("backend_down", backend="b3", reason="3 failures")
            self.assertEqual(self.names(path), ["backend_down"])
            log.close()

        asyncio.run(scenario())

    def test_timestamp_is_taken_at_call_time_not_flush_time(self):
        log, path = self.make_test_logger()
        first = datetime(2026, 10, 9, 14, 3, 21, 482000, tzinfo=timezone.utc)
        later = datetime(2026, 10, 9, 14, 3, 25, 0, tzinfo=timezone.utc)

        async def scenario():
            with mock.patch.object(logger_module, "_utc_now", lambda: first):
                log.event("request", status=200)     # buffered (loop is running)
            with mock.patch.object(logger_module, "_utc_now", lambda: later):
                log.close()                           # flushed 3.5 "seconds" later

        asyncio.run(scenario())
        self.assertEqual(read_lines(path)[0]["ts"], "2026-10-09T14:03:21.482Z")

    def test_burst_triggers_early_flush(self):
        log, path = self.make_test_logger()

        async def scenario():
            for i in range(10):
                log.event("request", i=i)
            self.assertEqual(len(read_lines(path)), 10)  # buffer limit reached, no timer needed
            log.close()

        with mock.patch.object(logger_module, "MAX_BUFFERED_LINES", 10):
            asyncio.run(scenario())


# ---------------------------------------------------------------- metrics

class TestMetrics(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        values = list(range(1, 101))                  # 1..100
        self.assertEqual(percentile(values, 50), 50)
        self.assertEqual(percentile(values, 95), 95)
        self.assertEqual(percentile(values, 99), 99)
        self.assertEqual(percentile(values, 100), 100)
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([7.0], 99), 7.0)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2)    # ceil(0.5 * 4) = rank 2

    def test_error_and_backend_counters(self):
        m = MetricsCollector()
        m.request_done("b1", 200, 5.0, 100, 1000, "10.0.1.11")
        m.request_done("b1", 502, 6.0, 100, 50, "10.0.1.11")
        m.request_done("b2", 200, 7.0, 10, 20, "10.0.1.12")
        m.request_done(None, 503, 0.5, 5, 30, "10.0.1.13")   # no healthy backend
        snap = m.snapshot()
        self.assertEqual(snap["total_requests"], 4)
        self.assertEqual(snap["errors"], 2)                   # the 502 and the None-backend one
        self.assertEqual(snap["per_backend"]["b1"], {
            "requests": 2, "errors": 1, "bytes_up": 200, "bytes_down": 1050})
        self.assertEqual(snap["per_backend"]["b2"]["errors"], 0)
        self.assertNotIn(None, snap["per_backend"])           # unrouted requests have no backend row

    def test_status_below_500_is_not_an_error(self):
        m = MetricsCollector()
        m.request_done("b1", 404, 1.0, 0, 0, "c")
        m.request_done("b1", 499, 1.0, 0, 0, "c")
        self.assertEqual(m.snapshot()["errors"], 0)

    def test_latency_percentiles_in_snapshot(self):
        m = MetricsCollector()
        for v in range(1, 101):
            m.request_done("b1", 200, float(v), 0, 0, "c")
        self.assertEqual(m.snapshot()["latency_ms"], {"p50": 50, "p95": 95, "p99": 99})

    def test_latency_samples_are_bounded(self):
        m = MetricsCollector(max_samples=10)
        for v in range(1, 26):                                # 25 requests, only last 10 kept
            m.request_done("b1", 200, float(v), 0, 0, "c")
        # Stored samples are 16..25, so the median is the 5th of them: 20.
        self.assertEqual(m.snapshot()["latency_ms"]["p50"], 20)
        self.assertEqual(m.snapshot()["total_requests"], 25)  # counters are not bounded

    def test_health_events_are_bounded_and_ordered(self):
        m = MetricsCollector()
        for i in range(150):
            m.health_transition("b3", i % 2 == 0, "reason %d" % i)
        events = m.snapshot()["health_events"]
        self.assertEqual(len(events), 100)
        self.assertEqual(events[0]["reason"], "reason 50")    # oldest kept
        self.assertEqual(events[-1]["reason"], "reason 149")  # newest last
        self.assertEqual(set(events[0]), {"ts", "backend", "healthy", "reason"})
        self.assertRegex(events[0]["ts"], TS_PATTERN)

    def test_snapshot_keys_and_json_serialisable(self):
        m = MetricsCollector()
        m.request_done("b1", 200, 1.23456, 1, 2, "c")
        m.health_transition("b1", False, "r")
        snap = m.snapshot()
        self.assertEqual(set(snap), {"uptime_s", "total_requests", "errors", "latency_ms",
                                     "per_backend", "health_events"})
        self.assertEqual(set(snap["latency_ms"]), {"p50", "p95", "p99"})
        json.dumps(snap)                                      # must not raise

    def test_floats_are_rounded_to_three_decimals(self):
        m = MetricsCollector()
        m.request_done("b1", 200, 1.23456789, 0, 0, "c")
        self.assertEqual(m.snapshot()["latency_ms"]["p50"], 1.235)

    def test_request_done_never_raises_and_ignores_bad_calls(self):
        m = MetricsCollector()
        m.request_done("b1", "oops", 1.0, 0, 0, "c")          # status is not a number
        m.request_done("b1", 200, None, 0, 0, "c")            # latency missing
        m.request_done("b1", 200, 1.0, None, 0, "c")          # bytes missing
        self.assertEqual(m.snapshot()["total_requests"], 0)   # nothing half-recorded
        self.assertEqual(m.internal_errors, 3)
        m.request_done("b1", 200, float("nan"), 0, 0, "c")    # counted, latency skipped
        snap = m.snapshot()
        self.assertEqual(snap["total_requests"], 1)
        self.assertEqual(snap["latency_ms"]["p50"], 0.0)

    def test_uptime_grows(self):
        m = MetricsCollector()
        first = m.snapshot()["uptime_s"]
        self.assertGreaterEqual(first, 0)
        self.assertGreaterEqual(m.snapshot()["uptime_s"], first)

    def test_factory_returns_collector(self):
        self.assertIsInstance(make_metrics({}), MetricsCollector)


if __name__ == "__main__":
    unittest.main()
