#!/usr/bin/env python3
"""
jarvis_websocket_relay — WebSocket relay server for real-time LLM streaming
Bridges HTTP/SSE LLM backends to WebSocket clients with room-based pub/sub
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web, WSMsgType

log = logging.getLogger("jarvis.websocket_relay")

WS_PORT = 8769
LM_URL = "http://127.0.0.1:1234"
REDIS_CHANNEL = "jarvis:ws:broadcast"


@dataclass
class WSClient:
    client_id: str
    ws: web.WebSocketResponse
    rooms: set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    messages_sent: int = 0
    model: str = "qwen/qwen3.5-9b"


class WebSocketRelay:
    def __init__(self, port: int = WS_PORT):
        self.port = port
        self.redis: aioredis.Redis | None = None
        self._clients: dict[str, WSClient] = {}
        self._rooms: dict[str, set[str]] = {}  # room → set of client_ids

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _join_room(self, client: WSClient, room: str):
        client.rooms.add(room)
        self._rooms.setdefault(room, set()).add(client.client_id)

    async def _leave_room(self, client: WSClient, room: str):
        client.rooms.discard(room)
        if room in self._rooms:
            self._rooms[room].discard(client.client_id)

    async def _broadcast_room(
        self, room: str, message: dict, exclude: str | None = None
    ):
        clients = self._rooms.get(room, set())
        dead = set()
        for cid in clients:
            if cid == exclude:
                continue
            client = self._clients.get(cid)
            if not client:
                dead.add(cid)
                continue
            try:
                await client.ws.send_json(message)
                client.messages_sent += 1
            except Exception:
                dead.add(cid)
        for cid in dead:
            self._rooms[room].discard(cid)

    async def _stream_llm(self, client: WSClient, prompt: str, session_id: str):
        """Stream LLM response token-by-token to the client."""
        messages = [{"role": "user", "content": prompt}]
        token_count = 0

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": client.model,
                        "messages": messages,
                        "max_tokens": 1024,
                        "temperature": 0.7,
                        "stream": True,
                    },
                ) as r:
                    if r.status != 200:
                        await client.ws.send_json(
                            {"type": "error", "message": f"LLM HTTP {r.status}"}
                        )
                        return

                    full_text = ""
                    async for line_bytes in r.content:
                        line = line_bytes.decode().strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                full_text += token
                                token_count += 1
                                await client.ws.send_json(
                                    {
                                        "type": "token",
                                        "session_id": session_id,
                                        "token": token,
                                        "index": token_count,
                                    }
                                )
                                client.messages_sent += 1
                        except Exception:
                            pass

                    await client.ws.send_json(
                        {
                            "type": "done",
                            "session_id": session_id,
                            "text": full_text,
                            "tokens": token_count,
                        }
                    )

        except Exception as e:
            log.error(f"Stream error: {e}")
            try:
                await client.ws.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        client_id = str(uuid.uuid4())[:8]
        client = WSClient(client_id=client_id, ws=ws)
        self._clients[client_id] = client

        # Welcome
        await ws.send_json(
            {
                "type": "connected",
                "client_id": client_id,
                "ts": time.time(),
            }
        )
        log.info(f"WS connected: {client_id}")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_message(client, msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except Exception as e:
            log.error(f"WS error [{client_id}]: {e}")
        finally:
            # Cleanup
            for room in list(client.rooms):
                await self._leave_room(client, room)
            del self._clients[client_id]
            log.info(f"WS disconnected: {client_id}")

        return ws

    async def _handle_message(self, client: WSClient, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            await client.ws.send_json({"type": "error", "message": "Invalid JSON"})
            return

        msg_type = msg.get("type", "")

        if msg_type == "chat":
            prompt = msg.get("prompt", "")
            session_id = msg.get("session_id", str(uuid.uuid4())[:8])
            if msg.get("model"):
                client.model = msg["model"]
            asyncio.create_task(self._stream_llm(client, prompt, session_id))

        elif msg_type == "join":
            room = msg.get("room", "default")
            await self._join_room(client, room)
            await client.ws.send_json({"type": "joined", "room": room})

        elif msg_type == "leave":
            room = msg.get("room", "default")
            await self._leave_room(client, room)
            await client.ws.send_json({"type": "left", "room": room})

        elif msg_type == "broadcast":
            room = msg.get("room", "default")
            payload = msg.get("payload", {})
            await self._broadcast_room(
                room,
                {
                    "type": "broadcast",
                    "from": client.client_id,
                    "room": room,
                    "payload": payload,
                    "ts": time.time(),
                },
                exclude=client.client_id,
            )

        elif msg_type == "ping":
            await client.ws.send_json({"type": "pong", "ts": time.time()})

        elif msg_type == "status":
            await client.ws.send_json(
                {
                    "type": "status",
                    "client_id": client.client_id,
                    "rooms": list(client.rooms),
                    "model": client.model,
                    "messages_sent": client.messages_sent,
                    "connected_s": round(time.time() - client.connected_at, 1),
                }
            )

        else:
            await client.ws.send_json(
                {"type": "error", "message": f"Unknown type: {msg_type}"}
            )

    async def handle_stats(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(
                {
                    "clients": len(self._clients),
                    "rooms": {r: len(c) for r, c in self._rooms.items()},
                    "ts": time.time(),
                }
            ),
            content_type="application/json",
        )

    async def run(self):
        app = web.Application()
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/stats", self.handle_stats)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info(f"WebSocket relay on ws://0.0.0.0:{self.port}/ws")
        return runner


async def main():
    import sys

    relay = WebSocketRelay()
    await relay.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd == "serve":
        runner = await relay.run()
        print(f"WS relay running on ws://0.0.0.0:{relay.port}/ws (Ctrl+C to stop)")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()

    elif cmd == "test":
        # Quick WS client test
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"ws://127.0.0.1:{relay.port}/ws") as ws:
                msg = await ws.receive_json()
                print(f"Connected: {msg}")
                await ws.send_json({"type": "ping"})
                pong = await ws.receive_json()
                print(f"Pong: {pong}")


if __name__ == "__main__":
    asyncio.run(main())
