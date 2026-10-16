"""Compatible incremental JSON encoding for large canonical snapshots."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Callable


def canonical_json_chunks(value: Any, *, default: Callable | None = None) -> Iterator[bytes]:
    # iterencode's normal path walks containers in Python instead of handing
    # the entire document to the one-shot C encoder. A worker thread can yield
    # interpreter time during that traversal; checksum callers need no second
    # complete copy of the encoded document.
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), default=default)
    for chunk in encoder.iterencode(value):
        yield chunk.encode("utf-8")


def canonical_json_bytes(value: Any, *, default: Callable | None = None) -> bytes:
    result = bytearray()
    for chunk in canonical_json_chunks(value, default=default):
        result.extend(chunk)
    return bytes(result)
