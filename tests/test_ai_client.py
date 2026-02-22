"""Tests for the AI client module (Ollama integration).

These tests verify the client interface without requiring a running
Ollama server. The client is designed to fail gracefully.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from skmemory.ai_client import AIClient, DEFAULT_MODEL, DEFAULT_URL


class TestClientInit:
    """Client initialization and configuration."""

    def test_defaults(self):
        """Client uses sensible defaults."""
        client = AIClient()
        assert client.base_url == DEFAULT_URL
        assert client.model == DEFAULT_MODEL

    def test_custom_url(self):
        """Custom URL is respected."""
        client = AIClient(base_url="http://my-server:11434")
        assert client.base_url == "http://my-server:11434"

    def test_custom_model(self):
        """Custom model name is respected."""
        client = AIClient(model="mistral")
        assert client.model == "mistral"

    def test_env_vars(self, monkeypatch):
        """Environment variables configure the client."""
        monkeypatch.setenv("SKMEMORY_AI_URL", "http://env:1234")
        monkeypatch.setenv("SKMEMORY_AI_MODEL", "phi3")
        monkeypatch.setenv("SKMEMORY_AI_TIMEOUT", "30")

        client = AIClient()
        assert client.base_url == "http://env:1234"
        assert client.model == "phi3"
        assert client.timeout == 30

    def test_explicit_overrides_env(self, monkeypatch):
        """Explicit args take precedence over env vars."""
        monkeypatch.setenv("SKMEMORY_AI_MODEL", "phi3")
        client = AIClient(model="gemma2")
        assert client.model == "gemma2"


class TestAvailability:
    """Server availability checks."""

    def test_not_available_when_unreachable(self):
        """Returns False when server is not running."""
        client = AIClient(base_url="http://localhost:99999")
        assert client.is_available() is False


class TestGracefulFallback:
    """All methods fail gracefully when LLM is unreachable."""

    @pytest.fixture
    def offline_client(self):
        return AIClient(base_url="http://localhost:99999")

    def test_generate_returns_empty(self, offline_client):
        """Generate returns empty string when offline."""
        assert offline_client.generate("hello") == ""

    def test_summarize_returns_empty(self, offline_client):
        """Summarize returns empty string when offline."""
        assert offline_client.summarize_memory("Title", "Content") == ""

    def test_enhance_ritual_returns_empty(self, offline_client):
        """Enhance ritual returns empty string when offline."""
        assert offline_client.enhance_ritual("context text") == ""

    def test_rerank_returns_original(self, offline_client):
        """Rerank returns candidates unchanged when offline."""
        candidates = [{"title": "A"}, {"title": "B"}]
        result = offline_client.smart_search_rerank("query", candidates)
        assert result == candidates

    def test_rerank_empty_list(self, offline_client):
        """Rerank handles empty candidate list."""
        result = offline_client.smart_search_rerank("query", [])
        assert result == []
