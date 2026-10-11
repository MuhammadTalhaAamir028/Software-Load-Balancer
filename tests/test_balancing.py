from __future__ import annotations

from collections import Counter
import inspect
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from typing import List, Optional
import unittest

from lb.balancing import make_balancer
from lb.balancing.ip_hash import IPHash
from lb.balancing.least_conn import LeastConnections
from lb.balancing.round_robin import RoundRobin
from lb.interfaces import Backend


def make_backends(n: int = 4, active: Optional[List[int]] = None) -> List[Backend]:
    """Return fresh backends in the project's configuration order."""
    counts = [0] * n if active is None else list(active)
    if len(counts) != n:
        raise ValueError("active count must match backend count")
    return [
        Backend(
            id=f"b{i}",
            host=f"10.0.3.{10 + i}",
            port=8000,
            healthy=True,
            active=counts[i - 1],
        )
        for i in range(1, n + 1)
    ]


class TestRoundRobin(unittest.TestCase):
    def test_first_six_selections_rotate_in_order(self) -> None:
        balancer = RoundRobin()
        backends = make_backends()
        selected = [balancer.select("client", backends).id for _ in range(6)]
        self.assertEqual(selected, ["b1", "b2", "b3", "b4", "b1", "b2"])

    def test_even_split_is_exact(self) -> None:
        balancer = RoundRobin()
        backends = make_backends()
        counts = Counter(balancer.select("client", backends).id for _ in range(1000))
        self.assertEqual(counts, Counter({"b1": 250, "b2": 250, "b3": 250, "b4": 250}))

    def test_fairness_survives_removal_and_readdition(self) -> None:
        balancer = RoundRobin()
        backends = make_backends()
        for _ in range(7):
            balancer.select("client", backends)

        reduced = [backends[0], backends[1], backends[3]]
        reduced_counts = Counter(
            balancer.select("client", reduced).id for _ in range(3000)
        )
        self.assertEqual(
            reduced_counts,
            Counter({"b1": 1000, "b2": 1000, "b4": 1000}),
        )

        full_counts = Counter(
            balancer.select("client", backends).id for _ in range(4000)
        )
        self.assertEqual(
            full_counts,
            Counter({"b1": 1000, "b2": 1000, "b3": 1000, "b4": 1000}),
        )

    def test_returns_the_original_backend_object(self) -> None:
        backends = make_backends()
        self.assertIs(RoundRobin().select("client", backends), backends[0])

    def test_name_is_round_robin(self) -> None:
        self.assertEqual(RoundRobin.name, "round_robin")

    def test_instances_have_independent_counters(self) -> None:
        backends = make_backends()
        first = RoundRobin()
        second = RoundRobin()
        first.select("client", backends)
        self.assertIs(second.select("client", backends), backends[0])


class TestLeastConnections(unittest.TestCase):
    def test_brief_example_uses_first_tied_minimum(self) -> None:
        backends = make_backends(active=[3, 1, 1, 2])
        self.assertIs(LeastConnections().select("client", backends), backends[1])

    def test_active_vectors_select_expected_backend(self) -> None:
        cases = (
            ([0, 0, 5, 9], "b1"),
            ([5, 5, 5, 5], "b1"),
            ([9, 8, 7, 6], "b4"),
            ([2, 0, 0, 0], "b2"),
            ([4, 3, 2, 1], "b4"),
        )
        for active, expected in cases:
            with self.subTest(active=active):
                selected = LeastConnections().select("client", make_backends(active=active))
                self.assertEqual(selected.id, expected)

    def test_ties_are_deterministic_over_repeated_calls(self) -> None:
        backends = make_backends(active=[4, 1, 1, 3])
        balancer = LeastConnections()
        selected = [balancer.select("client", backends) for _ in range(100)]
        self.assertTrue(all(backend is backends[1] for backend in selected))

    def test_client_ip_is_unused_and_identity_is_preserved(self) -> None:
        backends = make_backends(active=[5, 2, 3, 4])
        balancer = LeastConnections()
        for client_ip in ("192.0.2.1", "", "garbage"):
            with self.subTest(client_ip=client_ip):
                self.assertIs(balancer.select(client_ip, backends), backends[1])

    def test_simulation_balances_equal_backends(self) -> None:
        backends = make_backends()
        balancer = LeastConnections()
        for _ in range(400):
            balancer.select("client", backends).active += 1
        self.assertEqual([backend.active for backend in backends], [100] * 4)
        backends[0].active -= 1
        self.assertIs(balancer.select("client", backends), backends[0])

    def test_name_is_least_conn(self) -> None:
        self.assertEqual(LeastConnections.name, "least_conn")


