"""Mutable save baselines whose lifetime follows the loaded collection."""

from __future__ import annotations

import threading
import weakref
from typing import Any


class SnapshotDict(dict):
    __slots__ = ("__weakref__",)


class SnapshotList(list):
    __slots__ = ("__weakref__",)


class SaveSnapshots:
    def __init__(self) -> None:
        self._entries: dict[int, tuple[weakref.ReferenceType, Any]] = {}
        self._lock = threading.RLock()

    def remember(self, collection: SnapshotDict | SnapshotList, baseline: Any) -> None:
        key = id(collection)

        def discarded(reference: weakref.ReferenceType) -> None:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry[0] is reference:
                    del self._entries[key]

        reference = weakref.ref(collection, discarded)
        with self._lock:
            self._entries[key] = (reference, baseline)

    def take(self, collection: Any) -> Any:
        with self._lock:
            entry = self._entries.get(id(collection))
            if entry is None or entry[0]() is not collection:
                return None
            del self._entries[id(collection)]
            return entry[1]
