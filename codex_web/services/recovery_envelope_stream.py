"""Bounded readers for existing single-envelope Recovery backups.

No keys, plaintext, storage writes or qualification live here. Ciphertext is
untrusted until the caller exhausts the second pass AND authenticates its tag.
Unusual but potentially legacy-valid JSON spellings use the original guarded
reader; they are not reported as corrupt merely because this reader declines.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import stat
from typing import BinaryIO, Callable, Iterator

READ_BYTES = 64 * 1024
MAX_METADATA_BYTES = 64 * 1024


class EnvelopeStreamRefused(ValueError):
    """Closed malformed/changed input refusal, never containing input bytes."""


class EnvelopeStreamUnsupported(EnvelopeStreamRefused):
    """Use the original reader with its original admission; not corruption."""


@dataclass(frozen=True)
class EnvelopeScan:
    metadata: dict
    ciphertext_size: int
    plaintext_size: int
    _identity: tuple = field(repr=False)
    _descriptor: int = field(repr=False)


def _identity(handle: BinaryIO) -> tuple:
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise EnvelopeStreamRefused('envelope_regular_file_required')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Reader:
    def __init__(self, handle, expected_size, checkpoint):
        self.handle, self.expected_size, self.checkpoint = handle, expected_size, checkpoint
        self.buffer, self.offset, self.read_size = b'', 0, 0
        self.digest = hashlib.sha256()

    def fill(self):
        if self.offset < len(self.buffer):
            return True
        self.checkpoint()
        block = self.handle.read(min(READ_BYTES, self.expected_size - self.read_size + 1))
        if not isinstance(block, bytes):
            raise EnvelopeStreamRefused('envelope_binary_reader_required')
        self.read_size += len(block)
        if self.read_size > self.expected_size:
            raise EnvelopeStreamRefused('envelope_size_changed')
        self.digest.update(block)
        self.buffer, self.offset = block, 0
        return bool(block)

    def peek(self):
        return self.buffer[self.offset] if self.fill() else None

    def byte(self):
        value = self.peek()
        if value is None:
            raise EnvelopeStreamRefused('envelope_truncated')
        self.offset += 1
        return value

    def space(self):
        while self.fill():
            block = self.buffer[self.offset:]
            stripped = block.lstrip(b' \t\r\n')
            self.offset += len(block) - len(stripped)
            if stripped:
                break

    def expect(self, value):
        if self.byte() != value:
            raise EnvelopeStreamRefused('envelope_invalid_json')


def _nonfinite(_value):
    raise EnvelopeStreamUnsupported('envelope_nonfinite_metadata_original_reader_required')


def _token(reader, limit):
    """Retain only a bounded JSON metadata token, never ciphertext."""
    token = bytearray()
    depth, quoted, escaped = 0, False, False
    first = reader.peek()
    if first is None:
        raise EnvelopeStreamRefused('envelope_truncated')
    while True:
        value = reader.peek()
        if value is None:
            break
        if not quoted and depth == 0 and token and value in b',} \t\r\n':
            break
        token.append(reader.byte())
        if len(token) > limit:
            raise EnvelopeStreamUnsupported('envelope_metadata_limit_original_reader_required')
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
                if first == 34 and depth == 0:
                    break
        elif value == 34:
            quoted = True
        elif value in (91, 123):
            depth += 1
        elif value in (93, 125):
            depth -= 1
            if depth == 0:
                break
    try:
        return json.loads(token, parse_constant=_nonfinite), len(token)
    except EnvelopeStreamUnsupported:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise EnvelopeStreamRefused('envelope_invalid_metadata') from None


def _ciphertext(reader) -> Iterator[bytes]:
    if reader.byte() != 34:
        raise EnvelopeStreamUnsupported('envelope_ciphertext_shape_original_reader_required')
    pending, padded = b'', False
    while True:
        if not reader.fill():
            raise EnvelopeStreamRefused('envelope_truncated')
        block = reader.buffer[reader.offset:]
        end = len(block)
        for special in (b'"', b'\\'):
            found = block.find(special)
            if found >= 0:
                end = min(end, found)
        part = block[:end]
        reader.offset += end
        if part:
            if any(value in part for value in (b' ', b'\t', b'\r', b'\n')):
                raise EnvelopeStreamUnsupported('envelope_ciphertext_whitespace_original_reader_required')
            if padded:
                raise EnvelopeStreamRefused('envelope_invalid_base64')
            encoded = pending + part
            length = len(encoded) // 4 * 4
            pending = encoded[length:]
            if length:
                chunk = encoded[:length]
                try:
                    decoded = base64.b64decode(chunk, validate=True)
                except (ValueError, binascii.Error):
                    raise EnvelopeStreamRefused('envelope_invalid_base64') from None
                padded = b'=' in chunk
                if padded and pending:
                    raise EnvelopeStreamRefused('envelope_invalid_base64')
                if decoded:
                    yield decoded
        if end < len(block):
            if block[end] == 92:
                raise EnvelopeStreamUnsupported('envelope_escaped_ciphertext_original_reader_required')
            reader.offset += 1  # closing quote
            if pending:
                raise EnvelopeStreamRefused('envelope_invalid_base64')
            return


def _walk(handle, expected_size, expected_sha256, checkpoint, scan=None):
    if (type(expected_size) is not int or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_sha256)):
        raise EnvelopeStreamRefused('envelope_expected_identity_required')
    checkpoint()
    identity = _identity(handle)
    if identity[2] != expected_size:
        raise EnvelopeStreamRefused('envelope_size_changed')
    if scan is not None and (identity != scan._identity or handle.fileno() != scan._descriptor):
        raise EnvelopeStreamRefused('envelope_file_changed')
    handle.seek(0)
    reader = _Reader(handle, expected_size, checkpoint)
    metadata, seen, retained, ciphertext_size = {}, set(), 0, 0
    reader.space()
    reader.expect(123)
    reader.space()
    if reader.peek() == 125:
        raise EnvelopeStreamRefused('envelope_ciphertext_missing')
    while True:
        key, size = _token(reader, MAX_METADATA_BYTES - retained)
        retained += size
        if not isinstance(key, str):
            raise EnvelopeStreamRefused('envelope_invalid_key')
        if key in seen:
            raise EnvelopeStreamUnsupported('envelope_duplicate_key_original_reader_required')
        seen.add(key)
        reader.space()
        reader.expect(58)
        reader.space()
        if key == 'ciphertext_b64':
            for chunk in _ciphertext(reader):
                checkpoint()
                ciphertext_size += len(chunk)
                yield chunk
        else:
            value, size = _token(reader, MAX_METADATA_BYTES - retained)
            retained += size
            metadata[key] = value
        reader.space()
        delimiter = reader.byte()
        if delimiter == 125:
            break
        if delimiter != 44:
            raise EnvelopeStreamRefused('envelope_invalid_json')
        reader.space()
    reader.space()
    if reader.peek() is not None or reader.read_size != expected_size:
        raise EnvelopeStreamRefused('envelope_trailing_or_truncated')
    checkpoint()
    if reader.digest.hexdigest() != expected_sha256 or _identity(handle) != identity:
        raise EnvelopeStreamRefused('envelope_checksum_or_file_changed')
    if 'ciphertext_b64' not in seen or ciphertext_size < 16:
        raise EnvelopeStreamRefused('envelope_ciphertext_tag_missing')
    result = EnvelopeScan(metadata, ciphertext_size, ciphertext_size - 16, identity, handle.fileno())
    if scan is not None and result != scan:
        raise EnvelopeStreamRefused('envelope_metadata_or_length_changed')
    return result


def scan_envelope(handle: BinaryIO, expected_size: int, expected_sha256: str,
                  checkpoint: Callable[[], None]) -> EnvelopeScan:
    """First pass validates/hash-counts without retaining encoded/decoded payload."""
    iterator = _walk(handle, expected_size, expected_sha256, checkpoint)
    while True:
        try:
            next(iterator)
        except StopIteration as completed:
            return completed.value


def iter_envelope_ciphertext(handle: BinaryIO, scan: EnvelopeScan, expected_size: int,
                             expected_sha256: str, checkpoint: Callable[[], None]) -> Iterator[bytes]:
    """Yield <=64KiB ciphertext blocks INCLUDING tag; MUST exhaust before exposure.

    A consumer abandoning iteration has no complete file/hash validation. These
    bytes convey no authentication, plaintext or Recovery qualification.
    """
    yield from _walk(handle, expected_size, expected_sha256, checkpoint, scan)
