"""Deterministic balancing evidence for the Project 5 report."""
from __future__ import annotations

from collections import Counter
import random
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lb.balancing import make_balancer
from lb.interfaces import Backend


def make_backends(count: int = 4) -> List[Backend]:
    """Return fresh backends in project configuration order."""
    return [
        Backend(f"b{i}", f"10.0.3.{10 + i}", 8000)
        for i in range(1, count + 1)
    ]


def print_counts(counts: Counter, backends: Sequence[Backend]) -> None:
    """Print counts and percentages for the given backend order."""
    print("backend  count  percent")
    print("-------  -----  -------")
    total = sum(counts.values())
    for backend in backends:
        count = counts[backend.id]
        print(f"{backend.id:<7}  {count:>5}  {count / total * 100:>6.2f}%")


def round_robin_section() -> None:
    """Print round-robin rotation and removal evidence."""
    print("1. ROUND ROBIN")
    backends = make_backends()
    balancer = make_balancer("round_robin")
    first = [balancer.select("client", backends).id for _ in range(8)]
    print("first 8 selections: " + " ".join(first))

    counts = Counter(balancer.select("client", backends).id for _ in range(10000))
    print("\n10,000 selections across 4 healthy backends:")
    print_counts(counts, backends)

    reduced = [backends[0], backends[1], backends[3]]
    reduced_counts = Counter(
        balancer.select("client", reduced).id for _ in range(3000)
    )
    print("\n3,000 further selections after b3 removal:")
    print_counts(reduced_counts, reduced)


def least_connections_section() -> None:
    """Print least-connections examples and a held-connection simulation."""
    print("2. LEAST CONNECTIONS")
    cases = (
        ([3, 1, 1, 2], "b2", "tie b2/b3; list order"),
        ([0, 0, 5, 9], "b1", "tie b1/b2; list order"),
        ([5, 5, 5, 5], "b1", "all tied; first wins"),
        ([9, 8, 7, 6], "b4", "lowest active count"),
    )
    print("active vector       chosen  note")
    print("------------------  ------  -------------------------")
    for active, expected, note in cases:
        backends = make_backends()
        for backend, count in zip(backends, active):
            backend.active = count
        chosen = make_balancer("least_conn").select("client", backends)
        print(f"{str(active):<18}  {chosen.id:<6}  {note}")
        if chosen.id != expected:
            raise AssertionError(f"least-conn expected {expected}, got {chosen.id}")

    backends = make_backends()
    balancer = make_balancer("least_conn")
    print("\nheld-connection simulation:")
    for round_number in range(1, 9):
        chosen = balancer.select("client", backends)
        chosen.active += 1
        active = [backend.active for backend in backends]
        print(f"round {round_number}: selected {chosen.id}; active {active}")
    backends[1].active -= 1
    chosen = balancer.select("client", backends)
    print(f"after b2 finishes (active 2 -> 1): selected {chosen.id}")


def random_ipv4_addresses(count: int, seed: int) -> List[str]:
    """Generate deterministic dotted-quad addresses."""
    rng = random.Random(seed)
    return [
        ".".join(str(rng.randrange(256)) for _ in range(4))
        for _ in range(count)
    ]


def ip_hash_mapping(
    clients: Iterable[str],
    backends: Sequence[Backend],
) -> dict:
    """Return the IP-hash mapping for clients and backends."""
    balancer = make_balancer("ip_hash")
    return {client: balancer.select(client, backends).id for client in clients}


def ip_hash_section() -> bool:
    """Print fixed-client mappings, spread, and reshuffle evidence."""
    print("3. IP-HASH")
    clients = [f"10.0.1.{i}" for i in range(11, 15)]
    full = make_backends()
    reduced = [full[0], full[1], full[3]]
    full_mapping = ip_hash_mapping(clients, full)
    reduced_mapping = ip_hash_mapping(clients, reduced)
    expected = {
        "10.0.1.11": ("b3", "b1"),
        "10.0.1.12": ("b2", "b1"),
        "10.0.1.13": ("b2", "b2"),
        "10.0.1.14": ("b2", "b2"),
    }

    print("Client        4 healthy  b3 down  Changed?")
    print("------------  ---------  -------  --------")
    for client in clients:
        changed = "YES" if full_mapping[client] != reduced_mapping[client] else "NO"
        print(
            f"{client:<12}  {full_mapping[client]:<9}  "
            f"{reduced_mapping[client]:<7}  {changed}"
        )
    matches = all(
        (full_mapping[client], reduced_mapping[client]) == expected[client]
        for client in clients
    )
    print("table matches brief: " + ("YES" if matches else "NO"))

    client_counts = Counter(full_mapping.values())
    print(
        "four-client counts: "
        + ", ".join(f"{backend.id}={client_counts[backend.id]}" for backend in full)
    )
    print("Known behaviour, reported as a finding (hash not tuned)")

    addresses = random_ipv4_addresses(10000, 2026)
    spread_full = Counter(ip_hash_mapping(addresses, full).values())
    spread_reduced = Counter(ip_hash_mapping(addresses, reduced).values())
    print("\n10,000 random IPv4 addresses (seed 2026), 4 healthy:")
    print_counts(spread_full, full)
    print("\n10,000 random IPv4 addresses (seed 2026), b3 down:")
    print_counts(spread_reduced, reduced)

    full_random = ip_hash_mapping(addresses, full)
    reduced_random = ip_hash_mapping(addresses, reduced)
    changed_count = sum(
        full_random[address] != reduced_random[address] for address in addresses
    )
    print(
        "\nreshuffled mappings: "
        f"{changed_count} ({changed_count / len(addresses) * 100:.2f}%)"
    )
    print(
        "Consistent hashing would avoid this reshuffle; it is not built and is out of scope."
    )

    return matches


def main() -> int:
    """Print all deterministic balancing evidence."""
    round_robin_section()
    print()
    least_connections_section()
    print()
    matches = ip_hash_section()
    print("\ndemo complete")
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
