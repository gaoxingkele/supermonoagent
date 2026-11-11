"""Local address candidates. Uses connected UDP sockets only to read the routing choice; sends nothing."""
from __future__ import annotations

import ipaddress
import socket


def _route_source(family: int, probe: str) -> str | None:
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect((probe, 53))
            return sock.getsockname()[0]
    except OSError:
        return None


def _addresses(family: int) -> set[str]:
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, family):
            found.add(info[4][0])
    except OSError:
        pass
    return found


def lan_ipv4() -> list[str]:
    found = _addresses(socket.AF_INET)
    routed = _route_source(socket.AF_INET, '8.8.8.8')
    if routed:
        found.add(routed)
    result = []
    for value in found:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        result.append(value)
    # The routed address first: it is the interface the peer most likely shares.
    return sorted(result, key=lambda v: (v != routed, v))


def global_ipv6() -> list[str]:
    found = _addresses(socket.AF_INET6)
    routed = _route_source(socket.AF_INET6, '2001:4860:4860::8888')
    if routed:
        found.add(routed)
    result = []
    for value in found:
        try:
            ip = ipaddress.ip_address(value.split('%')[0])
        except ValueError:
            continue
        if ip.is_global:
            result.append(str(ip))
    return sorted(result, key=lambda v: (v != routed, v))[:4]


def candidates(port: int, *, lan: bool = True, ipv6: bool = True) -> list[dict]:
    items = []
    if lan:
        items += [{'kind': 'lan', 'host': host, 'port': port} for host in lan_ipv4()[:6]]
    if ipv6:
        items += [{'kind': 'ipv6', 'host': host, 'port': port} for host in global_ipv6()]
    return items


def is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False
