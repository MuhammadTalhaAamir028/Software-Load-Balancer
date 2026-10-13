"""Least-connections backend selection."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from lb.interfaces import Backend


class LeastConnections:
    """Select the backend with the fewest active connections."""

    name = "least_conn"

    def __init__(self) -> None:
        """Initialize the stateless least-connections balancer."""

    def select(
        self,
        client_ip: str,
        backends: Sequence[Backend],
    ) -> Optional[Backend]:
        """Return the first backend with the lowest active count, or None."""
        if not backends:
            return None
        return min(backends, key=lambda backend: backend.active)
