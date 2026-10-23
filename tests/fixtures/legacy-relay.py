"""Pair an outbound host with outbound phones and copy frames. Do not decode them."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import struct
from dataclasses import dataclass, field

from websockets.asyncio.server import serve

from . import web

QUEUE_FRAMES = 32
FIRST_FRAME_S = 2.0


@dataclass
class Room:
    host: object | None = None
    phones: dict = field(default_factory=dict)
    queues: dict = field(default_factory=dict)
    tokens: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    next_id: int = 1


class Relay:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def room(self, code: str) -> Room:
        return self.rooms.setdefault(code, Room())

    async def serve_page(self, connection, request):
        page = importlib.reload(web)
        path = request.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            return page.text_response(connection, 200, page.PAGE, "text/html; charset=utf-8")
        if path in page.STATIC:
            name, content_type = page.STATIC[path]
            return page.text_response(connection, 200, page.static_text(name), content_type)
        if path == "/health":
            codes = [code for code, room in self.rooms.items() if room.host is not None]
            body = json.dumps({"ok": True, "role": "relay", "codes": codes})
            return page.text_response(connection, 200, body, "application/json")
        return None

    async def handler(self, ws) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), FIRST_FRAME_S)
            hello = json.loads(raw)
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError):
            await ws.close(code=4000)
            return
        code = str(hello.get("code") or "")
        role = hello.get("op")
        if not code or role not in {"host", "phone", "register", "login", "rotate"}:
            await ws.close(code=4000)
            return
        room = self.room(code)
        if role == "host":
            room.tokens = {str(item) for item in hello.get("tokens") or [] if str(item)}
            await self._host(ws, code, room)
        elif role in {"register", "login", "rotate"}:
            await self._account(ws, code, hello)
        else:
            token = str(hello.get("token") or "")
            if token not in room.tokens:
                await ws.close(code=4001)
                return
            await self._phone(ws, code, room, token)

    async def _host(self, ws, code: str, room: Room) -> None:
        if room.host is not None:
            await ws.close(code=4003)
            return
        room.host = ws
        await ws.send(json.dumps({"op": "ready"}))
        try:
            async for message in ws:
                if isinstance(message, str):
                    try:
                        ctrl = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if ctrl.get("op") == "tokens":
                        room.tokens = {str(item) for item in ctrl.get("tokens") or [] if str(item)}
                    elif ctrl.get("op") == "auth" and ctrl.get("id") is not None:
                        fut = room.pending.get(int(ctrl["id"]))
                        if fut is not None and not fut.done():
                            fut.set_result(ctrl)
                    continue
                if len(message) < 4:
                    continue
                phone_id = struct.unpack_from("<I", message)[0]
                self._enqueue(room, phone_id, message[4:])
        finally:
            if room.host is ws:
                room.host = None
            if room.host is None and not room.phones:
                self.rooms.pop(code, None)

    async def _account(self, ws, code: str, hello: dict) -> None:
        room = self.rooms.get(code)
        if room is None or room.host is None:
            await ws.send(json.dumps({"op": "auth", "error": "办公室主机还没连上"}, ensure_ascii=False))
            await ws.close(code=4004)
            return
        auth_id = room.next_id
        room.next_id += 1
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        room.pending[auth_id] = fut
        try:
            await room.host.send(json.dumps({
                "op": hello.get("op"),
                "id": auth_id,
                "email": hello.get("email") or hello.get("name") or "",
                "password": hello.get("password") or "",
            }))
            reply = await asyncio.wait_for(fut, 8)
            await ws.send(json.dumps(reply, ensure_ascii=False))
        except Exception:
            await ws.send(json.dumps({"op": "auth", "error": "注册没有完成"}, ensure_ascii=False))
        finally:
            room.pending.pop(auth_id, None)
            await ws.close()

    async def _phone(self, ws, code: str, room: Room, token: str) -> None:
        if room.host is None:
            await ws.close(code=4004)
            return
        phone_id = room.next_id
        room.next_id += 1
        queue: asyncio.Queue = asyncio.Queue(QUEUE_FRAMES)
        room.phones[phone_id] = ws
        room.queues[phone_id] = queue
        sender = asyncio.create_task(self._drain(ws, queue))
        await room.host.send(json.dumps({"op": "join", "id": phone_id, "token": token}))
        await ws.send(json.dumps({"op": "ready"}))
        try:
            async for message in ws:
                host = room.host
                if host is None:
                    break
                body = message if isinstance(message, bytes) else message.encode()
                await host.send(struct.pack("<I", phone_id) + body)
        finally:
            room.phones.pop(phone_id, None)
            room.queues.pop(phone_id, None)
            sender.cancel()
            host = room.host
            if host is not None:
                try:
                    await host.send(json.dumps({"op": "leave", "id": phone_id}))
                except Exception:
                    pass
            if room.host is None and not room.phones:
                self.rooms.pop(code, None)

    def _enqueue(self, room: Room, phone_id: int, message: bytes) -> None:
        queue = room.queues.get(phone_id)
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            room.phones.pop(phone_id, None)

    async def _drain(self, ws, queue: asyncio.Queue) -> None:
        try:
            while True:
                await ws.send(await queue.get())
        except asyncio.CancelledError:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMonoAgent relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6770)
    args = parser.parse_args()
    relay = Relay()

    async def run() -> None:
        async with serve(
            relay.handler,
            args.host,
            args.port,
            process_request=relay.serve_page,
            max_size=1024 * 1024,
            ping_interval=25,
            ping_timeout=25,
        ):
            print(f"relay ws://{args.host}:{args.port}")
            await asyncio.Future()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
