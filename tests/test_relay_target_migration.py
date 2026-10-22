"""Saved host relay targets upgrade before the daemon's TLS-only uplink gate."""
import asyncio

import pytest

from supermonoagent.daemon import clean_relays, relay_uplink
from supermonoagent.defaults import DEFAULT_RELAY


@pytest.mark.parametrize("url", [
    "ws://117.50.48.22:8817/ws", "http://117.50.48.22:8817",
    "ws://117.50.48.22:8817/ws/", "117.50.48.22:8817",
])
def test_saved_legacy_host_reaches_secure_connector(url, monkeypatch):
    monkeypatch.setenv("SUPERMONO_DEFAULT_RELAY", "wss://unrelated.example/ws")
    targets = clean_relays([{"url": url, "code": "same-room", "owner_secret": "same-owner-secret"}])
    assert targets[0]["url"] == DEFAULT_RELAY
    assert targets[0]["code"] == "same-room"
    assert targets[0]["owner_secret"] == "same-owner-secret"
    contacted = []

    class StopBeforeNetwork:
        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *_):
            pass

    def connector(address, **_):
        contacted.append(address)
        return StopBeforeNetwork()

    monkeypatch.setattr("websockets.connect", connector)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(relay_uplink(None, targets[0]["url"], "same-room", "fixture"))
    assert contacted == [DEFAULT_RELAY]


def test_migrated_target_deduplicates_and_preserves_explicit_alternatives():
    values = ["ws://127.0.0.1:8817/ws", "wss://custom.example/ws", "wss://117.50.48.22:8817/ws"]
    targets = clean_relays([
        {"url": "ws://117.50.48.22:8817/ws", "code": "same-room"},
        {"url": DEFAULT_RELAY, "code": "same-room"},
        *[{"url": url, "code": "other-room"} for url in values],
    ])
    assert [item["url"] for item in targets] == [DEFAULT_RELAY, *values]
