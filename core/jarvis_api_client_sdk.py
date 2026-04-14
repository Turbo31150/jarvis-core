#!/usr/bin/env python3
"""
jarvis_api_client_sdk — Auto-generated Python SDK for JARVIS API
Typed async client with retry, auth, streaming, and pagination support
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiohttp

log = logging.getLogger("jarvis.api_client_sdk")

DEFAULT_BASE_URL = "http://127.0.0.1:8768"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RETRIES = 3


@dataclass
class APIResponse:
    status: int
    data: Any
    headers: dict
    latency_ms: float

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class ClientConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    retry_delay_s: float = 0.5
    user_agent: str = "JarvisSDK/1.0"


class JarvisAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}")


class JarvisClient:
    def __init__(self, config: ClientConfig | None = None):
        self.config = config or ClientConfig()
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            headers = {"User-Agent": self.config.user_agent}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._session = aiohttp.ClientSession(
                base_url=self.config.base_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_s),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> APIResponse:
        await self._ensure_session()
        last_err: Exception | None = None
        for attempt in range(self.config.retries):
            t0 = time.time()
            try:
                async with self._session.request(
                    method, path, json=json_body, params=params
                ) as r:
                    latency = (time.time() - t0) * 1000
                    try:
                        data = await r.json()
                    except Exception:
                        data = await r.text()
                    resp = APIResponse(
                        status=r.status,
                        data=data,
                        headers=dict(r.headers),
                        latency_ms=round(latency, 1),
                    )
                    if r.status >= 500 and attempt < self.config.retries - 1:
                        await asyncio.sleep(self.config.retry_delay_s * (attempt + 1))
                        continue
                    if r.status >= 400:
                        msg = (
                            data.get("detail", str(data))
                            if isinstance(data, dict)
                            else str(data)
                        )
                        raise JarvisAPIError(r.status, msg)
                    return resp
            except JarvisAPIError:
                raise
            except Exception as e:
                last_err = e
                if attempt < self.config.retries - 1:
                    await asyncio.sleep(self.config.retry_delay_s * (attempt + 1))
        raise last_err or RuntimeError("Request failed")

    # ── Inference endpoints ────────────────────────────────────────────────────

    async def infer(
        self,
        prompt: str,
        model: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> str:
        body: dict = {
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if model:
            body["model"] = model
        if system:
            body["system"] = system
        resp = await self._request("POST", "/infer", json_body=body)
        if isinstance(resp.data, dict):
            return resp.data.get("response", "") or resp.data.get("content", "")
        return str(resp.data)

    async def stream_infer(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        await self._ensure_session()
        body: dict = {"prompt": prompt, "max_tokens": max_tokens, "stream": True}
        if model:
            body["model"] = model
        async with self._session.post("/infer/stream", json=body) as r:
            if r.status != 200:
                raise JarvisAPIError(r.status, "Stream failed")
            async for line_bytes in r.content:
                line = line_bytes.decode().strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            chunk = json.loads(data)
                            token = chunk.get("token", "")
                            if token:
                                yield token
                        except Exception:
                            pass

    # ── Health ─────────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        resp = await self._request("GET", "/health")
        return resp.data if isinstance(resp.data, dict) else {"status": "ok"}

    async def ping(self) -> float:
        """Returns round-trip latency in ms."""
        t0 = time.time()
        await self.health()
        return round((time.time() - t0) * 1000, 1)

    # ── Models ─────────────────────────────────────────────────────────────────

    async def list_models(self) -> list[dict]:
        resp = await self._request("GET", "/models")
        if isinstance(resp.data, dict):
            return resp.data.get("models", resp.data.get("data", []))
        return resp.data if isinstance(resp.data, list) else []

    # ── Memory / KV ───────────────────────────────────────────────────────────

    async def memory_get(self, key: str) -> Any:
        resp = await self._request("GET", f"/memory/{key}")
        return resp.data

    async def memory_set(self, key: str, value: Any, ttl: int = 0) -> bool:
        body: dict = {"value": value}
        if ttl:
            body["ttl"] = ttl
        resp = await self._request("POST", f"/memory/{key}", json_body=body)
        return resp.ok

    async def memory_delete(self, key: str) -> bool:
        resp = await self._request("DELETE", f"/memory/{key}")
        return resp.ok

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def submit_task(self, task_type: str, payload: dict) -> str:
        resp = await self._request(
            "POST", "/tasks", json_body={"type": task_type, "payload": payload}
        )
        return resp.data.get("task_id", "") if isinstance(resp.data, dict) else ""

    async def get_task(self, task_id: str) -> dict:
        resp = await self._request("GET", f"/tasks/{task_id}")
        return resp.data if isinstance(resp.data, dict) else {}

    async def wait_task(
        self, task_id: str, timeout_s: float = 60.0, poll_s: float = 1.0
    ) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            task = await self.get_task(task_id)
            status = task.get("status", "")
            if status in ("done", "failed", "cancelled"):
                return task
            await asyncio.sleep(poll_s)
        raise TimeoutError(f"Task {task_id} did not complete within {timeout_s}s")

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def metrics(self) -> dict:
        resp = await self._request("GET", "/metrics/json")
        return resp.data if isinstance(resp.data, dict) else {}

    def stats(self) -> dict:
        return {
            "base_url": self.config.base_url,
            "timeout_s": self.config.timeout_s,
            "retries": self.config.retries,
            "session_open": self._session is not None and not self._session.closed,
        }


async def main():
    import sys

    config = ClientConfig(base_url=DEFAULT_BASE_URL)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"

    async with JarvisClient(config) as client:
        if cmd == "ping":
            try:
                ms = await client.ping()
                print(f"Ping: {ms}ms")
            except Exception as e:
                print(f"Ping failed: {e}")

        elif cmd == "health":
            try:
                h = await client.health()
                print(json.dumps(h, indent=2))
            except Exception as e:
                print(f"Health failed: {e}")

        elif cmd == "models":
            try:
                models = await client.list_models()
                for m in models:
                    mid = m.get("id", m.get("model_id", str(m)))
                    print(f"  {mid}")
            except Exception as e:
                print(f"Models failed: {e}")

        elif cmd == "infer" and len(sys.argv) > 2:
            prompt = " ".join(sys.argv[2:])
            try:
                result = await client.infer(prompt)
                print(result)
            except Exception as e:
                print(f"Infer failed: {e}")

        elif cmd == "stats":
            print(json.dumps(client.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
