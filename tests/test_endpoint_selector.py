"""Tests for SKMemory EndpointSelector — HA routing engine."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from skmemory.endpoint_selector import (
    Endpoint,
    EndpointSelector,
    RoutingConfig,
)

# ---------------------------------------------------------------------------
# Endpoint model tests
# ---------------------------------------------------------------------------


class TestEndpointModel:
    """Tests for Endpoint model serialization and defaults."""

    def test_defaults(self) -> None:
        """Endpoint has sensible defaults."""
        ep = Endpoint(url="http://localhost:6333")
        assert ep.role == "primary"
        assert ep.latency_ms == -1.0
        assert ep.healthy is True
        assert ep.fail_count == 0
        assert ep.tailscale_ip == ""

    def test_serialization_roundtrip(self) -> None:
        """Endpoint serializes and deserializes correctly."""
        ep = Endpoint(url="http://host:6333", role="replica", latency_ms=12.5)
        data = ep.model_dump()
        restored = Endpoint(**data)
        assert restored.url == ep.url
        assert restored.role == ep.role
        assert restored.latency_ms == ep.latency_ms

    def test_unhealthy_state(self) -> None:
        """Endpoint transitions to unhealthy correctly."""
        ep = Endpoint(url="http://host:6333", healthy=False, fail_count=5)
        assert ep.healthy is False
        assert ep.fail_count == 5

    def test_last_checked_timestamp(self) -> None:
        """last_checked stores ISO timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        ep = Endpoint(url="http://host:6333", last_checked=now)
        assert ep.last_checked == now


class TestRoutingConfigModel:
    """Tests for RoutingConfig defaults."""

    def test_defaults(self) -> None:
        """RoutingConfig has sensible defaults."""
        rc = RoutingConfig()
        assert rc.strategy == "failover"
        assert rc.probe_interval_seconds == 30
        assert rc.probe_timeout_seconds == 3
        assert rc.max_fail_count == 3
        assert rc.recovery_interval_seconds == 60

    def test_custom_values(self) -> None:
        """RoutingConfig accepts custom values."""
        rc = RoutingConfig(strategy="latency", probe_interval_seconds=10)
        assert rc.strategy == "latency"
        assert rc.probe_interval_seconds == 10


# ---------------------------------------------------------------------------
# Latency probing tests
# ---------------------------------------------------------------------------