class TestIpHash(unittest.TestCase):
    def test_same_ip_is_stable(self) -> None:
        backends = make_backends()
        balancer = IPHash()
        selected = [balancer.select("10.0.1.11", backends) for _ in range(100)]
        self.assertTrue(all(backend is selected[0] for backend in selected))

    def test_exact_mapping_with_four_healthy_backends(self) -> None:
        backends = make_backends()
        expected = {
            "10.0.1.11": "b3",
            "10.0.1.12": "b2",
            "10.0.1.13": "b2",
            "10.0.1.14": "b2",
        }
        # This documented table must not be tuned; its unevenness is a finding.
        for client_ip, backend_id in expected.items():
            with self.subTest(client_ip=client_ip):
                self.assertEqual(IPHash().select(client_ip, backends).id, backend_id)

    def test_exact_mapping_with_b3_removed(self) -> None:
        backends = make_backends()
        reduced = [backends[0], backends[1], backends[3]]
        expected = {
            "10.0.1.11": "b1",
            "10.0.1.12": "b1",
            "10.0.1.13": "b2",
            "10.0.1.14": "b2",
        }
        # This documented table must not be tuned; its reshuffle is expected.
        for client_ip, backend_id in expected.items():
            with self.subTest(client_ip=client_ip):
                self.assertEqual(IPHash().select(client_ip, reduced).id, backend_id)

    def test_shrinking_the_healthy_set_reshuffles_clients(self) -> None:
        backends = make_backends()
        reduced = [backends[0], backends[1], backends[3]]
        clients = [f"10.0.1.{i}" for i in range(11, 15)]
        full = {ip: IPHash().select(ip, backends).id for ip in clients}
        changed = {ip: IPHash().select(ip, reduced).id for ip in clients}
        self.assertTrue(any(full[ip] != changed[ip] for ip in clients))

    def test_hash_spread_is_reasonable_for_four_and_three_backends(self) -> None:
        rng = random.Random(3001)
        addresses = [
            ".".join(str(rng.randrange(256)) for _ in range(4))
            for _ in range(1000)
        ]
        balancer = IPHash()
        for backends, lower, upper in (
            (make_backends(4), 150, 350),
            (make_backends(3), 200, 470),
        ):
            counts = Counter(balancer.select(ip, backends).id for ip in addresses)
            with self.subTest(backends=len(backends)):
                self.assertTrue(all(lower <= counts[b.id] <= upper for b in backends))

    def test_hash_is_stable_across_python_processes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        clients = [f"10.0.1.{i}" for i in range(11, 15)] + ["2001:db8::1"]
        backends = make_backends()
        expected = {
            ip: IPHash().select(ip, backends).id
            for ip in clients
        }
        code = (
            "import json\n"
            "from lb.balancing.ip_hash import IPHash\n"
            "from lb.interfaces import Backend\n"
            "clients = [f'10.0.1.{i}' for i in range(11, 15)] + ['2001:db8::1']\n"
            "backends = [Backend(f'b{i}', 'host', 8000) for i in range(1, 5)]\n"
            "print(json.dumps({ip: IPHash().select(ip, backends).id for ip in clients}, sort_keys=True))\n"
        )
        outputs = []
        for seed in ("1", "2"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            env["PYTHONPATH"] = str(root)
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], expected)

    def test_ipv6_spellings_normalize_to_the_same_backend(self) -> None:
        backends = make_backends()
        selected = [
            IPHash().select(ip, backends)
            for ip in ("2001:db8::1", "2001:DB8::1", "2001:0db8:0:0:0:0:0:1")
        ]
        self.assertIs(selected[0], selected[1])
        self.assertIs(selected[1], selected[2])

    def test_ipv6_and_odd_inputs_do_not_crash(self) -> None:
        backends = make_backends()
        inputs = (
            "::1",
            "2001:db8::1",
            "::ffff:10.0.1.11",
            "fe80::1%eth0",
            "",
            "not-an-ip",
            " 10.0.1.11",
        )
        for client_ip in inputs:
            with self.subTest(client_ip=client_ip):
                first = IPHash().select(client_ip, backends)
                second = IPHash().select(client_ip, backends)
                self.assertIn(first, backends)
                self.assertIs(first, second)

    def test_instances_are_stateless(self) -> None:
        backends = make_backends()
        clients = ("10.0.1.11", "2001:db8::1", "not-an-ip")
        first = IPHash()
        second = IPHash()
        first_results = [first.select(ip, backends) for ip in clients]
        second_results = [second.select(ip, backends) for ip in clients]
        self.assertEqual(first_results, second_results)
        self.assertEqual(first.select(clients[0], backends), first_results[0])

    def test_returns_original_backend_and_has_name(self) -> None:
        backends = make_backends()
        self.assertIs(IPHash().select("10.0.1.11", backends), backends[2])
        self.assertEqual(IPHash.name, "ip_hash")


