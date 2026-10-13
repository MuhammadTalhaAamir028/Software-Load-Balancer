"""Deterministic client-IP-based backend selection."""
from __future__ import annotations

import hashlib
import ipaddress
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from lb.interfaces import Backend


class IPHash:
    """Select a backend deterministically using the client's IP address."""

    name = "ip_hash"

    def __init__(self) -> None:
        """Initialize the stateless IP-hash balancer."""

    @staticmethod
    def _norm(ip: str) -> str:
        """Return the compressed IP, or the original string if invalid."""
        try:
            return ipaddress.ip_address(ip).compressed
        except ValueError:
            return ip

    @staticmethod
    def _h(ip: str) -> int:
        """Return the big-endian integer from the first eight SHA-256 bytes."""
        return int.from_bytes(
            hashlib.sha256(IPHash._norm(ip).encode()).digest()[:8],
            "big",
        )

    def select(
        self,
        client_ip: str,
        backends: Sequence[Backend],
    ) -> Optional[Backend]:
        """Return the backend selected by the normalized client-IP hash."""
        if not backends:
            return None
        return backends[self._h(client_ip) % len(backends)]
