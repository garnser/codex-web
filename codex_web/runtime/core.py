"""Thin compatibility alias for the quarantined legacy runtime.

New runtime behavior belongs in composed services, API routers, storage modules,
or focused runtime modules. ``legacy_core`` exists only while the remaining
historical implementation is extracted and deleted.
"""
from __future__ import annotations

import sys

from . import legacy_core as _legacy_core

# Preserve module identity for existing consumers. Functions defined in the
# legacy module continue to resolve globals from that same module, so
# application-level compatibility rebinding remains effective during migration.
sys.modules[__name__] = _legacy_core
