"""
SKMemory Endpoint Selector — HA routing for SKVector and SKGraph backends.

Discovers multiple backend endpoints (via config or heartbeat mesh),
probes their latency, selects the fastest healthy one, and fails over
automatically.  No background threads — probing is on-demand with a
TTL cache.

Design principles:
    - Selector picks a URL, backends stay unchanged
    - On-demand probing with TTL cache (no background threads)
    - Config endpoints take precedence over heartbeat discovery
    - Graceful degradation everywhere
    - Backward compatible: single-URL configs work unchanged
"""

from __future__ import annotations

import json
import logging
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger("skmemory.endpoint_selector")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Endpoint(BaseModel):
    """A single backend endpoint with health and latency tracking."""

    url: str
    role: str = "primary"           # primary | replica
    tailscale_ip: str = ""          # optional, for display
    latency_ms: float = -1.0        # -1 = not yet probed
    healthy: bool = True
    last_checked: str = ""          # ISO timestamp
    fail_count: int = 0


class RoutingConfig(BaseModel):
    """Configuration for endpoint routing behavior."""

    strategy: str = "failover"      # failover | latency | local-first | read-local-write-primary
    probe_interval_seconds: int = 30
    probe_timeout_seconds: int = 3
    max_fail_count: int = 3         # mark unhealthy after N consecutive failures
    recovery_interval_seconds: int = 60  # re-check unhealthy endpoints


# ---------------------------------------------------------------------------
# EndpointSelector
# ---------------------------------------------------------------------------