class TestLatencyProbing:
    """Tests for TCP latency probing (all networking mocked)."""

    def test_probe_success(self) -> None:
        """Successful probe updates latency and marks healthy."""
        ep = Endpoint(url="http://localhost:6333")
        selector = EndpointSelector(skvector_endpoints=[ep])

        mock_sock = MagicMock()
        with patch("skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock):
            result = selector.probe_endpoint(ep)

        assert result.healthy is True
        assert result.latency_ms >= 0
        assert result.fail_count == 0
        assert result.last_checked != ""
        mock_sock.close.assert_called_once()

    def test_probe_timeout(self) -> None:
        """Timeout increments fail_count but keeps healthy until max."""
        ep = Endpoint(url="http://localhost:6333")
        selector = EndpointSelector(skvector_endpoints=[ep])

        with patch(
            "skmemory.endpoint_selector.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            result = selector.probe_endpoint(ep)

        assert result.fail_count == 1
        assert result.healthy is True  # Still healthy (max_fail_count=3)
        assert result.latency_ms == -1.0

    def test_probe_connection_refused(self) -> None:
        """Connection refused increments fail_count."""
        ep = Endpoint(url="http://localhost:6333")
        selector = EndpointSelector(skvector_endpoints=[ep])

        with patch(
            "skmemory.endpoint_selector.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            result = selector.probe_endpoint(ep)

        assert result.fail_count == 1
        assert result.latency_ms == -1.0

    def test_probe_unhealthy_after_max_failures(self) -> None:
        """Endpoint marked unhealthy after max_fail_count failures."""
        ep = Endpoint(url="http://localhost:6333", fail_count=2)
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(max_fail_count=3),
        )

        with patch(
            "skmemory.endpoint_selector.socket.create_connection",
            side_effect=OSError("unreachable"),
        ):
            result = selector.probe_endpoint(ep)

        assert result.fail_count == 3
        assert result.healthy is False

    def test_probe_recovery(self) -> None:
        """Unhealthy endpoint recovers when probe succeeds."""
        ep = Endpoint(url="http://localhost:6333", healthy=False, fail_count=5)
        selector = EndpointSelector(skvector_endpoints=[ep])

        mock_sock = MagicMock()
        with patch("skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock):
            result = selector.probe_endpoint(ep)

        assert result.healthy is True
        assert result.fail_count == 0
        assert result.latency_ms >= 0

    def test_probe_all_updates_timestamp(self) -> None:
        """probe_all updates the last probe time."""
        ep1 = Endpoint(url="http://host1:6333")
        ep2 = Endpoint(url="redis://host2:6379")
        selector = EndpointSelector(skvector_endpoints=[ep1], skgraph_endpoints=[ep2])

        mock_sock = MagicMock()
        with patch("skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock):
            results = selector.probe_all()

        assert len(results["skvector"]) == 1
        assert len(results["skgraph"]) == 1
        assert selector._last_probe_time > 0

    def test_probe_infers_redis_port(self) -> None:
        """Probe infers port 6379 for redis:// scheme."""
        ep = Endpoint(url="redis://myhost")
        selector = EndpointSelector(skgraph_endpoints=[ep])

        mock_sock = MagicMock()
        with patch(
            "skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock
        ) as mock_connect:
            selector.probe_endpoint(ep)

        args = mock_connect.call_args[0]
        assert args[0] == ("myhost", 6379)

    def test_probe_infers_https_port(self) -> None:
        """Probe infers port 443 for https:// scheme."""
        ep = Endpoint(url="https://secure.qdrant.io")
        selector = EndpointSelector(skvector_endpoints=[ep])

        mock_sock = MagicMock()
        with patch(
            "skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock
        ) as mock_connect:
            selector.probe_endpoint(ep)

        args = mock_connect.call_args[0]
        assert args[0] == ("secure.qdrant.io", 443)


# ---------------------------------------------------------------------------
# Routing strategy tests
# ---------------------------------------------------------------------------


class TestFailoverStrategy:
    """Tests for the failover routing strategy."""

    def test_picks_first_healthy(self) -> None:
        """Failover returns the first healthy endpoint."""
        ep1 = Endpoint(url="http://a:6333", latency_ms=50)
        ep2 = Endpoint(url="http://b:6333", latency_ms=10)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="failover", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://a:6333"

    def test_skips_unhealthy(self) -> None:
        """Failover skips unhealthy endpoints."""
        ep1 = Endpoint(url="http://a:6333", healthy=False)
        ep2 = Endpoint(url="http://b:6333", healthy=True)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="failover", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://b:6333"

    def test_returns_none_when_all_unhealthy(self) -> None:
        """Failover returns None when all endpoints are unhealthy."""
        ep1 = Endpoint(url="http://a:6333", healthy=False)
        ep2 = Endpoint(url="http://b:6333", healthy=False)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="failover", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        assert selector.select_skvector() is None


class TestLatencyStrategy:
    """Tests for the latency routing strategy."""

    def test_picks_lowest_latency(self) -> None:
        """Latency strategy picks the endpoint with lowest latency."""
        ep1 = Endpoint(url="http://a:6333", latency_ms=50)
        ep2 = Endpoint(url="http://b:6333", latency_ms=10)
        ep3 = Endpoint(url="http://c:6333", latency_ms=30)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2, ep3],
            config=RoutingConfig(strategy="latency", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://b:6333"

    def test_falls_back_to_first_when_unprobed(self) -> None:
        """Latency falls back to first healthy if none probed."""
        ep1 = Endpoint(url="http://a:6333")  # latency_ms=-1
        ep2 = Endpoint(url="http://b:6333")
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="latency", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://a:6333"

    def test_skips_unhealthy(self) -> None:
        """Latency skips unhealthy endpoints even with low latency."""
        ep1 = Endpoint(url="http://a:6333", latency_ms=5, healthy=False)
        ep2 = Endpoint(url="http://b:6333", latency_ms=50)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="latency", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://b:6333"


class TestLocalFirstStrategy:
    """Tests for the local-first routing strategy."""

    def test_prefers_localhost(self) -> None:
        """Local-first picks localhost over lower-latency remote."""
        ep1 = Endpoint(url="http://remote:6333", latency_ms=5)
        ep2 = Endpoint(url="http://localhost:6333", latency_ms=15)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="local-first", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://localhost:6333"

    def test_prefers_127_0_0_1(self) -> None:
        """Local-first recognizes 127.0.0.1 as local."""
        ep1 = Endpoint(url="http://remote:6333", latency_ms=5)
        ep2 = Endpoint(url="http://127.0.0.1:6333", latency_ms=15)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="local-first", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://127.0.0.1:6333"

    def test_falls_back_to_latency_when_no_local(self) -> None:
        """Local-first falls back to lowest latency when no local endpoint."""
        ep1 = Endpoint(url="http://remote-a:6333", latency_ms=50)
        ep2 = Endpoint(url="http://remote-b:6333", latency_ms=10)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="local-first", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://remote-b:6333"

    def test_skips_unhealthy_local(self) -> None:
        """Local-first skips unhealthy localhost."""
        ep1 = Endpoint(url="http://localhost:6333", healthy=False)
        ep2 = Endpoint(url="http://remote:6333", latency_ms=10)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="local-first", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://remote:6333"


class TestReadLocalWritePrimaryStrategy:
    """Tests for the read-local-write-primary strategy."""

    def test_writes_go_to_primary_only(self) -> None:
        """Writes are routed only to primary endpoints."""
        ep1 = Endpoint(url="http://localhost:6333", role="replica", latency_ms=2)
        ep2 = Endpoint(url="http://primary:6333", role="primary", latency_ms=20)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="read-local-write-primary", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector(for_write=True)
        assert result is not None
        assert result.url == "http://primary:6333"

    def test_reads_prefer_local(self) -> None:
        """Reads prefer localhost in read-local-write-primary."""
        ep1 = Endpoint(url="http://primary:6333", role="primary", latency_ms=20)
        ep2 = Endpoint(url="http://localhost:6333", role="replica", latency_ms=2)
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="read-local-write-primary", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector(for_write=False)
        assert result is not None
        assert result.url == "http://localhost:6333"

    def test_writes_return_none_when_no_primary(self) -> None:
        """Writes return None if no primary is healthy."""
        ep1 = Endpoint(url="http://localhost:6333", role="replica")
        ep2 = Endpoint(url="http://remote:6333", role="replica")
        selector = EndpointSelector(
            skvector_endpoints=[ep1, ep2],
            config=RoutingConfig(strategy="read-local-write-primary", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        assert selector.select_skvector(for_write=True) is None


# ---------------------------------------------------------------------------
# Failover behavior tests
# ---------------------------------------------------------------------------


class TestFailover:
    """Tests for failover transitions and recovery."""

    def test_healthy_to_unhealthy_transition(self) -> None:
        """Endpoint transitions from healthy to unhealthy after max failures."""
        ep = Endpoint(url="http://host:6333")
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(max_fail_count=2),
        )

        with patch(
            "skmemory.endpoint_selector.socket.create_connection",
            side_effect=OSError("down"),
        ):
            selector.probe_endpoint(ep)
            assert ep.healthy is True  # 1 failure, not yet max

            selector.probe_endpoint(ep)
            assert ep.healthy is False  # 2 failures = max

    def test_mark_unhealthy_by_url(self) -> None:
        """mark_unhealthy marks the correct endpoint."""
        ep1 = Endpoint(url="http://a:6333")
        ep2 = Endpoint(url="http://b:6333")
        selector = EndpointSelector(skvector_endpoints=[ep1, ep2])

        selector.mark_unhealthy("http://a:6333")
        assert ep1.healthy is False
        assert ep2.healthy is True

    def test_mark_unhealthy_nonexistent_url(self) -> None:
        """mark_unhealthy does nothing for unknown URLs."""
        ep = Endpoint(url="http://a:6333")
        selector = EndpointSelector(skvector_endpoints=[ep])

        selector.mark_unhealthy("http://unknown:6333")
        assert ep.healthy is True

    def test_recovery_after_failure(self) -> None:
        """Endpoint recovers when probing succeeds again."""
        ep = Endpoint(url="http://host:6333", healthy=False, fail_count=5)
        selector = EndpointSelector(skvector_endpoints=[ep])

        mock_sock = MagicMock()
        with patch("skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock):
            selector.probe_endpoint(ep)

        assert ep.healthy is True
        assert ep.fail_count == 0


# ---------------------------------------------------------------------------
# Heartbeat discovery tests
# ---------------------------------------------------------------------------


class TestHeartbeatDiscovery:
    """Tests for discovering endpoints from heartbeat files."""

    def test_discover_qdrant_from_heartbeat(self, tmp_path: Path) -> None:
        """Discovers Qdrant endpoint from heartbeat file."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "tailscale_ip": "100.64.0.5",
                    "services": [
                        {"name": "skvector", "port": 6333, "protocol": "http"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 1
        assert selector.skvector_endpoints[0].url == "http://100.64.0.5:6333"
        assert selector.skvector_endpoints[0].role == "replica"

    def test_discover_falkordb_from_heartbeat(self, tmp_path: Path) -> None:
        """Discovers FalkorDB endpoint from heartbeat file."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "services": [
                        {"name": "skgraph", "port": 6379, "protocol": "redis"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skgraph_endpoints) == 1
        assert selector.skgraph_endpoints[0].url == "redis://peer-host:6379"

    def test_discover_uses_tailscale_ip_over_hostname(self, tmp_path: Path) -> None:
        """Tailscale IP is preferred over hostname for URL construction."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "tailscale_ip": "100.64.0.10",
                    "services": [
                        {"name": "skvector", "port": 6333, "protocol": "http"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert selector.skvector_endpoints[0].url == "http://100.64.0.10:6333"

    def test_discover_skips_existing_urls(self, tmp_path: Path) -> None:
        """Discovery doesn't duplicate existing config endpoints."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "tailscale_ip": "100.64.0.5",
                    "services": [
                        {"name": "skvector", "port": 6333, "protocol": "http"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        existing = Endpoint(url="http://100.64.0.5:6333")
        selector = EndpointSelector(skvector_endpoints=[existing])
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 1

    def test_discover_empty_dir(self, tmp_path: Path) -> None:
        """Empty heartbeat dir adds no endpoints."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 0
        assert len(selector.skgraph_endpoints) == 0

    def test_discover_missing_dir(self, tmp_path: Path) -> None:
        """Missing heartbeat dir doesn't raise."""
        selector = EndpointSelector()
        selector.discover_from_heartbeats(tmp_path / "nonexistent")

        assert len(selector.skvector_endpoints) == 0

    def test_discover_skips_invalid_json(self, tmp_path: Path) -> None:
        """Invalid JSON in heartbeat files is skipped gracefully."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "bad.json").write_text("not json", encoding="utf-8")

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 0

    def test_discover_skips_no_host(self, tmp_path: Path) -> None:
        """Heartbeat without hostname or tailscale_ip is skipped."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "services": [
                        {"name": "skvector", "port": 6333},
                    ],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 0

    def test_discover_multiple_services(self, tmp_path: Path) -> None:
        """Discovers both Qdrant and FalkorDB from one heartbeat."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "services": [
                        {"name": "skvector", "port": 6333, "protocol": "http"},
                        {"name": "skgraph", "port": 6379, "protocol": "redis"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 1
        assert len(selector.skgraph_endpoints) == 1

    def test_discover_skips_tmp_files(self, tmp_path: Path) -> None:
        """Files ending in .tmp are skipped."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        (hb_dir / "peer.json.tmp").write_text(
            json.dumps(
                {
                    "agent_name": "peer",
                    "hostname": "peer-host",
                    "services": [{"name": "skvector", "port": 6333}],
                }
            ),
            encoding="utf-8",
        )

        selector = EndpointSelector()
        selector.discover_from_heartbeats(hb_dir)

        assert len(selector.skvector_endpoints) == 0


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests that single-URL configs still work."""

    def test_empty_endpoints(self) -> None:
        """Empty endpoint lists return None for selection."""
        selector = EndpointSelector(
            config=RoutingConfig(probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        assert selector.select_skvector() is None
        assert selector.select_skgraph() is None

    def test_single_endpoint(self) -> None:
        """Single endpoint is selected regardless of strategy."""
        ep = Endpoint(url="http://localhost:6333")
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(strategy="latency", probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skvector()
        assert result is not None
        assert result.url == "http://localhost:6333"

    def test_normalize_from_dict(self) -> None:
        """Endpoints can be passed as dicts."""
        selector = EndpointSelector(
            skvector_endpoints=[{"url": "http://host:6333", "role": "replica"}],
        )
        assert len(selector.skvector_endpoints) == 1
        assert selector.skvector_endpoints[0].role == "replica"

    def test_normalize_from_endpoint_config(self) -> None:
        """Endpoints can be passed as EndpointConfig-like objects."""
        from skmemory.config import EndpointConfig

        ec = EndpointConfig(url="http://host:6333", role="replica")
        selector = EndpointSelector(skvector_endpoints=[ec])

        assert len(selector.skvector_endpoints) == 1
        assert selector.skvector_endpoints[0].url == "http://host:6333"

    def test_falkordb_selection(self) -> None:
        """FalkorDB endpoints work the same as Qdrant."""
        ep = Endpoint(url="redis://localhost:6379")
        selector = EndpointSelector(
            skgraph_endpoints=[ep],
            config=RoutingConfig(probe_interval_seconds=9999),
        )
        selector._last_probe_time = time.monotonic()

        result = selector.select_skgraph()
        assert result is not None
        assert result.url == "redis://localhost:6379"


# ---------------------------------------------------------------------------
# Status reporting tests
# ---------------------------------------------------------------------------


class TestEndpointSelectorStatus:
    """Tests for status output format."""

    def test_status_format(self) -> None:
        """Status returns expected keys and format."""
        ep = Endpoint(url="http://host:6333", latency_ms=12.5)
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(strategy="latency"),
        )

        info = selector.status()
        assert info["strategy"] == "latency"
        assert "probe_interval_seconds" in info
        assert "last_probe_age_seconds" in info
        assert "skvector_endpoints" in info
        assert "skgraph_endpoints" in info
        assert len(info["skvector_endpoints"]) == 1
        assert info["skvector_endpoints"][0]["url"] == "http://host:6333"

    def test_status_empty(self) -> None:
        """Status works with no endpoints."""
        selector = EndpointSelector()
        info = selector.status()
        assert info["skvector_endpoints"] == []
        assert info["skgraph_endpoints"] == []

    def test_status_probe_age(self) -> None:
        """Status reflects probe age correctly."""
        selector = EndpointSelector()
        info = selector.status()
        assert info["last_probe_age_seconds"] == -1  # Never probed


# ---------------------------------------------------------------------------
# Maybe-probe (lazy probing) tests
# ---------------------------------------------------------------------------


class TestMaybeProbe:
    """Tests for lazy on-demand probing."""

    def test_probes_on_first_select(self) -> None:
        """First select triggers a probe."""
        ep = Endpoint(url="http://host:6333")
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(probe_interval_seconds=30),
        )

        mock_sock = MagicMock()
        with patch(
            "skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock
        ) as mock_conn:
            selector.select_skvector()

        mock_conn.assert_called_once()

    def test_does_not_reprobe_if_fresh(self) -> None:
        """Second select within interval does not re-probe."""
        ep = Endpoint(url="http://host:6333")
        selector = EndpointSelector(
            skvector_endpoints=[ep],
            config=RoutingConfig(probe_interval_seconds=30),
        )

        mock_sock = MagicMock()
        with patch(
            "skmemory.endpoint_selector.socket.create_connection", return_value=mock_sock
        ) as mock_conn:
            selector.select_skvector()
            selector.select_skvector()

        # Should only probe once (first call)
        assert mock_conn.call_count == 1


# ---------------------------------------------------------------------------
# Config integration tests
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    """Tests for build_endpoint_list backward compat bridge."""

    def test_single_url_becomes_endpoint(self) -> None:
        """Single URL is promoted to endpoint list."""
        from skmemory.config import build_endpoint_list

        result = build_endpoint_list("http://localhost:6333", [])
        assert len(result) == 1
        assert result[0].url == "http://localhost:6333"
        assert result[0].role == "primary"

    def test_endpoints_list_takes_precedence(self) -> None:
        """Endpoint list is used when present."""
        from skmemory.config import EndpointConfig, build_endpoint_list

        eps = [EndpointConfig(url="http://a:6333"), EndpointConfig(url="http://b:6333")]
        result = build_endpoint_list("http://c:6333", eps)
        assert len(result) == 3  # c prepended since not in list
        assert result[0].url == "http://c:6333"

    def test_no_duplicate_when_url_in_list(self) -> None:
        """Single URL not duplicated if already in endpoints list."""
        from skmemory.config import EndpointConfig, build_endpoint_list

        eps = [EndpointConfig(url="http://a:6333")]
        result = build_endpoint_list("http://a:6333", eps)
        assert len(result) == 1

    def test_empty_returns_empty(self) -> None:
        """No URL and no endpoints returns empty list."""
        from skmemory.config import build_endpoint_list

        result = build_endpoint_list(None, [])
        assert result == []
