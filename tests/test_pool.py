"""Unit tests for the shared backend pool."""
from __future__ import annotations

from typing import Any, Optional
import unittest

from lb.balancing import make_pool
from lb.balancing.pool import SimpleBackendPool
from lb.config import validate_config
from lb.interfaces import Backend
from tests.fakes import FakeLogger, FakeMetrics


def four_backend_cfg() -> dict:
    """Return a fresh validated configuration with four backends."""
    return validate_config(
        {
            "backends": [
                {"id": "b1", "host": "10.0.3.11", "port": 8000},
                {"id": "b2", "host": "10.0.3.12", "port": 8000},
                {"id": "b3", "host": "10.0.3.13", "port": 8000},
                {"id": "b4", "host": "10.0.3.14", "port": 8000},
            ]
        }
    )


def make_pool_under_test(
    log: Optional[FakeLogger] = None,
    metrics: Optional[FakeMetrics] = None,
) -> tuple[SimpleBackendPool, FakeLogger, FakeMetrics]:
    """Create a fresh pool and recording doubles."""
    actual_log = log if log is not None else FakeLogger()
    actual_metrics = metrics if metrics is not None else FakeMetrics()
    pool = SimpleBackendPool(four_backend_cfg(), actual_log, actual_metrics)
    return pool, actual_log, actual_metrics


class TestConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.pool, self.log, self.metrics = make_pool_under_test()

    def test_builds_backends_in_config_order_with_defaults(self) -> None:
        backends = self.pool.all()
        self.assertEqual([backend.id for backend in backends], ["b1", "b2", "b3", "b4"])
        self.assertEqual(
            [(backend.host, backend.port) for backend in backends],
            [
                ("10.0.3.11", 8000),
                ("10.0.3.12", 8000),
                ("10.0.3.13", 8000),
                ("10.0.3.14", 8000),
            ],
        )
        self.assertTrue(all(backend.healthy for backend in backends))
        self.assertTrue(all(backend.active == 0 for backend in backends))

    def test_all_elements_are_backend_instances(self) -> None:
        self.assertTrue(all(isinstance(backend, Backend) for backend in self.pool.all()))

    def test_pool_does_not_hold_config_entries(self) -> None:
        cfg = four_backend_cfg()
        pool = SimpleBackendPool(cfg, FakeLogger(), FakeMetrics())
        cfg["backends"][0]["host"] = "changed"
        cfg["backends"].append({"id": "b5", "host": "h5", "port": 8000})
        self.assertEqual(pool.all()[0].host, "10.0.3.11")
        self.assertEqual(len(pool.all()), 4)

    def test_single_and_eight_backend_configs_work(self) -> None:
        for count in (1, 8):
            with self.subTest(count=count):
                cfg = four_backend_cfg()
                cfg["backends"] = [
                    {"id": f"b{i}", "host": f"10.0.3.{i}", "port": 8000}
                    for i in range(1, count + 1)
                ]
                pool = SimpleBackendPool(cfg, FakeLogger(), FakeMetrics())
                self.assertEqual(len(pool.all()), count)

    def test_construction_is_silent(self) -> None:
        self.assertEqual(self.log.events, [])
        self.assertEqual(self.metrics.transitions, [])


class TestAccessors(unittest.TestCase):
    def setUp(self) -> None:
        self.pool, self.log, self.metrics = make_pool_under_test()

    def test_all_returns_new_mutable_lists(self) -> None:
        first = self.pool.all()
        second = self.pool.all()
        self.assertIsNot(first, second)
        first.append(first[0])
        second.clear()
        self.assertEqual(len(self.pool.all()), 4)

    def test_healthy_returns_new_lists_in_config_order(self) -> None:
        first = self.pool.healthy()
        second = self.pool.healthy()
        self.assertIsInstance(first, list)
        self.assertIsNot(first, second)
        self.assertEqual([backend.id for backend in first], ["b1", "b2", "b3", "b4"])

    def test_backend_objects_are_shared_across_accessors(self) -> None:
        all_backends = self.pool.all()
        healthy_backends = self.pool.healthy()
        for left, right in zip(all_backends, self.pool.all()):
            self.assertIs(left, right)
        for left, right in zip(all_backends, healthy_backends):
            self.assertIs(left, right)
        all_backends[0].active = 5
        self.assertEqual(self.pool.all()[0].active, 5)
        self.assertEqual(self.pool.healthy()[0].active, 5)

    def test_health_removal_preserves_config_order_and_readds_in_place(self) -> None:
        self.pool.set_health("b3", False, "down")
        self.assertEqual([backend.id for backend in self.pool.healthy()], ["b1", "b2", "b4"])
        self.assertEqual(len(self.pool.all()), 4)
        self.pool.set_health("b3", True, "up")
        self.assertEqual([backend.id for backend in self.pool.healthy()], ["b1", "b2", "b3", "b4"])

    def test_all_backends_down_returns_empty_healthy_list(self) -> None:
        for backend in self.pool.all():
            self.pool.set_health(backend.id, False, "down")
        self.assertEqual(self.pool.healthy(), [])
        self.assertEqual(len(self.pool.all()), 4)


class TestAcquireRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.pool, self.log, self.metrics = make_pool_under_test()

    def test_acquire_and_release_balance_active_counts(self) -> None:
        backends = self.pool.all()
        for backend in backends:
            for _ in range(3):
                self.pool.acquire(backend)
            for _ in range(3):
                self.pool.release(backend)
        self.assertEqual([backend.active for backend in backends], [0, 0, 0, 0])

    def test_acquire_and_release_do_not_touch_other_backends(self) -> None:
        first, second = self.pool.all()[:2]
        self.pool.acquire(first)
        self.assertEqual(first.active, 1)
        self.assertEqual(second.active, 0)
        self.pool.release(first)
        self.assertEqual(second.active, 0)

    def test_normal_acquire_release_is_silent(self) -> None:
        backend = self.pool.all()[0]
        self.pool.acquire(backend)
        self.pool.release(backend)
        self.assertEqual(self.log.events, [])
        self.assertEqual(self.metrics.transitions, [])

    def test_down_backend_can_be_acquired_and_released(self) -> None:
        backend = self.pool.all()[0]
        self.pool.set_health(backend.id, False, "down")
        self.pool.acquire(backend)
        self.pool.release(backend)
        self.assertFalse(backend.healthy)
        self.assertEqual(backend.active, 0)

    def test_over_release_clamps_and_logs_each_underflow(self) -> None:
        backend = self.pool.all()[0]
        self.pool.release(backend)
        self.pool.release(backend)
        self.assertEqual(backend.active, 0)
        self.assertEqual(self.log.names(), ["backend_error", "backend_error"])
        self.assertEqual(
            self.log.named("backend_error"),
            [
                {"backend": backend.id, "error": "release_underflow"},
                {"backend": backend.id, "error": "release_underflow"},
            ],
        )

    def test_second_release_after_active_one_is_underflow(self) -> None:
        backend = self.pool.all()[0]
        self.pool.acquire(backend)
        self.pool.release(backend)
        self.pool.release(backend)
        self.assertEqual(self.log.names(), ["backend_error"])

    def test_release_does_not_raise_when_logger_raises(self) -> None:
        pool, log, _ = make_pool_under_test(FakeLogger(RuntimeError("boom")))
        backend = pool.all()[0]
        pool.release(backend)
        self.assertEqual(backend.active, 0)
        self.assertEqual(log.names(), ["backend_error"])

    def test_acquire_release_do_not_change_health(self) -> None:
        backend = self.pool.all()[0]
        self.pool.acquire(backend)
        self.pool.release(backend)
        self.assertTrue(backend.healthy)


