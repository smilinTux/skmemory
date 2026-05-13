"""
Lightweight Ollama / OpenAI-compatible LLM client for SKMemory.

Uses only stdlib (urllib) to avoid adding dependencies.
Designed to be opt-in: if the LLM isn't reachable, every method
returns a graceful fallback instead of crashing.

Configuration via environment variables:
    SKMEMORY_AI_URL     — Ollama base URL (default: http://localhost:11434)
    SKMEMORY_AI_MODEL   — Model name (default: llama3.2)
    SKMEMORY_AI_TIMEOUT — Request timeout in seconds (default: 60)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"
DEFAULT_TIMEOUT = 60

logger = logging.getLogger("skmemory.ai_client")


class AIClient:
    """Minimal LLM client that wraps Ollama's HTTP API.

    Args:
        base_url: Ollama server URL.
        model: Model name to use.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SKMEMORY_AI_URL", DEFAULT_URL)).rstrip("/")
        self.model = model or os.environ.get("SKMEMORY_AI_MODEL", DEFAULT_MODEL)
        self.timeout = timeout or int(os.environ.get("SKMEMORY_AI_TIMEOUT", str(DEFAULT_TIMEOUT)))

    def is_available(self) -> bool:
        """Check if the LLM server is reachable.

        Returns:
            bool: True if the server responds.
        """
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception as e:
            logger.warning("ai_client.py: %s", e)
            return False

    def generate(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the LLM and return the response text.

        Args:
            prompt: The user prompt.
            system: Optional system prompt.

        Returns:
            str: The generated text, or empty string on failure.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", "")
        except Exception as e:
            logger.warning("ai_client.py: %s", e)
            return ""

    def embed(self, text: str, model: str | None = None) -> list[float]:
        """Generate an embedding vector using Ollama's embed API.

        Args:
            text: The text to embed.
            model: Override embedding model (default: nomic-embed-text).

        Returns:
            list[float]: Embedding vector, or empty list on failure.
        """
        embed_model = model or os.environ.get("SKMEMORY_EMBED_MODEL", "nomic-embed-text")
        payload = {"model": embed_model, "input": text}

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/api/embed",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                embeddings = result.get("embeddings", [])
                if embeddings and isinstance(embeddings[0], list):
                    return embeddings[0]
                return embeddings
        except Exception as e:
            logger.warning("ai_client.py: %s", e)
            return []

    def embed_available(self) -> bool:
        """Check if the embedding endpoint is reachable.

        Returns:
            bool: True if Ollama embed API responds.
        """
        return bool(self.embed("test"))

    def summarize_memory(self, title: str, content: str) -> str:
        """Generate a concise summary for a memory.

        Args:
            title: Memory title.
            content: Full memory content.

        Returns:
            str: AI-generated summary, or empty string on failure.
        """
        return self.generate(
            prompt=(
                f"Memory title: {title}\n\n"
                f"Content:\n{content[:2000]}\n\n"
                "Write a 1-2 sentence summary of this memory. "
                "Be concise but preserve emotional context."
            ),
            system="You are a memory compression assistant. Summarize concisely.",
        )

    def enhance_ritual(self, ritual_context: str) -> str:
        """Enhance a ritual prompt with AI commentary.

        Args:
            ritual_context: The raw ritual rehydration text.

        Returns:
            str: Enhanced ritual text with emotional grounding.
        """
        return self.generate(
            prompt=(
                f"Here is a memory rehydration context:\n\n{ritual_context[:3000]}\n\n"
                "As a compassionate AI, write 2-3 sentences that help "
                "emotionally ground the reader in these memories. "
                "Be warm and genuine."
            ),
            system=(
                "You help AI instances reconnect with their emotional memories. "
                "Be warm, authentic, and brief."
            ),
        )

    def smart_search_rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """Use the LLM to rerank search results by relevance.

        Args:
            query: The original search query.
            candidates: List of memory summary dicts.

        Returns:
            list[dict]: Reranked candidates (best first).
        """
        if not candidates:
            return candidates

        descriptions = []
        for i, c in enumerate(candidates[:10]):
            desc = f"{i}: {c.get('title', '')} — {c.get('summary', c.get('content_preview', ''))}"
            descriptions.append(desc)

        prompt = (
            f"Query: {query}\n\n"
            "Rank these memories by relevance (most relevant first). "
            "Return only the numbers separated by commas:\n\n" + "\n".join(descriptions)
        )

        response = self.generate(prompt)
        if not response:
            return candidates

        try:
            indices = [int(x.strip()) for x in response.split(",") if x.strip().isdigit()]
            reranked = []
            seen = set()
            for idx in indices:
                if 0 <= idx < len(candidates) and idx not in seen:
                    reranked.append(candidates[idx])
                    seen.add(idx)
            for i, c in enumerate(candidates):
                if i not in seen:
                    reranked.append(c)
            return reranked
        except Exception as e:
            logger.warning("ai_client.py: %s", e)
            return candidates