class EndpointSelector:
    """Routes requests to the best available backend endpoint.

    Sits between config resolution and backend construction — picks the
    best URL, then the caller creates backends normally with that URL.

    Args:
        skvector_endpoints: List of SKVector endpoint dicts or Endpoint objects.
        skgraph_endpoints: List of SKGraph endpoint dicts or Endpoint objects.
        config: Routing configuration.
    """

    def __init__(
        self,
        skvector_endpoints: Optional[list[dict | Endpoint]] = None,
        skgraph_endpoints: Optional[list[dict | Endpoint]] = None,
        config: Optional[RoutingConfig] = None,
    ) -> None:
        self._config = config or RoutingConfig()
        self._skvector: list[Endpoint] = self._normalize(skvector_endpoints or [])
        self._skgraph: list[Endpoint] = self._normalize(skgraph_endpoints or [])
        self._last_probe_time: float = 0.0

    @staticmethod
    def _normalize(endpoints: list[dict | Endpoint]) -> list[Endpoint]:
        """Convert dicts/Endpoints into a uniform list of Endpoint objects."""
        result: list[Endpoint] = []
        for ep in endpoints:
            if isinstance(ep, Endpoint):
                result.append(ep)
            elif isinstance(ep, dict):
                result.append(Endpoint(**ep))
            else:
                # Try pydantic model with .url attribute (EndpointConfig)
                try:
                    result.append(Endpoint(
                        url=ep.url,
                        role=getattr(ep, "role", "primary"),
                        tailscale_ip=getattr(ep, "tailscale_ip", ""),
                    ))
                except AttributeError:
                    logger.warning("Cannot normalize endpoint: %s", ep)
        return result

    # -------------------------------------------------------------------
    # Core selection
    # -------------------------------------------------------------------

    def select_skvector(self, for_write: bool = False) -> Optional[Endpoint]:
        """Select the best SKVector endpoint.

        Args:
            for_write: If True and strategy is read-local-write-primary,
                       returns only primary endpoints.

        Returns:
            Best Endpoint or None if all unhealthy.
        """
        self._maybe_probe()
        return self._select(self._skvector, for_write)

    def select_skgraph(self, for_write: bool = False) -> Optional[Endpoint]:
        """Select the best SKGraph endpoint.

        Args:
            for_write: If True and strategy is read-local-write-primary,
                       returns only primary endpoints.

        Returns:
            Best Endpoint or None if all unhealthy.
        """
        self._maybe_probe()
        return self._select(self._skgraph, for_write)

    def _select(self, endpoints: list[Endpoint], for_write: bool) -> Optional[Endpoint]:
        """Apply the routing strategy to pick the best endpoint."""
        if not endpoints:
            return None

        strategy = self._config.strategy

        if strategy == "read-local-write-primary" and for_write:
            candidates = [ep for ep in endpoints if ep.healthy and ep.role == "primary"]
        else:
            candidates = [ep for ep in endpoints if ep.healthy]

        if not candidates:
            return None

        if strategy == "failover":
            return candidates[0]

        if strategy == "latency":
            probed = [ep for ep in candidates if ep.latency_ms >= 0]
            if probed:
                return min(probed, key=lambda e: e.latency_ms)
            return candidates[0]

        if strategy == "local-first":
            for ep in candidates:
                parsed = urlparse(ep.url)
                host = parsed.hostname or ""
                if host in ("localhost", "127.0.0.1", "::1"):
                    return ep
            # Fall back to lowest latency
            probed = [ep for ep in candidates if ep.latency_ms >= 0]
            if probed:
                return min(probed, key=lambda e: e.latency_ms)
            return candidates[0]

        if strategy == "read-local-write-primary":
            if for_write:
                # Already filtered to primary above
                return candidates[0] if candidates else None
            # Reads: prefer local, then lowest latency
            for ep in candidates:
                parsed = urlparse(ep.url)
                host = parsed.hostname or ""
                if host in ("localhost", "127.0.0.1", "::1"):
                    return ep
            probed = [ep for ep in candidates if ep.latency_ms >= 0]
            if probed:
                return min(probed, key=lambda e: e.latency_ms)
            return candidates[0]

        # Unknown strategy, fall back to first healthy
        return candidates[0]

    # -------------------------------------------------------------------
    # Health probing
    # -------------------------------------------------------------------

    def _maybe_probe(self) -> None:
        """Probe if results are stale (older than probe_interval_seconds)."""
        now = time.monotonic()
        if now - self._last_probe_time >= self._config.probe_interval_seconds:
            self.probe_all()

    def probe_all(self) -> dict:
        """Probe all endpoints and return results summary.

        Returns:
            Dict with skvector and skgraph probe results.
        """
        results = {
            "skvector": [self.probe_endpoint(ep) for ep in self._skvector],
            "skgraph": [self.probe_endpoint(ep) for ep in self._skgraph],
        }
        self._last_probe_time = time.monotonic()
        return results

    def probe_endpoint(self, endpoint: Endpoint) -> Endpoint:
        """Probe a single endpoint's TCP connectivity and measure latency.

        Updates the endpoint in-place and returns it.

        Args:
            endpoint: The endpoint to probe.

        Returns:
            The same Endpoint, updated with latency/health status.
        """
        parsed = urlparse(endpoint.url)
        host = parsed.hostname or "localhost"
        port = parsed.port

        if port is None:
            # Infer default ports from scheme
            if parsed.scheme in ("redis", "rediss"):
                port = 6379
            elif parsed.scheme == "https":
                port = 443
            else:
                port = 80

        try:
            start = time.monotonic()
            sock = socket.create_connection(
                (host, port),
                timeout=self._config.probe_timeout_seconds,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            sock.close()

            endpoint.latency_ms = round(elapsed_ms, 2)
            endpoint.fail_count = 0
            endpoint.healthy = True
        except (OSError, socket.timeout):
            endpoint.fail_count += 1
            endpoint.latency_ms = -1.0
            if endpoint.fail_count >= self._config.max_fail_count:
                endpoint.healthy = False

        endpoint.last_checked = datetime.now(timezone.utc).isoformat()
        return endpoint

    def mark_unhealthy(self, url: str) -> None:
        """Mark an endpoint as unhealthy by URL.

        Called externally when a backend operation fails, so the next
        selection picks a different endpoint.

        Args:
            url: The URL of the endpoint to mark.
        """
        for ep in self._skvector + self._skgraph:
            if ep.url == url:
                ep.fail_count = self._config.max_fail_count
                ep.healthy = False
                ep.last_checked = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------
    # Heartbeat mesh discovery
    # -------------------------------------------------------------------

    def discover_from_heartbeats(self, heartbeat_dir: Optional[Path] = None) -> None:
        """Discover backend endpoints from heartbeat mesh files.

        Reads heartbeat JSON files and looks for a ``services`` field
        containing advertised backend services.  Discovered endpoints are
        merged with existing ones (config takes precedence).

        Args:
            heartbeat_dir: Path to heartbeat directory.
                           Defaults to ``~/.skcapstone/heartbeats/``.
        """
        if heartbeat_dir is None:
            heartbeat_dir = Path.home() / ".skcapstone" / "heartbeats"

        if not heartbeat_dir.is_dir():
            logger.debug("Heartbeat directory not found: %s", heartbeat_dir)
            return

        existing_skvector_urls = {ep.url for ep in self._skvector}
        existing_skgraph_urls = {ep.url for ep in self._skgraph}

        for f in sorted(heartbeat_dir.glob("*.json")):
            if f.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("Cannot read heartbeat %s: %s", f.name, exc)
                continue

            services = data.get("services", [])
            if not services:
                continue

            hostname = data.get("hostname", "")
            tailscale_ip = data.get("tailscale_ip", "")
            # Prefer tailscale_ip, fall back to hostname
            host = tailscale_ip or hostname
            if not host:
                continue

            for svc in services:
                name = svc.get("name", "")
                port = svc.get("port", 0)
                protocol = svc.get("protocol", "http")

                if not name or not port:
                    continue

                url = f"{protocol}://{host}:{port}"

                if name == "skvector" and url not in existing_skvector_urls:
                    self._skvector.append(Endpoint(
                        url=url,
                        role="replica",
                        tailscale_ip=tailscale_ip,
                    ))
                    existing_skvector_urls.add(url)
                    logger.info("Discovered SKVector endpoint: %s", url)

                elif name == "skgraph" and url not in existing_skgraph_urls:
                    self._skgraph.append(Endpoint(
                        url=url,
                        role="replica",
                        tailscale_ip=tailscale_ip,
                    ))
                    existing_skgraph_urls.add(url)
                    logger.info("Discovered SKGraph endpoint: %s", url)

    # -------------------------------------------------------------------
    # Status reporting
    # -------------------------------------------------------------------

    def status(self) -> dict:
        """Return a status report of all endpoints.

        Returns:
            Dict with strategy, endpoint lists, and probe staleness.
        """
        now = time.monotonic()
        stale_seconds = now - self._last_probe_time if self._last_probe_time > 0 else -1

        return {
            "strategy": self._config.strategy,
            "probe_interval_seconds": self._config.probe_interval_seconds,
            "last_probe_age_seconds": round(stale_seconds, 1),
            "skvector_endpoints": [ep.model_dump() for ep in self._skvector],
            "skgraph_endpoints": [ep.model_dump() for ep in self._skgraph],
        }

    @property
    def skvector_endpoints(self) -> list[Endpoint]:
        """Access the SKVector endpoint list."""
        return self._skvector

    @property
    def skgraph_endpoints(self) -> list[Endpoint]:
        """Access the SKGraph endpoint list."""
        return self._skgraph

    @property
    def config(self) -> RoutingConfig:
        """Access the routing configuration."""
        return self._config
