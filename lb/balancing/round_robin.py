"""Round-robin backend selection."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from lb.interfaces import Backend


class RoundRobin:
    """Select healthy backends in rotating order."""

    name = "round_robin"

    def __init__(self) -> None:
        """Initialize an independent round-robin selection counter."""
        self._i = 0

    def select(
        self,
        client_ip: str,
        backends: Sequence[Backend],
    ) -> Optional[Backend]:
        """Return the next backend in rotation, or None if none are available."""
        if not backends:
            return None

        backend = backends[self._i % len(backends)]
        self._i += 1
        return backend
