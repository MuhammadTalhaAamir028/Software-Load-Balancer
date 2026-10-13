"""Load and validate the load balancer configuration.

JSON is canonical; YAML is optional when PyYAML is installed. Optional
sections and listener/statistics settings receive documented defaults, while
backend settings remain required.
Failure case: a misspelled key raises ``ConfigError`` naming the key and its
allowed alternatives instead of silently using a default.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = ["ConfigError", "load_config", "validate_config"]

# Keep this tuple in sync with make_balancer in lb.balancing.
_ALGORITHMS = ("round_robin", "least_conn", "ip_hash")
_TOP_LEVEL_KEYS = (
    "listen",
    "algorithm",
    "backends",
    "health",
    "timeouts",
    "logging",
    "stats",
    "retry_on_connect_error",
)
_SECTION_KEYS = MappingProxyType({
    "listen": ("host", "port"),
    "health": ("path", "interval_s", "timeout_s", "fail_threshold", "success_threshold"),
    "timeouts": ("connect_s", "read_s", "keepalive_s"),
    "logging": ("path", "level"),
    "stats": ("host", "port"),
})
_HEALTH_DEFAULTS = MappingProxyType({
    "path": "/health",
    "interval_s": 1.0,
    "timeout_s": 0.5,
    "fail_threshold": 3,
    "success_threshold": 2,
})
_LISTEN_DEFAULTS = MappingProxyType({"host": "0.0.0.0", "port": 8080})
_STATS_DEFAULTS = MappingProxyType({"host": "0.0.0.0", "port": 9090})
_TIMEOUT_DEFAULTS = MappingProxyType({
    "connect_s": 2.0,
    "read_s": 10.0,
    "keepalive_s": 15.0,
})
_LOGGING_DEFAULTS = MappingProxyType({"path": "logs/lb.jsonl", "level": "INFO"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_BACKEND_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or fails validation."""


def load_config(path: str) -> dict:
    """Read a JSON or optional YAML file and return normalized configuration."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix not in (".json", ".yaml", ".yml"):
        raise ConfigError(
            f"{file_path}: unsupported extension {suffix or '<none>'}; "
            "supported extensions: .json, .yaml, .yml"
        )

    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"{file_path}: unable to read file: {exc}") from exc

    if not text.strip():
        raise ConfigError(f"{file_path}: configuration file is empty")

    try:
        if suffix == ".json":
            raw = json.loads(text)
        else:
            try:
                import yaml
            except ImportError as exc:
                raise ConfigError(
                    f"{file_path}: install PyYAML or use a .json config."
                ) from exc
            try:
                raw = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                location = _parser_location(exc)
                detail = f": {location}" if location else ""
                raise ConfigError(f"{file_path}{detail}: {exc}") from exc
    except ConfigError:
        raise
    except (json.JSONDecodeError, ValueError, SyntaxError) as exc:
        location = _parser_location(exc)
        detail = f": {location}" if location else ""
        raise ConfigError(f"{file_path}{detail}: {exc}") from exc

    try:
        return validate_config(raw)
    except ConfigError as exc:
        raise ConfigError(f"{file_path}:\n{exc}") from exc


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a parsed mapping and return a new normalized configuration."""
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise ConfigError(f"config: expected an object/mapping, got {type(raw).__name__}")

    _check_unknown(raw, "config", _TOP_LEVEL_KEYS, errors)
    result: dict[str, Any] = {}

    listen = _optional_section(raw, "listen", _LISTEN_DEFAULTS, errors)
    if listen is not None:
        result["listen"] = _validate_host_port(listen, "listen", errors)

    algorithm = raw.get("algorithm")
    if "algorithm" not in raw:
        result["algorithm"] = "round_robin"
    elif not isinstance(algorithm, str) or algorithm not in _ALGORITHMS:
        errors.append(
            f"algorithm: unknown value {algorithm!r}; "
            f"valid values: {', '.join(_ALGORITHMS)}"
        )
    else:
        result["algorithm"] = algorithm

    backends = raw.get("backends")
    if "backends" not in raw:
        errors.append("backends: required")
    elif not isinstance(backends, list):
        errors.append(f"backends: must be a list, got {type(backends).__name__}")
    elif not 1 <= len(backends) <= 8:
        errors.append(
            f"backends: must contain between 1 and 8 entries, got {len(backends)}"
        )
        if not backends:
            result["backends"] = []
        else:
            result["backends"] = _validate_backends(backends, errors)
    else:
        result["backends"] = _validate_backends(backends, errors)

    health = _optional_section(raw, "health", _HEALTH_DEFAULTS, errors)
    if health is not None:
        result["health"] = _validate_health(health, errors)

    timeouts = _optional_section(raw, "timeouts", _TIMEOUT_DEFAULTS, errors)
    if timeouts is not None:
        result["timeouts"] = _validate_timeouts(timeouts, errors)

    logging = _optional_section(raw, "logging", _LOGGING_DEFAULTS, errors)
    if logging is not None:
        result["logging"] = _validate_logging(logging, errors)

    stats = _optional_section(raw, "stats", _STATS_DEFAULTS, errors)
    if stats is not None:
        result["stats"] = _validate_host_port(stats, "stats", errors)

    if (
        "listen" in result
        and "stats" in result
        and "port" in result["listen"]
        and "port" in result["stats"]
        and result["listen"]["port"] == result["stats"]["port"]
    ):
        errors.append("stats.port: must differ from listen.port")

    if "health" in result:
        health_result = result["health"]
        if (
            "timeout_s" in health_result
            and "interval_s" in health_result
            and health_result["timeout_s"] >= health_result["interval_s"]
        ):
            errors.append(
                "health.timeout_s: must be less than interval_s "
                f"(got {health_result['timeout_s']} >= {health_result['interval_s']})"
            )

    retry = raw.get("retry_on_connect_error", False)
    if not isinstance(retry, bool):
        errors.append(
            "retry_on_connect_error: must be a boolean, "
            f"got {retry!r}"
        )
    else:
        result["retry_on_connect_error"] = retry

    if errors:
        raise ConfigError("\n".join(errors))
    return result