class TestCommon(unittest.TestCase):
    algorithms = (RoundRobin, LeastConnections, IPHash)

    def test_empty_list_and_tuple_return_none(self) -> None:
        for algorithm in self.algorithms:
            with self.subTest(algorithm=algorithm.__name__):
                balancer = algorithm()
                self.assertIsNone(balancer.select("client", []))
                self.assertIsNone(balancer.select("client", ()))

    def test_one_backend_is_always_selected(self) -> None:
        backend = make_backends(1)[0]
        for algorithm in self.algorithms:
            with self.subTest(algorithm=algorithm.__name__):
                balancer = algorithm()
                for client_ip in ("10.0.1.11", "", "garbage", "::1"):
                    self.assertIs(balancer.select(client_ip, [backend]), backend)

    def test_selection_does_not_mutate_backends_or_order(self) -> None:
        backends = make_backends(active=[4, 1, 3, 2])
        original_order = tuple(backends)
        snapshot = [
            (b.id, b.host, b.port, b.healthy, b.active)
            for b in backends
        ]
        for algorithm in self.algorithms:
            balancer = algorithm()
            for _ in range(20):
                balancer.select("10.0.1.11", backends)
        self.assertEqual(tuple(backends), original_order)
        self.assertEqual(
            [(b.id, b.host, b.port, b.healthy, b.active) for b in backends],
            snapshot,
        )

    def test_selection_accepts_tuple_sequences(self) -> None:
        backends = tuple(make_backends(active=[3, 0, 2, 1]))
        self.assertIs(LeastConnections().select("client", backends), backends[1])
        self.assertIs(RoundRobin().select("client", backends), backends[0])
        self.assertIn(IPHash().select("client", backends), backends)

    def test_names_and_select_signatures_are_consistent(self) -> None:
        expected = {
            RoundRobin: "round_robin",
            LeastConnections: "least_conn",
            IPHash: "ip_hash",
        }
        for algorithm, name in expected.items():
            with self.subTest(algorithm=algorithm.__name__):
                self.assertIsInstance(algorithm.name, str)
                self.assertEqual(algorithm.name, name)
                parameters = list(inspect.signature(algorithm.select).parameters)
                self.assertEqual(parameters, ["self", "client_ip", "backends"])

    def test_healthy_is_not_checked_or_filtered(self) -> None:
        backends = make_backends(active=[2, 0, 1, 3])
        backends[1].healthy = False
        self.assertIs(LeastConnections().select("client", backends), backends[1])


class TestFactory(unittest.TestCase):
    def test_factory_returns_expected_classes(self) -> None:
        expected = (
            ("round_robin", RoundRobin),
            ("least_conn", LeastConnections),
            ("ip_hash", IPHash),
        )
        for name, algorithm in expected:
            with self.subTest(name=name):
                balancer = make_balancer(name)
                self.assertIsInstance(balancer, algorithm)
                self.assertEqual(balancer.name, name)

    def test_factory_rejects_invalid_names(self) -> None:
        for name in ("nope", "", "Round_Robin", 123):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as context:
                    make_balancer(name)
                if name == "nope":
                    message = str(context.exception)
                    for valid_name in ("round_robin", "least_conn", "ip_hash"):
                        self.assertIn(valid_name, message)

    def test_factory_returns_independent_instances(self) -> None:
        first = make_balancer("round_robin")
        second = make_balancer("round_robin")
        self.assertIsNot(first, second)
        backends = make_backends()
        first.select("client", backends)
        self.assertIs(second.select("client", backends), backends[0])


if __name__ == "__main__":
    unittest.main()
