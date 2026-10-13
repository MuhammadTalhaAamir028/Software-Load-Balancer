"""Unit tests for the strict configuration loader."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from lb.config import ConfigError, load_config, validate_config


def valid_config() -> dict:
    """Return the complete valid example configuration."""
    return {
        "listen": {"host": "0.0.0.0", "port": 8080},
        "algorithm": "round_robin",
        "backends": [
            {"id": "b1", "host": "10.0.3.11", "port": 8000},
            {"id": "b2", "host": "10.0.3.12", "port": 8000},
            {"id": "b3", "host": "10.0.3.13", "port": 8000},
            {"id": "b4", "host": "10.0.3.14", "port": 8000},
        ],
        "health": {
            "path": "/health",
            "interval_s": 1.0,
            "timeout_s": 0.5,
            "fail_threshold": 3,
            "success_threshold": 2,
        },
        "timeouts": {"connect_s": 2.0, "read_s": 10.0, "keepalive_s": 15.0},
        "logging": {"path": "logs/lb.jsonl", "level": "INFO"},
        "stats": {"host": "0.0.0.0", "port": 9090},
    }


def minimal_config() -> dict:
    """Return the smallest valid configuration."""
    return {"backends": [{"id": "b1", "host": "10.0.3.11", "port": 8000}]}


class ConfigTestCase(unittest.TestCase):
    def assert_error(self, cfg: object, *substrings: str) -> None:
        """Assert validation fails and reports each requested path."""
        with self.assertRaises(ConfigError) as context:
            validate_config(cfg)  # type: ignore[arg-type]
        message = str(context.exception)
        for substring in substrings:
            self.assertIn(substring, message)

    def write_json(self, directory: str, name: str, value: object) -> Path:
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path


class TestLoadFile(ConfigTestCase):
    def test_valid_json_accepts_string_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "config.json", valid_config())
            self.assertEqual(load_config(str(path)), load_config(path))
            self.assertEqual(load_config(path), valid_config() | {"retry_on_connect_error": False})

    def test_missing_empty_whitespace_invalid_and_unsupported_files_raise_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(ConfigError) as context:
                load_config(missing)
            self.assertIn(str(missing), str(context.exception))

            for name, text in (
                ("empty.json", ""),
                ("spaces.json", " \n\t"),
                ("invalid.json", '{"backends":'),
            ):
                path = Path(directory) / name
                path.write_text(text, encoding="utf-8")
                with self.subTest(name=name):
                    with self.assertRaises(ConfigError) as error:
                        load_config(path)
                    self.assertIn(str(path), str(error.exception))

            for suffix in (".toml", ".txt"):
                path = Path(directory) / ("config" + suffix)
                path.write_text("{}", encoding="utf-8")
                with self.subTest(suffix=suffix):
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_json_top_level_list_and_config_error_inherit_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "list.json", [])
            with self.assertRaises(ConfigError) as context:
                load_config(path)
            self.assertIn("object", str(context.exception))
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_file_validation_error_contains_path_and_all_problems(self) -> None:
        cfg = minimal_config()
        cfg["listen"] = {"host": "", "port": 0}
        cfg["algorithm"] = "nope"
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "bad.json", cfg)
            with self.assertRaises(ConfigError) as context:
                load_config(path)
        message = str(context.exception)
        self.assertIn(str(path), message)
        self.assertIn("listen.host", message)
        self.assertIn("listen.port", message)
        self.assertIn("algorithm", message)

    def test_repository_example_loads_when_present(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        if not path.exists():
            self.skipTest("config.example.json is written later")
        self.assertIsInstance(load_config(path), dict)


class TestDefaults(ConfigTestCase):
    def test_valid_example_equals_input_plus_retry_default(self) -> None:
        raw = valid_config()
        expected = copy.deepcopy(raw)
        expected["retry_on_connect_error"] = False
        self.assertEqual(validate_config(raw), expected)

    def test_minimal_config_gets_all_defaults(self) -> None:
        expected = {
            "listen": {"host": "0.0.0.0", "port": 8080},
            "algorithm": "round_robin",
            "backends": [{"id": "b1", "host": "10.0.3.11", "port": 8000}],
            "health": {
                "path": "/health",
                "interval_s": 1.0,
                "timeout_s": 0.5,
                "fail_threshold": 3,
                "success_threshold": 2,
            },
            "timeouts": {"connect_s": 2.0, "read_s": 10.0, "keepalive_s": 15.0},
            "logging": {"path": "logs/lb.jsonl", "level": "INFO"},
            "stats": {"host": "0.0.0.0", "port": 9090},
            "retry_on_connect_error": False,
        }
        self.assertEqual(validate_config(minimal_config()), expected)

    def test_partial_sections_fill_only_missing_keys(self) -> None:
        cfg = minimal_config()
        cfg["health"] = {"interval_s": 2.0, "timeout_s": 1.0}
        result = validate_config(cfg)
        self.assertEqual(result["health"]["interval_s"], 2.0)
        self.assertEqual(result["health"]["timeout_s"], 1.0)
        self.assertEqual(result["health"]["path"], "/health")
        self.assertEqual(result["health"]["fail_threshold"], 3)
        self.assertEqual(result["health"]["success_threshold"], 2)

    def test_algorithms_and_backend_order(self) -> None:
        for algorithm in ("round_robin", "least_conn", "ip_hash"):
            cfg = minimal_config()
            cfg["algorithm"] = algorithm
            self.assertEqual(validate_config(cfg)["algorithm"], algorithm)
        for algorithm in ("nope", "Round_Robin", 5):
            with self.subTest(algorithm=algorithm):
                cfg = minimal_config()
                cfg["algorithm"] = algorithm
                self.assert_error(
                    cfg, "algorithm", "round_robin", "least_conn", "ip_hash"
                )
        cfg = minimal_config()
        cfg["backends"] = [
            {"id": "b2", "host": "h2", "port": 8001},
            {"id": "b1", "host": "h1", "port": 8000},
        ]
        self.assertEqual(
            [item["id"] for item in validate_config(cfg)["backends"]], ["b2", "b1"]
        )


class TestTypesAndBools(ConfigTestCase):
    def test_bool_where_integer_is_expected_is_rejected(self) -> None:
        cases = (
            ("listen", "port"),
            ("stats", "port"),
            ("backends", "port"),
            ("health", "fail_threshold"),
            ("health", "success_threshold"),
        )
        for section, key in cases:
            with self.subTest(section=section, key=key):
                cfg = minimal_config()
                if section == "backends":
                    cfg[section][0][key] = True
                else:
                    cfg[section] = {key: True}
                    if section == "listen":
                        cfg["listen"]["port"] = True
                path = f"backends[0].{key}" if section == "backends" else f"{section}.{key}"
                self.assert_error(cfg, path)

    def test_bool_where_float_is_expected_is_rejected(self) -> None:
        for section, key in (
            ("health", "interval_s"),
            ("health", "timeout_s"),
            ("timeouts", "connect_s"),
            ("timeouts", "read_s"),
            ("timeouts", "keepalive_s"),
        ):
            with self.subTest(section=section, key=key):
                cfg = minimal_config()
                cfg[section] = {key: True}
                path = f"backends[0].{key}" if section == "backends" else f"{section}.{key}"
                self.assert_error(cfg, path)

    def test_int_where_float_is_expected_becomes_float(self) -> None:
        cfg = minimal_config()
        cfg["health"] = {"interval_s": 2, "timeout_s": 1}
        cfg["timeouts"] = {"connect_s": 2, "read_s": 3, "keepalive_s": 4}
        result = validate_config(cfg)
        for section, keys in (
            ("health", ("interval_s", "timeout_s")),
            ("timeouts", ("connect_s", "read_s", "keepalive_s")),
        ):
            for key in keys:
                with self.subTest(section=section, key=key):
                    self.assertIsInstance(result[section][key], float)

    def test_ports_require_integer_range(self) -> None:
        for section in ("listen", "stats"):
            for value in ("8080", 0, -1, 65536):
                with self.subTest(section=section, value=value):
                    cfg = minimal_config()
                    cfg[section] = {"port": value}
                    self.assert_error(cfg, f"{section}.port")
        for value in ("8080", 0, -1, 65536):
            cfg = minimal_config()
            cfg["backends"][0]["port"] = value
            self.assert_error(cfg, "backends[0].port")
        for value in (1, 65535):
            cfg = minimal_config()
            cfg["listen"] = {"port": value}
            cfg["stats"] = {"port": 2 if value == 1 else 1}
            self.assertEqual(validate_config(cfg)["listen"]["port"], value)

    def test_non_object_sections_and_non_string_values_are_rejected(self) -> None:
        for section, value in (
            ("listen", "x"),
            ("health", []),
            ("backends", "b1"),
            ("timeouts", "x"),
            ("logging", "x"),
            ("stats", "x"),
        ):
            with self.subTest(section=section):
                cfg = minimal_config()
                cfg[section] = value
                self.assert_error(cfg, section)
        cfg = minimal_config()
        cfg["backends"][0] = "backend"
        self.assert_error(cfg, "backends[0]")
        for section, key in (
            ("listen", "host"),
            ("stats", "host"),
            ("backends", "host"),
        ):
            for value in (None, ""):
                with self.subTest(section=section, key=key, value=value):
                    cfg = minimal_config()
                    if section == "backends":
                        cfg[section][0][key] = value
                    else:
                        cfg[section] = {key: value}
                    path = (
                        f"backends[0].{key}"
                        if section == "backends"
                        else f"{section}.{key}"
                    )
                    self.assert_error(cfg, path)
        for section, key, value in (
            ("logging", "path", None),
            ("logging", "path", ""),
            ("health", "path", 5),
        ):
            cfg = minimal_config()
            cfg[section] = {key: value}
            self.assert_error(cfg, f"{section}.{key}")


class TestBackends(ConfigTestCase):
    def test_backend_count_duplicate_ids_and_id_regex(self) -> None:
        for entries in ([], [minimal_config()["backends"][0]] * 9):
            cfg = minimal_config()
            cfg["backends"] = entries
            self.assert_error(cfg, "backends")
        cfg = minimal_config()
        cfg["backends"] = [
            {"id": "b1", "host": "h1", "port": 1},
            {"id": "b1", "host": "h2", "port": 2},
        ]
        self.assert_error(cfg, "backends[1].id", "b1")
        accepted = ("b1", "web_01", "A-b", "a" * 16)
        for backend_id in accepted:
            cfg = minimal_config()
            cfg["backends"][0]["id"] = backend_id
            self.assertEqual(validate_config(cfg)["backends"][0]["id"], backend_id)
        for backend_id in ("", "b 1", "b/1", "a" * 17, 5):
            with self.subTest(backend_id=backend_id):
                cfg = minimal_config()
                cfg["backends"][0]["id"] = backend_id
                self.assert_error(cfg, "backends[0].id")

    def test_required_backend_fields_report_entry_paths(self) -> None:
        for key in ("id", "host", "port"):
            cfg = minimal_config()
            del cfg["backends"][0][key]
            self.assert_error(cfg, f"backends[0].{key}")

    def test_backend_list_accepts_one_and_eight_entries(self) -> None:
        for count in (1, 8):
            cfg = minimal_config()
            cfg["backends"] = [
                {"id": f"b{i}", "host": f"h{i}", "port": 8000 + i}
                for i in range(count)
            ]
            self.assertEqual(len(validate_config(cfg)["backends"]), count)


class TestHealthAndTimeouts(ConfigTestCase):
    def test_health_path_and_positive_values(self) -> None:
        cfg = minimal_config()
        cfg["health"] = {"path": "health"}
        self.assert_error(cfg, "health.path")
        for section, keys in (
            ("health", ("interval_s", "timeout_s", "fail_threshold", "success_threshold")),
            ("timeouts", ("connect_s", "read_s", "keepalive_s")),
        ):
            for key in keys:
                for value in (0, -1):
                    with self.subTest(section=section, key=key, value=value):
                        cfg = minimal_config()
                        cfg[section] = {key: value}
                        self.assert_error(cfg, f"{section}.{key}")

    def test_logging_levels_normalize_and_reject_invalid_values(self) -> None:
        for level in ("debug", "INFO", "WARNING", "ERROR"):
            cfg = minimal_config()
            cfg["logging"] = {"level": level}
            expected = level.upper()
            self.assertEqual(validate_config(cfg)["logging"]["level"], expected)
        for level in ("CRITICAL", "verbose", None):
            cfg = minimal_config()
            cfg["logging"] = {"level": level}
            self.assert_error(cfg, "logging.level")

    def test_retry_on_connect_error_requires_real_bool(self) -> None:
        for value in (True, False):
            cfg = minimal_config()
            cfg["retry_on_connect_error"] = value
            self.assertIs(validate_config(cfg)["retry_on_connect_error"], value)
        for value in ("yes", 0, 1, None):
            cfg = minimal_config()
            cfg["retry_on_connect_error"] = value
            self.assert_error(cfg, "retry_on_connect_error")


class TestCrossFieldRules(ConfigTestCase):
    def test_health_timeout_must_be_less_than_interval(self) -> None:
        for interval, timeout in ((1.0, 1.5), (1.0, 1.0)):
            cfg = minimal_config()
            cfg["health"] = {"interval_s": interval, "timeout_s": timeout}
            self.assert_error(cfg, "health.timeout_s", "less than interval_s")
        for interval, timeout in ((1.0, 0.5), (0.1, 0.05)):
            cfg = minimal_config()
            cfg["health"] = {"interval_s": interval, "timeout_s": timeout}
            validate_config(cfg)

    def test_listen_and_stats_ports_must_differ_including_defaults(self) -> None:
        for cfg in (
            dict(minimal_config(), listen={"port": 9090}),
            dict(minimal_config(), stats={"port": 8080}),
            dict(minimal_config(), listen={"port": 8080}, stats={"port": 8080}),
        ):
            self.assert_error(cfg, "stats.port")

    def test_invalid_timeout_does_not_add_cross_field_error(self) -> None:
        cfg = minimal_config()
        cfg["health"] = {"timeout_s": "not-a-number"}
        with self.assertRaises(ConfigError) as context:
            validate_config(cfg)
        message = str(context.exception)
        self.assertIn("health.timeout_s", message)
        self.assertNotIn("less than interval_s", message)


class TestUnknownKeys(ConfigTestCase):
    def test_unknown_top_level_key_is_rejected(self) -> None:
        cfg = minimal_config()
        cfg["algoritm"] = "round_robin"
        self.assert_error(cfg, "config.algoritm")

    def test_unknown_nested_keys_are_rejected(self) -> None:
        for section, key in (
            ("listen", "extra"),
            ("health", "fail_treshold"),
            ("timeouts", "extra"),
            ("logging", "extra"),
            ("stats", "extra"),
        ):
            with self.subTest(section=section):
                cfg = minimal_config()
                cfg[section] = {key: 1}
                self.assert_error(cfg, f"{section}.{key}")
        cfg = minimal_config()
        cfg["backends"][0]["extra"] = True
        self.assert_error(cfg, "backends[0].extra")


class TestAggregationAndImmutability(ConfigTestCase):
    def test_independent_errors_are_aggregated(self) -> None:
        cfg = minimal_config()
        cfg["algorithm"] = "invalid"
        cfg["listen"] = {"port": 0}
        cfg["health"] = {"interval_s": 0}
        with self.assertRaises(ConfigError) as context:
            validate_config(cfg)
        message = str(context.exception)
        self.assertGreaterEqual(len(message.splitlines()), 3)
        for path in ("algorithm", "listen.port", "health.interval_s"):
            self.assertIn(path, message)

    def test_input_is_not_mutated_and_result_is_independent(self) -> None:
        raw = minimal_config()
        original = copy.deepcopy(raw)
        result = validate_config(raw)
        self.assertEqual(raw, original)
        result["backends"][0]["host"] = "changed"
        result["health"]["path"] = "/changed"
        self.assertEqual(raw, original)
        self.assertIsNot(result["backends"][0], raw["backends"][0])

    def test_repeated_validation_is_equal(self) -> None:
        raw = minimal_config()
        self.assertEqual(validate_config(raw), validate_config(raw))


class TestYaml(ConfigTestCase):
    def test_missing_pyyaml_has_clear_error_for_both_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".yaml", ".yml"):
                path = Path(directory) / ("config" + suffix)
                path.write_text("backends: []", encoding="utf-8")
                with self.subTest(suffix=suffix), mock.patch.dict(sys.modules, {"yaml": None}):
                    with self.assertRaises(ConfigError) as context:
                        load_config(path)
                    message = str(context.exception)
                    self.assertIn("PyYAML", message)
                    self.assertIn(".json", message)

    def test_valid_yaml_matches_json_when_pyyaml_is_installed(self) -> None:
        if importlib.util.find_spec("yaml") is None:
            self.skipTest("PyYAML is not installed")
        yaml_text = (
            "backends:\n"
            "  - id: b1\n"
            "    host: 10.0.3.11\n"
            "    port: 8000\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            yaml_path = Path(directory) / "config.yaml"
            json_path = Path(directory) / "config.json"
            yaml_path.write_text(yaml_text, encoding="utf-8")
            json_path.write_text(json.dumps(minimal_config()), encoding="utf-8")
            self.assertEqual(load_config(yaml_path), load_config(json_path))