class TestSetHealth(unittest.TestCase):
    def setUp(self) -> None:
        self.pool, self.log, self.metrics = make_pool_under_test()

    def test_same_state_is_silent_no_op(self) -> None:
        self.pool.set_health("b1", True, "unchanged")
        self.assertEqual(self.log.events, [])
        self.assertEqual(self.metrics.transitions, [])

    def test_repeated_down_transition_logs_once(self) -> None:
        self.pool.set_health("b3", False, "failure")
        self.pool.set_health("b3", False, "failure-again")
        self.assertEqual(self.log.names(), ["backend_down"])
        self.assertEqual(self.metrics.transitions, [("b3", False, "failure")])

    def test_down_transition_updates_state_and_notifies(self) -> None:
        self.pool.set_health("b3", False, "3 consecutive failures: timeout")
        self.assertFalse(self.pool.all()[2].healthy)
        self.assertEqual(
            self.log.named("backend_down"),
            [{"backend": "b3", "reason": "3 consecutive failures: timeout"}],
        )
        self.assertEqual(
            self.metrics.transitions,
            [("b3", False, "3 consecutive failures: timeout")],
        )

    def test_down_then_up_preserves_event_and_metric_order(self) -> None:
        reason_down = "3 consecutive failures: timeout"
        reason_up = "recovered"
        self.pool.set_health("b3", False, reason_down)
        self.pool.set_health("b3", True, reason_up)
        self.assertEqual(self.log.names(), ["backend_down", "backend_up"])
        self.assertEqual(
            self.log.named("backend_down") + self.log.named("backend_up"),
            [
                {"backend": "b3", "reason": reason_down},
                {"backend": "b3", "reason": reason_up},
            ],
        )
        self.assertEqual(
            self.metrics.transitions,
            [("b3", False, reason_down), ("b3", True, reason_up)],
        )
        self.assertTrue(self.pool.all()[2].healthy)

    def test_logging_precedes_metrics_after_state_update(self) -> None:
        order: list[str] = []
        captured_health: list[bool] = []

        class OrderedLogger(FakeLogger):
            def event(self, name: str, **fields: Any) -> None:
                order.append(f"log:{name}")
                captured_health.append(pool.all()[2].healthy)
                super().event(name, **fields)

        class OrderedMetrics(FakeMetrics):
            def health_transition(
                self, backend_id: str, healthy: bool, reason: str
            ) -> None:
                order.append("metrics")
                super().health_transition(backend_id, healthy, reason)

        pool = SimpleBackendPool(
            four_backend_cfg(), OrderedLogger(), OrderedMetrics()
        )
        pool.set_health("b3", False, "down")
        self.assertEqual(order, ["log:backend_down", "metrics"])
        self.assertEqual(captured_health, [False])

    def test_unknown_id_raises_key_error_without_side_effects(self) -> None:
        with self.assertRaises(KeyError) as context:
            self.pool.set_health("missing", False, "down")
        self.assertIn("missing", str(context.exception))
        self.assertEqual(self.log.events, [])
        self.assertEqual(self.metrics.transitions, [])
        self.assertEqual([backend.healthy for backend in self.pool.all()], [True] * 4)

    def test_set_health_changes_only_named_backend(self) -> None:
        self.pool.set_health("b2", False, "down")
        self.assertEqual(
            [backend.healthy for backend in self.pool.all()],
            [True, False, True, True],
        )

    def test_reasons_are_passed_through_unchanged(self) -> None:
        reasons = (
            "refused",
            "unreachable",
            "reset",
            "timeout",
            "bad_status:503",
            "malformed_response",
            "error:ValueError",
            "recovered",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                pool, log, metrics = make_pool_under_test()
                pool.set_health("b1", False, reason)
                self.assertEqual(log.named("backend_down")[0]["reason"], reason)
                self.assertEqual(metrics.transitions[0][2], reason)

    def test_set_health_does_not_change_active(self) -> None:
        backend = self.pool.all()[0]
        backend.active = 7
        self.pool.set_health(backend.id, False, "down")
        self.assertEqual(backend.active, 7)


class TestFactory(unittest.TestCase):
    def test_factory_returns_independent_simple_pools(self) -> None:
        cfg = four_backend_cfg()
        first = make_pool(cfg, FakeLogger(), FakeMetrics())
        second = make_pool(cfg, FakeLogger(), FakeMetrics())
        self.assertIsInstance(first, SimpleBackendPool)
        self.assertEqual([b.id for b in first.all()], ["b1", "b2", "b3", "b4"])
        first.set_health("b1", False, "down")
        self.assertTrue(second.all()[0].healthy)

    def test_factory_result_has_all_protocol_methods(self) -> None:
        pool = make_pool(four_backend_cfg(), FakeLogger(), FakeMetrics())
        for name in ("all", "healthy", "acquire", "release", "set_health"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(pool, name)))


class TestFailureIsolation(unittest.TestCase):
    def test_logger_error_propagates_after_state_and_event(self) -> None:
        log = FakeLogger(RuntimeError("boom"))
        pool, _, _ = make_pool_under_test(log=log)
        with self.assertRaises(RuntimeError):
            pool.set_health("b2", False, "x")
        self.assertFalse(pool.all()[1].healthy)
        self.assertEqual(log.names(), ["backend_down"])

    def test_metrics_error_propagates_and_later_transition_works(self) -> None:
        metrics = FakeMetrics(RuntimeError("boom"))
        pool, log, _ = make_pool_under_test(metrics=metrics)
        with self.assertRaises(RuntimeError):
            pool.set_health("b2", False, "x")
        self.assertFalse(pool.all()[1].healthy)
        self.assertEqual(log.names(), ["backend_down"])
        pool._metrics = FakeMetrics()
        pool.set_health("b2", True, "recovered")
        self.assertTrue(pool.all()[1].healthy)
