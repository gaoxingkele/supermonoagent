"""Shared transport boundary checks; all credentials are message/header scoped."""
from __future__ import annotations
import argparse
import ipaddress
import ssl
from urllib.parse import urlsplit

MAX_FRAME = 1024 * 1024
FIRST_FRAME_S = 2.0

def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'

def request_allowed(connection, request, allowed_origins=()):
    headers = getattr(request, 'headers', None)
    if headers is None:  # Lightweight in-process response fixtures have no HTTP transport.
        return True
    try:
        authority = headers.get('Host', '')
        parsed = urlsplit('http://' + authority)
        if not authority or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.netloc != authority or not parsed.hostname:
            return False
        port = parsed.port or 80
        origins = set(allowed_origins)
        local = getattr(connection, 'local_address', None)
        expected_port = local[1] if local else port
        local_authority = loopback(parsed.hostname) and port == expected_port
        configured_authority = any(urlsplit(item).netloc == authority for item in origins)
        if not (local_authority or configured_authority):
            return False
        origin = headers.get('Origin')
        if origin is None:
            return True
        if origin == 'null':
            return False
        if origin in origins:
            return True
        scheme = 'https' if getattr(connection, 'transport', None) and connection.transport.get_extra_info('ssl_object') else 'http'
        return local_authority and origin == scheme + '://' + authority
    except (ValueError, TypeError, LookupError):
        return False

def tls_context(cert=None, key=None):
    if bool(cert) != bool(key):
        raise ValueError('TLS requires both certificate and private key')
    if not cert:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context

def origin_value(value):
    parsed = urlsplit(value)
    try:
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('Expected an exact HTTP(S) origin without a path')
        parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def transport_options(parser):
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--allowed-origin', action='append', type=origin_value, default=[])
    parser.add_argument('--reverse-proxy-tls', action='store_true', help='TLS terminates at a configured trusted reverse proxy')
