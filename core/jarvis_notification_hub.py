#!/usr/bin/env python3
"""
jarvis_notification_hub — Multi-channel notification dispatcher
Routes alerts and messages to Telegram, Redis pub/sub, file log, and webhooks
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.notification_hub")

REDIS_PREFIX = "jarvis:notif:"
NOTIF_LOG = Path("/home/turbo/IA/Core/jarvis/data/notifications.jsonl")

# Override in config or env
TELEGRAM_BOT_TOKEN = ""  # Set via env or config
TELEGRAM_CHAT_ID = ""  # Set via env or config


class Channel(str, Enum):
    REDIS = "redis"
    TELEGRAM = "telegram"
    WEBHOOK = "webhook"
    FILE = "file"
    STDOUT = "stdout"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Notification:
    title: str
    message: str
    priority: Priority = Priority.NORMAL
    source: str = "jarvis"
    tags: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    channels: list[Channel] = field(default_factory=list)
    notif_id: str = ""

    def to_dict(self) -> dict:
        return {
            "notif_id": self.notif_id,
            "title": self.title,
            "message": self.message,
            "priority": self.priority.value,
            "source": self.source,
            "tags": self.tags,
            "ts": self.ts,
        }


@dataclass
class DeliveryResult:
    channel: Channel
    success: bool
    error: str = ""
    latency_ms: float = 0.0


@dataclass
class ChannelConfig:
    channel: Channel
    enabled: bool = True
    min_priority: Priority = Priority.NORMAL
    # Channel-specific settings
    settings: dict = field(default_factory=dict)


class NotificationHub:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._channels: dict[Channel, ChannelConfig] = {
            Channel.REDIS: ChannelConfig(
                Channel.REDIS, enabled=True, min_priority=Priority.LOW
            ),
            Channel.FILE: ChannelConfig(
                Channel.FILE, enabled=True, min_priority=Priority.LOW
            ),
            Channel.STDOUT: ChannelConfig(
                Channel.STDOUT, enabled=True, min_priority=Priority.NORMAL
            ),
            Channel.TELEGRAM: ChannelConfig(
                Channel.TELEGRAM, enabled=False, min_priority=Priority.HIGH
            ),
            Channel.WEBHOOK: ChannelConfig(
                Channel.WEBHOOK, enabled=False, min_priority=Priority.NORMAL
            ),
        }
        self._stats: dict[str, int] = {c.value: 0 for c in Channel}
        self._history: list[Notification] = []
        self._counter = 0
        NOTIF_LOG.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            self._channels[Channel.REDIS].enabled = True
        except Exception:
            self.redis = None

    def configure(self, channel: Channel, **settings):
        cfg = self._channels.get(channel)
        if cfg:
            cfg.enabled = settings.pop("enabled", cfg.enabled)
            cfg.min_priority = settings.pop("min_priority", cfg.min_priority)
            cfg.settings.update(settings)

    def _next_id(self) -> str:
        self._counter += 1
        return f"n{int(time.time())}-{self._counter:04d}"

    def _priority_value(self, p: Priority) -> int:
        return {
            Priority.LOW: 0,
            Priority.NORMAL: 1,
            Priority.HIGH: 2,
            Priority.URGENT: 3,
        }[p]

    async def send(
        self,
        title: str,
        message: str,
        priority: Priority = Priority.NORMAL,
        source: str = "jarvis",
        tags: list[str] | None = None,
        data: dict | None = None,
        channels: list[Channel] | None = None,
    ) -> list[DeliveryResult]:
        notif = Notification(
            notif_id=self._next_id(),
            title=title,
            message=message,
            priority=priority,
            source=source,
            tags=tags or [],
            data=data or {},
            channels=channels or [],
        )
        self._history.append(notif)

        # Determine target channels
        target_channels = notif.channels or [
            ch
            for ch, cfg in self._channels.items()
            if cfg.enabled
            and self._priority_value(priority) >= self._priority_value(cfg.min_priority)
        ]

        results = await asyncio.gather(
            *[self._deliver(notif, ch) for ch in target_channels],
            return_exceptions=True,
        )

        return [r for r in results if isinstance(r, DeliveryResult)]

    async def _deliver(self, notif: Notification, channel: Channel) -> DeliveryResult:
        t0 = time.time()
        try:
            if channel == Channel.REDIS:
                await self._send_redis(notif)
            elif channel == Channel.TELEGRAM:
                await self._send_telegram(notif)
            elif channel == Channel.WEBHOOK:
                await self._send_webhook(notif)
            elif channel == Channel.FILE:
                self._send_file(notif)
            elif channel == Channel.STDOUT:
                self._send_stdout(notif)

            self._stats[channel.value] += 1
            return DeliveryResult(
                channel=channel, success=True, latency_ms=(time.time() - t0) * 1000
            )
        except Exception as e:
            log.debug(f"Notification delivery failed [{channel}]: {e}")
            return DeliveryResult(channel=channel, success=False, error=str(e))

    async def _send_redis(self, notif: Notification):
        if not self.redis:
            raise RuntimeError("Redis not connected")
        await self.redis.publish(
            "jarvis:notifications",
            json.dumps(notif.to_dict()),
        )
        await self.redis.lpush(f"{REDIS_PREFIX}history", json.dumps(notif.to_dict()))
        await self.redis.ltrim(f"{REDIS_PREFIX}history", 0, 999)

    async def _send_telegram(self, notif: Notification):
        cfg = self._channels[Channel.TELEGRAM].settings
        token = cfg.get("token", TELEGRAM_BOT_TOKEN)
        chat_id = cfg.get("chat_id", TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            raise RuntimeError("Telegram not configured")

        priority_emoji = {"low": "ℹ️", "normal": "📢", "high": "⚠️", "urgent": "🚨"}
        emoji = priority_emoji.get(notif.priority.value, "📢")
        text = f"{emoji} *{notif.title}*\n{notif.message}"

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            ) as r:
                if r.status != 200:
                    data = await r.text()
                    raise RuntimeError(f"Telegram error: {r.status} {data[:100]}")

    async def _send_webhook(self, notif: Notification):
        cfg = self._channels[Channel.WEBHOOK].settings
        url = cfg.get("url")
        if not url:
            raise RuntimeError("Webhook URL not configured")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sess:
            async with sess.post(url, json=notif.to_dict()) as r:
                if r.status >= 400:
                    raise RuntimeError(f"Webhook error: {r.status}")

    def _send_file(self, notif: Notification):
        with open(NOTIF_LOG, "a") as f:
            f.write(json.dumps(notif.to_dict()) + "\n")

    def _send_stdout(self, notif: Notification):
        icons = {"low": "·", "normal": "●", "high": "◆", "urgent": "★"}
        icon = icons.get(notif.priority.value, "●")
        print(
            f"[NOTIF] {icon} [{notif.priority.value.upper()}] {notif.title}: {notif.message}"
        )

    # ── Convenience methods ────────────────────────────────────────────────────

    async def info(self, title: str, message: str, **kwargs) -> list[DeliveryResult]:
        return await self.send(title, message, Priority.LOW, **kwargs)

    async def warning(self, title: str, message: str, **kwargs) -> list[DeliveryResult]:
        return await self.send(title, message, Priority.HIGH, **kwargs)

    async def critical(
        self, title: str, message: str, **kwargs
    ) -> list[DeliveryResult]:
        return await self.send(title, message, Priority.URGENT, **kwargs)

    def recent(self, limit: int = 20) -> list[dict]:
        return [n.to_dict() for n in reversed(self._history[-limit:])]

    def stats(self) -> dict:
        return {
            "total_sent": sum(self._stats.values()),
            "by_channel": dict(self._stats),
            "history_size": len(self._history),
            "channels_enabled": [
                ch.value for ch, cfg in self._channels.items() if cfg.enabled
            ],
        }


async def main():
    import sys

    hub = NotificationHub()
    await hub.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        results = await hub.send(
            "GPU Temperature Alert",
            "GPU0 reached 91°C — throttling may occur",
            priority=Priority.HIGH,
            source="gpu_monitor",
            tags=["gpu", "thermal"],
        )
        for r in results:
            status = "✅" if r.success else f"❌ {r.error}"
            print(f"  {r.channel.value}: {status} ({r.latency_ms:.0f}ms)")

        await hub.info("System", "All services operational")
        await hub.warning("Redis", "Connection pool 85% utilized")

        print(f"\nStats: {json.dumps(hub.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(hub.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
