"""Bounded-memory observation of immutable snapshot string lengths.

This contains one observed corruption shape; it does not repair native memory
corruption or protect a string corrupted after the final observation.
"""
from itertools import chain


class SnapshotStringMetadataChanged(ValueError):
    pass


def _lengths(value):
    # Iterator frames and ancestor identities cost O(depth), not O(catalog).
    # StateStore documents contain JSON values; never stringify their contents.
    frames = [(None, iter((value,)))]
    ancestors = set()
    while frames:
        container, iterator = frames[-1]
        try:
            item = next(iterator)
        except StopIteration:
            frames.pop()
            ancestors.discard(container)
            continue
        if isinstance(item, str):
            # Match the native encoder's actual Unicode length, including for
            # a str subclass whose Python __len__ could return another value.
            yield str.__len__(item)
        elif isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in ancestors:
                raise SnapshotStringMetadataChanged("Snapshot contains a circular document")
            ancestors.add(identity)
            children = chain.from_iterable(item.items()) if isinstance(item, dict) else iter(item)
            frames.append((identity, children))


def observe_snapshot_string_bound(documents):
    """Retain one scalar bound before the initial canonical checksum pass."""
    return max(_lengths(documents), default=0)


def require_snapshot_string_bound(documents, bound):
    """Reject impossible growth before asking the native encoder to scan it.

    Snapshot strings are immutable. This bound comes from the actual snapshot,
    not a guessed payload limit. It detects growth beyond the original maximum;
    changes within that maximum and later races remain outside this containment.
    """
    for length in _lengths(documents):
        if length > bound:
            raise SnapshotStringMetadataChanged(
                "Snapshot string metadata changed after initial observation; capture refused")
