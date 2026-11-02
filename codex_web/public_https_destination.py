"""Validate a public HTTPS route before a trusted dependency broker connects.

This is not an authority or allowlist service. The caller must independently
check the canonical profile's exact hostname declaration and own transport
lifetime. Returned addresses are numeric: transport must use these routes, not
resolve the original hostname again after validation.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable


class PublicHTTPSDestinationDenied(ValueError):
    """The complete destination observation does not establish a public route."""


def validate_public_https_host(host: str) -> str:
    """Return one canonical ASCII DNS name, never a URL, IP or wildcard."""
    if (not isinstance(host, str) or not host or len(host) > 253
            or not host.isascii() or host != host.strip()):
        raise PublicHTTPSDestinationDenied('A bounded exact ASCII DNS hostname is required')
    canonical = host.lower()
    labels = canonical.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                              for label in labels):
        raise PublicHTTPSDestinationDenied('A fully qualified DNS hostname is required')
    # Numeric final labels include legacy numeric IPv4 spellings that some
    # resolver implementations accept without ordinary DNS resolution.
    if labels[-1].isdigit() or all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', label) for label in labels):
        raise PublicHTTPSDestinationDenied('Numeric address literals are not DNS hostnames')
    return canonical


def resolve_public_https_destination(
    host: str,
    port: int = 443,
    *,
    resolver: Callable = socket.getaddrinfo,
) -> tuple[tuple[int, int, tuple], ...]:
    """Validate ALL DNS answers and return immutable numeric TCP routes.

    Any non-public, mixed, scoped, unsupported or malformed answer refuses the
    whole observation. There is no fallback to a partial public subset. This
    function neither connects nor supplies permission to connect.
    """
    canonical = validate_public_https_host(host)
    if type(port) is not int or port != 443:
        raise PublicHTTPSDestinationDenied('Only HTTPS port 443 is supported')
    try:
        answers = resolver(canonical, port, type=socket.SOCK_STREAM)
    except OSError:
        raise PublicHTTPSDestinationDenied('Public HTTPS DNS resolution failed') from None
    if not isinstance(answers, (tuple, list)) or not 1 <= len(answers) <= 64:
        raise PublicHTTPSDestinationDenied('A bounded complete DNS answer set is required')
    routes = []
    for answer in answers:
        if not isinstance(answer, (tuple, list)) or len(answer) != 5:
            raise PublicHTTPSDestinationDenied('Malformed HTTPS DNS answer')
        family, kind, protocol, _, address = answer
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP
                or not isinstance(address, tuple)
                or len(address) != (2 if family == socket.AF_INET else 4)):
            raise PublicHTTPSDestinationDenied('Unsupported HTTPS DNS route')
        numeric, resolved_port = address[:2]
        if (not isinstance(numeric, str) or '%' in numeric
                or type(resolved_port) is not int or resolved_port != port):
            raise PublicHTTPSDestinationDenied('Malformed numeric HTTPS destination')
        if family == socket.AF_INET6 and address[2:] != (0, 0):
            raise PublicHTTPSDestinationDenied('Scoped HTTPS destinations are not supported')
        try:
            ip = ipaddress.ip_address(numeric)
        except ValueError:
            raise PublicHTTPSDestinationDenied('DNS did not return a numeric IP address') from None
        if (ip.version != (4 if family == socket.AF_INET else 6)
                or not ip.is_global or ip.is_private or ip.is_loopback
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or ip.is_reserved or getattr(ip, 'ipv4_mapped', None) is not None
                or getattr(ip, 'sixtofour', None) is not None
                or getattr(ip, 'teredo', None) is not None):
            raise PublicHTTPSDestinationDenied('DNS returned a non-public HTTPS destination')
        route = (family, protocol, (str(ip), port) if ip.version == 4 else (str(ip), port, 0, 0))
        if route not in routes:
            routes.append(route)
    return tuple(routes)
