"""Shared connection suggestions; defaults never initiate a connection."""

import os
from urllib.parse import urlsplit


LEGACY_RELAY = "ws://117.50.48.22:8817/ws"
DEFAULT_RELAY = "wss://117.50.48.22/ws"
DEFAULT_UPDATE_FEED = "https://117.50.48.22/updates/stable/manifest.json"


def default_relay() -> str:
    """Allow deployment-specific suggestions without changing saved targets."""
    return os.environ.get("SUPERMONO_DEFAULT_RELAY", "").strip() or DEFAULT_RELAY


def migrate_legacy_relay(value: str) -> str:
    """Upgrade only the known same-host legacy suggestion, without connecting.

    Deployment overrides may name a different server and must never receive a
    saved connection's credentials through this migration. Custom URLs, existing
    TLS targets, and local development connections remain untouched.
    """
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme in {"ws", "http"} and parts.hostname == "117.50.48.22"
                and parts.port == 8817 and parts.path in {"", "/", "/ws", "/ws/"}
                and parts.username is None and parts.password is None
                and not parts.query and not parts.fragment):
            return DEFAULT_RELAY
    except ValueError:
        pass
    return value


def default_update_feed() -> str:
    """The signed update channel is independent of terminal relay credentials."""
    return os.environ.get("SUPERMONO_UPDATE_FEED", "").strip() or DEFAULT_UPDATE_FEED
