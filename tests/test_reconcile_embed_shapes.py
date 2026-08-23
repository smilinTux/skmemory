"""Embedding-response shape handling for the pgvector reconcile engine.

Regression cover for the 2026-08 silent-stall: ``embed_url`` was moved to the
OpenAI-compatible ``/v1/embeddings`` endpoint on the .100 Arc iGPU, but
``reconcile.embed()`` only understood Ollama's native ``{"embeddings": ...}``
shape, so every batch "failed", halved, truncated, and finally raised
``embed failed`` with 983 memories left unvectorised.
"""

from skmemory.reconcile import _embeddings_from_response


def test_ollama_native_shape():
    """Ollama /api/embed returns a top-level embeddings list, in order."""
    payload = {"model": "mxbai-embed-large", "embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    assert _embeddings_from_response(payload) == [[0.1, 0.2], [0.3, 0.4]]


def test_openai_shape():
    """OpenAI-compatible /v1/embeddings wraps each vector in a data row."""
    payload = {
        "model": "mxbai-embed-large",
        "object": "list",
        "data": [
            {"embedding": [0.1, 0.2], "index": 0},
            {"embedding": [0.3, 0.4], "index": 1},
        ],
    }
    assert _embeddings_from_response(payload) == [[0.1, 0.2], [0.3, 0.4]]


def test_openai_shape_out_of_order_is_sorted_by_index():
    """Vectors come back in request order even if the server reorders rows."""
    payload = {
        "data": [
            {"embedding": [0.3, 0.4], "index": 1},
            {"embedding": [0.1, 0.2], "index": 0},
        ]
    }
    assert _embeddings_from_response(payload) == [[0.1, 0.2], [0.3, 0.4]]


def test_error_payload_is_not_an_embedding():
    """An error body must read as a failed attempt, not an empty success."""
    assert _embeddings_from_response({"error": "context length exceeded"}) is None
    assert _embeddings_from_response({"data": [{"index": 0}]}) is None
    assert _embeddings_from_response("nope") is None
