"""
Turn text into vectors, locally, with the standard library.

Ollama's `/api/embed`. That is a different endpoint from the OpenAI-compatible
chat shim `provider.py` talks to, with a different request and response shape,
so it gets its own small client rather than a flag on that one.

## The asymmetry that has no error message

Snowflake Arctic Embed v2 is trained asymmetrically: a **query** is embedded
with a `query: ` prefix and a **document** with none. Send both the same way
and retrieval quietly gets worse - no exception, no warning, just a collection
that ranks slightly wrong forever. So the two directions are separate
functions here, the prefix is config rather than a literal, and the prefix that
was actually used is recorded against every stored vector.

Nothing leaves the machine: the endpoint is localhost.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config


class EmbedError(RuntimeError):
    """The embedding model could not be reached or did not answer usefully."""


def text_sha(text: str) -> str:
    """The hash stored against a vector to prove which string produced it."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _request(inputs: Sequence[str], timeout: int) -> List[List[float]]:
    """One embedding call, to whichever endpoint EMBED_URL names.

    TWO WIRE FORMATS, told apart by the response, not by the URL.

    Both send `{"model", "input"}`, so the request is shared. They disagree on
    the way back:

        Ollama /api/embed        {"embeddings": [[...], ...]}
        OpenAI /v1/embeddings    {"data": [{"embedding": [...], "index": 0}]}

    Sniffing the response rather than the hostname means a corporate gateway,
    Azure OpenAI, or a self-hosted OpenAI-shaped server all work without a new
    setting - and the failure, if the shape is neither, names what came back
    instead of raising a KeyError three frames down.
    """
    body = json.dumps({"model": config.EMBED_MODEL,
                       "input": list(inputs)}).encode("utf-8")
    headers = {"content-type": "application/json"}
    # A hosted endpoint needs a key; Ollama has none and must not be sent one.
    # Read at call time, not at import, so a key exported after the module
    # loaded still works - and so the key is never a module-level global.
    key = config.embed_key()
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        config.EMBED_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))

    if "embeddings" in payload:                      # Ollama /api/embed
        vectors = payload["embeddings"]
    elif "data" in payload:                          # OpenAI /v1/embeddings
        # `index` is authoritative: the API does not promise input order, and
        # silently mismatched vectors would be undetectable - every page would
        # embed successfully and retrieve the wrong thing.
        rows = payload["data"]
        try:
            rows = sorted(rows, key=lambda r: r.get("index", 0))
            vectors = [r["embedding"] for r in rows]
        except (TypeError, KeyError) as exc:
            raise EmbedError(f"OpenAI-shaped response with no embedding: "
                             f"{exc}") from None
    elif "embedding" in payload:                     # older /api/embeddings
        vectors = [payload["embedding"]]
    else:
        raise EmbedError(f"unexpected response shape: "
                         f"{sorted(payload)[:6]}")
    if len(vectors) != len(inputs):
        raise EmbedError(f"asked for {len(inputs)} vectors, got {len(vectors)}")
    return vectors


def embed(texts: Sequence[str], *, is_query: bool = False,
          attempts: int = 3, timeout: int = 300) -> List[List[float]]:
    """Vectors for `texts`, prefixed for their direction.

    Batched in one request because Ollama accepts a list and the round trip
    dominates for short pages.
    """
    if not texts:
        return []
    prefix = (config.EMBED_QUERY_PREFIX if is_query
              else config.EMBED_DOC_PREFIX)
    prepared = [prefix + t for t in texts]

    last = ""
    for attempt in range(attempts):
        try:
            vectors = _request(prepared, timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 404:
                raise EmbedError(
                    f"{config.EMBED_MODEL} is not pulled on this machine.\n"
                    f"       ollama pull {config.EMBED_MODEL}") from None
            last = f"HTTP {exc.code}: {body}"
            if exc.code < 500 and exc.code not in (408, 409, 429):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbedError(
                f"cannot reach {config.EMBED_URL} - {type(exc).__name__}.\n"
                f"       Is the server running?  ollama serve") from None
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise EmbedError(f"unreadable response: {exc}") from None
        else:
            _check_dimension(vectors[0])
            return vectors
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)
    raise EmbedError(last or "the model returned nothing")


def embed_one(text: str, *, is_query: bool = False) -> List[float]:
    return embed([text], is_query=is_query)[0]


_checked = False


def _check_dimension(vector: Sequence[float]) -> None:
    """Fail loudly the first time the model returns an unexpected width.

    A collection is created with a fixed dimension. A model that returns 768
    where 1024 was configured cannot be stored at all, and the useful place to
    say so is here - with the model named - rather than inside a vector store
    error about shapes.
    """
    global _checked
    if _checked:
        return
    if len(vector) != config.EMBED_DIM:
        raise EmbedError(
            f"{config.EMBED_MODEL} returned {len(vector)} dimensions, not "
            f"{config.EMBED_DIM}.\n"
            f"       Set ALPHAPLUS_EMBED_DIM={len(vector)} if that is the "
            f"model you want, and rebuild the collection - vectors of "
            f"different widths cannot share one.")
    _checked = True


def describe() -> str:
    """One line for status and doctor output."""
    return (f"{config.EMBED_MODEL} at {config.EMBED_URL}, "
            f"{config.EMBED_DIM} dims, "
            f"query prefix {config.EMBED_QUERY_PREFIX!r}")


def check() -> int:
    """`python -m kb.summarise.embed` - does the model answer, and how wide?"""
    print()
    print(f"  {describe()}")
    try:
        query = embed_one("does this model answer", is_query=True)
        doc = embed_one("a document about customer credit limits")
    except EmbedError as exc:
        print(f"  FAIL {exc}")
        return 1
    print(f"  ok   query vector  {len(query)} dims")
    print(f"  ok   document vector {len(doc)} dims")

    # Asymmetry sanity check: the same sentence embedded as a query and as a
    # document must NOT come back identical, or the prefix is not being applied
    # and the model is being used symmetrically without anyone noticing.
    same = embed_one("customer credit limits", is_query=True)
    other = embed_one("customer credit limits")
    identical = all(abs(a - b) < 1e-9 for a, b in zip(same, other))
    if config.EMBED_QUERY_PREFIX and identical:
        print("  warn query and document vectors are identical - the query "
              "prefix is having no effect")
    else:
        print("  ok   query and document encodings differ, as they should")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
