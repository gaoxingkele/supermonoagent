"""Shared connection suggestions; defaults never initiate a connection."""

import os


DEFAULT_RELAY = "ws://117.50.48.22:8817/ws"
DEFAULT_UPDATE_FEED = "https://117.50.48.22/updates/stable/manifest.json"


def default_relay() -> str:
    """Allow deployment-specific suggestions without changing saved targets."""
    return os.environ.get("SUPERMONO_DEFAULT_RELAY", "").strip() or DEFAULT_RELAY


def default_update_feed() -> str:
    """The signed update channel is independent of terminal relay credentials."""
    return os.environ.get("SUPERMONO_UPDATE_FEED", "").strip() or DEFAULT_UPDATE_FEED