def _parser_location(exc: BaseException) -> str:
    line = getattr(exc, "lineno", None)
    column = getattr(exc, "colno", None)
    if line is not None and column is not None:
        return f"line {line}, column {column}"
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return f"line {mark.line + 1}, column {mark.column + 1}"
    return ""


def _check_unknown(
    mapping: Mapping[str, Any],
    path: str,
    allowed: tuple[str, ...],
    errors: list[str],
) -> None:
    for key in mapping:
        if key not in allowed:
            errors.append(
                f"{path}.{key}: unknown key; allowed keys: {', '.join(allowed)}"
            )


def _required_section(
    raw: Mapping[str, Any], name: str, errors: list[str]
) -> Optional[dict]:
    if name not in raw:
        errors.append(f"{name}: required")
        return None
    value = raw[name]
    if not isinstance(value, Mapping):
        errors.append(f"{name}: must be an object/mapping, got {type(value).__name__}")
        return None
    _check_unknown(value, name, _SECTION_KEYS[name], errors)
    return dict(value)


def _optional_section(
    raw: Mapping[str, Any],
    name: str,
    defaults: Mapping[str, Any],
    errors: list[str],
) -> Optional[dict]:
    if name not in raw:
        return dict(defaults)
    value = raw[name]
    if not isinstance(value, Mapping):
        errors.append(f"{name}: must be an object/mapping, got {type(value).__name__}")
        return None
    _check_unknown(value, name, _SECTION_KEYS[name], errors)
    result = dict(defaults)
    result.update(value)
    return result


def _validate_host_port(
    section: Mapping[str, Any], path: str, errors: list[str]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("host", "port"):
        if key not in section:
            errors.append(f"{path}.{key}: required")
    if "host" in section and _valid_host(section["host"]):
        result["host"] = section["host"]
    elif "host" in section:
        errors.append(f"{path}.host: must be a non-empty string, got {section['host']!r}")
    if "port" in section:
        port = section["port"]
        if _valid_int(port) and 1 <= port <= 65535:
            result["port"] = port
        else:
            errors.append(
                f"{path}.port: must be an integer between 1 and 65535, got {port!r}"
            )
    return result


def _validate_backends(
    backends: list[Any], errors: list[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(backends):
        path = f"backends[{index}]"
        if not isinstance(value, Mapping):
            errors.append(f"{path}: must be an object/mapping, got {type(value).__name__}")
            continue
        _check_unknown(value, path, ("id", "host", "port"), errors)
        item: dict[str, Any] = {}
        for key in ("id", "host", "port"):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        backend_id = value.get("id")
        if not isinstance(backend_id, str) or not backend_id:
            errors.append(f"{path}.id: must be a non-empty string, got {backend_id!r}")
        elif not _BACKEND_ID_PATTERN.fullmatch(backend_id):
            errors.append(
                f"{path}.id: must match ^[A-Za-z0-9_-]{{1,16}}$, got {backend_id!r}"
            )
        elif backend_id in seen:
            errors.append(f"{path}.id: duplicate backend id {backend_id!r}")
        else:
            seen.add(backend_id)
            item["id"] = backend_id
        host = value.get("host")
        if _valid_host(host):
            item["host"] = host
        elif "host" in value:
            errors.append(f"{path}.host: must be a non-empty string, got {host!r}")
        port = value.get("port")
        if "port" in value:
            if _valid_int(port) and 1 <= port <= 65535:
                item["port"] = port
            else:
                errors.append(
                    f"{path}.port: must be an integer between 1 and 65535, got {port!r}"
                )
        if len(item) == 3:
            result.append(item)
    return result


def _validate_health(section: Mapping[str, Any], errors: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    path = section["path"]
    if not isinstance(path, str) or not path.startswith("/"):
        errors.append(f"health.path: must be a string starting with '/', got {path!r}")
    else:
        result["path"] = path
    for key in ("interval_s", "timeout_s"):
        value = section[key]
        if _valid_float(value) and value > 0:
            result[key] = float(value)
        else:
            errors.append(f"health.{key}: must be a number greater than 0, got {value!r}")
    for key in ("fail_threshold", "success_threshold"):
        value = section[key]
        if _valid_int(value) and value >= 1:
            result[key] = value
        else:
            errors.append(f"health.{key}: must be an integer >= 1, got {value!r}")
    return result


def _validate_timeouts(
    section: Mapping[str, Any], errors: list[str]
) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in ("connect_s", "read_s", "keepalive_s"):
        value = section[key]
        if _valid_float(value) and value > 0:
            result[key] = float(value)
        else:
            errors.append(f"timeouts.{key}: must be a number greater than 0, got {value!r}")
    return result


def _validate_logging(
    section: Mapping[str, Any], errors: list[str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    path = section["path"]
    if isinstance(path, str) and path:
        result["path"] = path
    else:
        errors.append(f"logging.path: must be a non-empty string, got {path!r}")
    level = section["level"]
    if isinstance(level, str) and level.upper() in _LOG_LEVELS:
        result["level"] = level.upper()
    else:
        errors.append(
            f"logging.level: must be DEBUG, INFO, WARNING, or ERROR, got {level!r}"
        )
    return result


def _valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_float(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_host(value: Any) -> bool:
    return isinstance(value, str) and bool(value)
