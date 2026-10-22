"""Shared connection suggestions; defaults never initiate a connection."""

import os


DEFAULT_RELAY = "ws://117.50.48.22:8817/ws"


def default_relay() -> str:
    """Allow deployment-specific suggestions without changing saved targets."""
    return os.environ.get("SUPERMONO_DEFAULT_RELAY", "").strip() or DEFAULT_RELAY
