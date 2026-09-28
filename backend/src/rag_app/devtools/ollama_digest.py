"""Digest of the embedding model served by Ollama (gate seed staleness, DA-B-9).

The gate seed stores document vectors made with one embedding model build. If Ollama later
serves a different build under the same name, query vectors no longer match the stored
ones and eval shifts silently, so ``gate.sh`` records the digest in ``seed.meta`` and fails
on a mismatch.

    python -m rag_app.devtools.ollama_digest [--host URL] [--model NAME]

Prints ``<model> <digest>``. Defaults: the app settings' ``OLLAMA_HOST`` / ``EMBED_MODEL``.
Exit 1 when Ollama is unreachable or does not serve the model.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx


def model_digest(tags: dict[str, Any], model: str) -> str | None:
    """Digest of ``model`` in an ``/api/tags`` response (a bare name means ``:latest``)."""
    wanted = {model} if ":" in model else {model, f"{model}:latest"}
    for entry in tags.get("models", []):
        if entry.get("name") in wanted or entry.get("model") in wanted:
            digest = entry.get("digest")
            return str(digest) if digest else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the Ollama digest of a model.")
    parser.add_argument("--host")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    host, model = args.host, args.model
    if not host or not model:
        from rag_app.config import get_settings

        settings = get_settings()
        host = host or settings.ollama_host
        model = model or settings.embed_model
    try:
        response = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=10)
        response.raise_for_status()
        tags = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"ollama_digest: {host} unreachable: {exc}", file=sys.stderr)
        return 1
    digest = model_digest(tags, model)
    if digest is None:
        print(f"ollama_digest: {model} not served by {host}", file=sys.stderr)
        return 1
    print(f"{model} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
