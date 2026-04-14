#!/usr/bin/env python3
"""
jarvis_response_builder — Structured response construction with templates
Builds consistent API responses with metadata, pagination, and error formatting
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.response_builder")


class ResponseStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    PARTIAL = "partial"
    PENDING = "pending"
    CACHED = "cached"


@dataclass
class Pagination:
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_prev": self.has_prev,
        }


@dataclass
class APIResponse:
    status: ResponseStatus
    data: Any = None
    error: str = ""
    error_code: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    duration_ms: float = 0.0
    cached: bool = False
    model: str = ""
    node: str = ""
    pagination: Pagination | None = None
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "status": self.status.value,
            "request_id": self.request_id,
            "ts": self.ts,
        }
        if self.data is not None:
            d["data"] = self.data
        if self.error:
            d["error"] = {"message": self.error, "code": self.error_code}
        if self.duration_ms:
            d["duration_ms"] = round(self.duration_ms, 1)
        if self.cached:
            d["cached"] = True
        if self.model:
            d["model"] = self.model
        if self.node:
            d["node"] = self.node
        if self.pagination:
            d["pagination"] = self.pagination.to_dict()
        if self.meta:
            d["meta"] = self.meta
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @property
    def ok(self) -> bool:
        return self.status == ResponseStatus.OK

    def http_status(self) -> int:
        mapping = {
            ResponseStatus.OK: 200,
            ResponseStatus.PARTIAL: 206,
            ResponseStatus.PENDING: 202,
            ResponseStatus.CACHED: 200,
            ResponseStatus.ERROR: 500,
        }
        return mapping.get(self.status, 200)


class ResponseBuilder:
    def __init__(self, service: str = "jarvis"):
        self.service = service
        self._templates: dict[str, dict] = {}
        self._stats: dict[str, int] = {"built": 0, "errors": 0, "cached": 0}

    def register_template(self, name: str, meta: dict):
        self._templates[name] = meta

    def ok(
        self,
        data: Any,
        duration_ms: float = 0.0,
        model: str = "",
        node: str = "",
        cached: bool = False,
        request_id: str | None = None,
        meta: dict | None = None,
    ) -> APIResponse:
        self._stats["built"] += 1
        if cached:
            self._stats["cached"] += 1
        return APIResponse(
            status=ResponseStatus.CACHED if cached else ResponseStatus.OK,
            data=data,
            duration_ms=duration_ms,
            model=model,
            node=node,
            cached=cached,
            request_id=request_id or str(uuid.uuid4())[:10],
            meta=meta or {},
        )

    def error(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        http_status: int = 500,
        request_id: str | None = None,
        meta: dict | None = None,
    ) -> APIResponse:
        self._stats["built"] += 1
        self._stats["errors"] += 1
        resp = APIResponse(
            status=ResponseStatus.ERROR,
            error=message,
            error_code=code,
            request_id=request_id or str(uuid.uuid4())[:10],
            meta={"http_status": http_status, **(meta or {})},
        )
        return resp

    def paginated(
        self,
        items: list,
        page: int,
        page_size: int,
        total: int,
        **kwargs,
    ) -> APIResponse:
        start = (page - 1) * page_size
        end = start + page_size
        page_data = items[start:end] if items else []
        resp = self.ok(page_data, **kwargs)
        resp.pagination = Pagination(page=page, page_size=page_size, total=total)
        return resp

    def pending(self, task_id: str, estimated_ms: float = 0.0) -> APIResponse:
        self._stats["built"] += 1
        return APIResponse(
            status=ResponseStatus.PENDING,
            data={"task_id": task_id},
            meta={"estimated_ms": estimated_ms},
        )

    def partial(self, data: Any, missing: list[str] | None = None) -> APIResponse:
        self._stats["built"] += 1
        return APIResponse(
            status=ResponseStatus.PARTIAL,
            data=data,
            meta={"missing": missing or []},
        )

    def from_template(self, name: str, data: Any, **kwargs) -> APIResponse:
        tmpl = self._templates.get(name, {})
        return self.ok(data, meta=tmpl, **kwargs)

    def infer_response(
        self,
        content: str,
        model: str,
        node: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
        cached: bool = False,
        request_id: str | None = None,
    ) -> APIResponse:
        """Build standard LLM inference response."""
        return self.ok(
            data={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
            duration_ms=duration_ms,
            model=model,
            node=node,
            cached=cached,
            request_id=request_id,
        )

    def error_map(self, exc: Exception, request_id: str | None = None) -> APIResponse:
        """Map common exceptions to structured errors."""
        exc_type = type(exc).__name__
        mapping = {
            "ValueError": ("INVALID_INPUT", 400),
            "KeyError": ("NOT_FOUND", 404),
            "PermissionError": ("FORBIDDEN", 403),
            "TimeoutError": ("TIMEOUT", 504),
            "ConnectionError": ("UPSTREAM_ERROR", 502),
            "asyncio.TimeoutError": ("TIMEOUT", 504),
        }
        code, http = mapping.get(exc_type, ("INTERNAL_ERROR", 500))
        return self.error(
            str(exc)[:500], code=code, http_status=http, request_id=request_id
        )

    def stats(self) -> dict:
        return {
            **self._stats,
            "templates": len(self._templates),
            "cache_rate": round(
                self._stats["cached"] / max(self._stats["built"], 1) * 100, 1
            ),
            "error_rate": round(
                self._stats["errors"] / max(self._stats["built"], 1) * 100, 1
            ),
        }


async def main():
    import sys

    builder = ResponseBuilder()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Normal response
        resp = builder.ok(
            {"answer": "Paris"}, duration_ms=142.3, model="qwen3.5-9b", node="m1"
        )
        print(f"OK: {resp.to_json()[:200]}")

        # Error response
        resp = builder.error(
            "Model unavailable", code="MODEL_UNAVAILABLE", http_status=503
        )
        print(f"Error: {resp.to_json()}")

        # Paginated
        items = list(range(100))
        resp = builder.paginated(
            items, page=2, page_size=10, total=100, duration_ms=5.0
        )
        print(f"Paginated p2: {json.dumps(resp.to_dict()['pagination'])}")

        # Inference
        resp = builder.infer_response(
            "The capital of France is Paris.",
            model="qwen3.5-9b",
            node="m1",
            input_tokens=15,
            output_tokens=8,
            duration_ms=220.0,
        )
        print(
            f"Infer: http={resp.http_status()} tokens={resp.data['usage']['total_tokens']}"
        )

        # Error map
        resp = builder.error_map(TimeoutError("Request timed out after 30s"))
        print(f"Mapped: code={resp.error_code} http={resp.meta.get('http_status')}")

        print(f"\nStats: {json.dumps(builder.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(builder.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
