"""Structured JSON-lines event logger (module lb.obs.logger).

Implements the frozen EventLogger contract from lb/interfaces.py:

    event(name, **fields) -> None     # writes ONE JSON line, the logger adds "ts"

Design goals (see the Project 5 observability brief, part A):
  * event() never raises and never blocks the asyncio event loop;
  * the timestamp is taken when event() is called, not when the line is flushed;
  * the always-on events "backend_down" and "backend_up" are written whatever
    the configured level is, because the failure-detection metric depends on them.

Standard library only. The logger is intended to be used from a single thread
(the asyncio event loop); it is not thread-safe.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

# Numeric severity of each configurable level (same order as the brief).
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

# Level of every known event. Unknown event names default to INFO.
EVENT_LEVELS = {
    "startup": LEVELS["INFO"],
    "shutdown": LEVELS["INFO"],
    "request": LEVELS["INFO"],
    "backend_selected": LEVELS["DEBUG"],
    "backend_error": LEVELS["WARNING"],
    "client_error": LEVELS["INFO"],
    "health_probe": LEVELS["DEBUG"],
}

# These events bypass the level filter completely.
ALWAYS_EVENTS = frozenset(("backend_down", "backend_up"))

# Buffered lines are written to disk at most this many seconds later.
FLUSH_INTERVAL_S = 1.0

# Safety valve: when this many lines are waiting, flush at once instead of
# waiting for the timer, so memory use stays bounded during a burst.
MAX_BUFFERED_LINES = 5000


def _utc_now() -> datetime:
    """Return the current UTC time. A separate function so tests can replace it."""
    return datetime.now(timezone.utc)


def utc_iso_ms(now: Optional[datetime] = None) -> str:
    """Format a time as UTC ISO-8601 with milliseconds and a trailing Z.

    Example: 2026-10-09T14:03:21.482Z (this is the format required by the brief).
    """
    if now is None:
        now = _utc_now()
    # strftime has no millisecond directive, so the milliseconds are appended
    # manually: microsecond // 1000 truncates (does not round) to milliseconds.
    return now.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (now.microsecond // 1000)


def _safe_str(value: Any) -> str:
    """str(value) that cannot raise, even if the object's __str__ is broken."""
    try:
        return str(value)
    except Exception:
        return "<unprintable %s>" % type(value).__name__


def _json_safe(value: Any) -> Any:
    """Return value unchanged if it can be encoded as strict JSON, else str(value).

    allow_nan=False rejects NaN and Infinity, which are not valid JSON and would
    break tools such as jq.
    """
    try:
        json.dumps(value, default=_safe_str, allow_nan=False)
        return value
    except Exception:
        return _safe_str(value)


def _render(ts: str, name: str, fields: Mapping[str, Any]) -> str:
    """Build one complete JSON line (including the trailing newline)."""
    record = {"ts": ts, "event": name}
    for key, value in fields.items():
        # A caller field must never overwrite the two reserved keys.
        record[key + "_" if key in ("ts", "event") else key] = value
    try:
        # default=_safe_str converts any non-serialisable object with str().
        return json.dumps(record, default=_safe_str, allow_nan=False,
                          separators=(",", ":")) + "\n"
    except Exception:
        # Rare path (NaN, circular reference, odd dict keys): sanitise field by
        # field so that one bad value does not destroy the whole line.
        cleaned = {k: _json_safe(v) for k, v in record.items()}
        return json.dumps(cleaned, default=_safe_str,
                          separators=(",", ":")) + "\n"


class JsonlEventLogger:
    """Append-only JSON-lines logger with a once-per-second flush.

    Attributes:
        dropped  number of lines lost because a write failed or an error
                 occurred while handling the event (see the brief).
    """

    def __init__(self, path: str, level: str = "INFO") -> None:
        # An unknown level name falls back to INFO instead of failing,
        # because the config loader is owned by another team member.
        self._threshold = LEVELS.get(_safe_str(level).upper(), LEVELS["INFO"])
        self.path = path
        self.dropped = 0

        # Create the log directory when it is missing.
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Append mode: a restart never erases earlier lines. If this open fails
        # the exception is allowed to propagate on purpose: a wrong log path is a
        # configuration error that should stop the start-up loudly, whereas
        # event() itself must never raise.
        self._file = open(path, "a", encoding="utf-8")

        self._buffer: List[str] = []
        self._timer: Optional[asyncio.TimerHandle] = None
        self._closed = False

    # ------------------------------------------------------------------ API

    def event(self, name: str, **fields: Any) -> None:
        """Record one event. Never raises."""
        try:
            if self._closed:
                return
            always = name in ALWAYS_EVENTS
            # Level filter first: it is the cheapest check and removes most
            # events (for example "request" during a WARNING benchmark run).
            if not always and EVENT_LEVELS.get(name, LEVELS["INFO"]) < self._threshold:
                return

            # The timestamp is taken HERE, at call time, not at flush time.
            line = _render(utc_iso_ms(), name, fields)
            self._buffer.append(line)

            if always or len(self._buffer) >= MAX_BUFFERED_LINES:
                # Rare, important events are written at once so that they are on
                # disk even if the process crashes a moment later.
                self._flush()
            else:
                self._schedule_flush()
        except Exception:
            # The contract says event() must not raise; count the lost line.
            self.dropped += 1

    def close(self) -> None:
        """Flush remaining lines and close the file. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._flush()
        try:
            self._file.close()
        except Exception:
            pass

    # ------------------------------------------------------------- internals

    def _schedule_flush(self) -> None:
        """Arrange for the buffer to be written within FLUSH_INTERVAL_S seconds."""
        if self._timer is not None:
            return  # a flush is already scheduled
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop is running (for example in a plain script or test):
            # there is nothing to block, so write immediately.
            self._flush()
            return
        # call_later runs _on_timer on the event loop after the interval, so no
        # extra thread is needed and the loop is never blocked while waiting.
        self._timer = loop.call_later(FLUSH_INTERVAL_S, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self._flush()

    def _flush(self) -> None:
        """Write all buffered lines to the file. Failed writes are counted, not raised."""
        if not self._buffer:
            return
        lines = self._buffer
        self._buffer = []
        try:
            self._file.write("".join(lines))
            self._file.flush()
        except Exception:
            self.dropped += len(lines)


def make_logger(cfg: Mapping[str, Any]) -> JsonlEventLogger:
    """Factory required by lb/interfaces.py: make_logger(cfg) -> EventLogger.

    Reads cfg["logging"]["path"] (default logs/lb.jsonl) and
    cfg["logging"]["level"] (default INFO).
    """
    section = cfg.get("logging") or {}
    return JsonlEventLogger(
        path=section.get("path", "logs/lb.jsonl"),
        level=section.get("level", "INFO"),
    )