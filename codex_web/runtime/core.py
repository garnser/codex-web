"""Intentional compatibility namespace for historical runtime imports.

The composed application and focused installers may publish verified
compatibility aliases onto this module for historical server consumers. This
module owns no runtime behavior or mutable application state and deliberately
does not import or alias the deleted legacy runtime.
"""

from __future__ import annotations
