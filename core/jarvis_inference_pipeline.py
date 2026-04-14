#!/usr/bin/env python3
"""
jarvis_inference_pipeline — End-to-end LLM inference pipeline
Pre-processing, routing, caching, post-processing, streaming, observability
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable

log = logging.getLogger("jarvis.inference_pipeline")


class PipelineStage(str, Enum):
    VALIDATE = "validate"
    PREPROCESS = "preprocess"
    ROUTE = "route"
    CACHE_LOOKUP = "cache_lookup"
    INFER = "infer"
    POSTPROCESS = "postprocess"
    CACHE_STORE = "cache_store"
    EMIT = "emit"


class InferenceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    CACHED = "cached"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class InferenceRequest:
    request_id: str
    prompt: str
    model: str = ""  # "" = auto-route
    system_prompt: str = ""
    max_tokens: int = 512
    temperature: float = 0.7
    stream: bool = False
    session_id: str = ""
    user_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class InferenceResponse:
    request_id: str
    text: str
    model: str
    node: str = ""
    status: InferenceStatus = InferenceStatus.DONE
    tokens_prompt: int = 0
    tokens_output: int = 0
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    cached: bool = False
    stage_timings: dict[str, float] = field(default_factory=dict)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tps(self) -> float:
        dur = max((self.total_ms - self.ttft_ms) / 1000, 0.001)
        return self.tokens_output / dur

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "model": self.model,
            "node": self.node,
            "status": self.status.value,
            "tokens_prompt": self.tokens_prompt,
            "tokens_output": self.tokens_output,
            "ttft_ms": round(self.ttft_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "tps": round(self.tps, 2),
            "cached": self.cached,
            "stage_timings": {k: round(v, 2) for k, v in self.stage_timings.items()},
            "error": self.error,
        }


# --- Stage handler type ---
# fn(request, response, context) -> (response, context)
StageHandler = Callable[
    [InferenceRequest, InferenceResponse, dict], tuple[InferenceResponse, dict]
]


class PipelineStageHandler:
    def __init__(self, stage: PipelineStage, fn: StageHandler, enabled: bool = True):
        self.stage = stage
        self.fn = fn
        self.enabled = enabled
        self.call_count = 0
        self.total_ms = 0.0

    async def run(
        self,
        request: InferenceRequest,
        response: InferenceResponse,
        ctx: dict,
    ) -> tuple[InferenceResponse, dict]:
        t0 = time.time()
        self.call_count += 1
        if asyncio.iscoroutinefunction(self.fn):
            response, ctx = await self.fn(request, response, ctx)
        else:
            response, ctx = self.fn(request, response, ctx)
        dur = (time.time() - t0) * 1000
        self.total_ms += dur
        response.stage_timings[self.stage.value] = dur
        return response, ctx


# --- Default stage implementations ---


def _validate_stage(
    req: InferenceRequest,
    resp: InferenceResponse,
    ctx: dict,
) -> tuple[InferenceResponse, dict]:
    if not req.prompt.strip():
        resp.status = InferenceStatus.FAILED
        resp.error = "empty prompt"
        ctx["abort"] = True
    if req.max_tokens < 1:
        req.max_tokens = 512
    if not (0.0 <= req.temperature <= 2.0):
        req.temperature = 0.7
    return resp, ctx


def _preprocess_stage(
    req: InferenceRequest,
    resp: InferenceResponse,
    ctx: dict,
) -> tuple[InferenceResponse, dict]:
    import re

    # Strip <think> blocks from prompt
    req.prompt = re.sub(r"<think>.*?</think>", "", req.prompt, flags=re.DOTALL).strip()
    # Truncate overlong prompts (rough estimate)
    max_chars = 32000
    if len(req.prompt) > max_chars:
        req.prompt = req.prompt[-max_chars:]
        ctx["truncated"] = True
    return resp, ctx


async def _mock_infer_stage(
    req: InferenceRequest,
    resp: InferenceResponse,
    ctx: dict,
) -> tuple[InferenceResponse, dict]:
    """Mock inference: returns a canned response for testing."""
    await asyncio.sleep(0.02)  # simulate ~20ms
    model = ctx.get("routed_model", req.model or "qwen3.5-9b")
    node = ctx.get("routed_node", "m1")
    resp.text = f"[{model}@{node}] Response to: {req.prompt[:60]}"
    resp.model = model
    resp.node = node
    resp.tokens_prompt = max(1, len(req.prompt.split()))
    resp.tokens_output = 20
    resp.ttft_ms = 15.0
    resp.status = InferenceStatus.DONE
    return resp, ctx


def _postprocess_stage(
    req: InferenceRequest,
    resp: InferenceResponse,
    ctx: dict,
) -> tuple[InferenceResponse, dict]:
    import re

    if resp.text:
        # Strip think tags from response
        resp.text = re.sub(
            r"<think>.*?</think>", "", resp.text, flags=re.DOTALL
        ).strip()
    return resp, ctx


class InferencePipeline:
    def __init__(self, timeout_s: float = 30.0):
        self._stages: list[PipelineStageHandler] = []
        self._timeout = timeout_s
        self._middleware: list[Callable] = []
        self._stats: dict[str, int | float] = {
            "requests": 0,
            "success": 0,
            "cached": 0,
            "failed": 0,
            "timeout": 0,
            "total_tokens_out": 0,
        }
        self._history: list[InferenceResponse] = []
        self._max_history = 2000
        self._build_default_pipeline()

    def _build_default_pipeline(self):
        self.add_stage(PipelineStage.VALIDATE, _validate_stage)
        self.add_stage(PipelineStage.PREPROCESS, _preprocess_stage)
        self.add_stage(PipelineStage.INFER, _mock_infer_stage)
        self.add_stage(PipelineStage.POSTPROCESS, _postprocess_stage)

    def add_stage(
        self,
        stage: PipelineStage,
        fn: StageHandler,
        enabled: bool = True,
        replace: bool = False,
    ):
        if replace:
            self._stages = [s for s in self._stages if s.stage != stage]
        self._stages.append(PipelineStageHandler(stage, fn, enabled))

    def remove_stage(self, stage: PipelineStage):
        self._stages = [s for s in self._stages if s.stage != stage]

    def enable_stage(self, stage: PipelineStage, enabled: bool = True):
        for s in self._stages:
            if s.stage == stage:
                s.enabled = enabled

    def replace_infer(self, fn: StageHandler):
        """Replace the inference stage with a real LLM backend."""
        self.add_stage(PipelineStage.INFER, fn, replace=True)

    async def run(self, request: InferenceRequest) -> InferenceResponse:
        t0 = time.time()
        self._stats["requests"] += 1

        resp = InferenceResponse(
            request_id=request.request_id,
            text="",
            model=request.model,
            status=InferenceStatus.RUNNING,
        )
        ctx: dict[str, Any] = {"request_id": request.request_id}

        try:
            coro = self._run_stages(request, resp, ctx)
            resp, ctx = await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.TimeoutError:
            resp.status = InferenceStatus.TIMEOUT
            resp.error = f"timeout after {self._timeout}s"
            self._stats["timeout"] += 1
        except Exception as e:
            resp.status = InferenceStatus.FAILED
            resp.error = str(e)
            self._stats["failed"] += 1
            log.error(f"Pipeline error [{request.request_id}]: {e}")

        resp.total_ms = (time.time() - t0) * 1000

        if resp.status == InferenceStatus.DONE:
            self._stats["success"] += 1
            self._stats["total_tokens_out"] += resp.tokens_output
        elif resp.status == InferenceStatus.CACHED:
            self._stats["cached"] += 1

        self._record(resp)
        return resp

    async def _run_stages(
        self,
        request: InferenceRequest,
        resp: InferenceResponse,
        ctx: dict,
    ) -> tuple[InferenceResponse, dict]:
        for handler in self._stages:
            if not handler.enabled:
                continue
            if ctx.get("abort"):
                break
            resp, ctx = await handler.run(request, resp, ctx)
        return resp, ctx

    async def run_many(
        self, requests: list[InferenceRequest]
    ) -> list[InferenceResponse]:
        tasks = [self.run(req) for req in requests]
        return list(await asyncio.gather(*tasks))

    async def stream(self, request: InferenceRequest) -> AsyncGenerator[str, None]:
        """Yield tokens one by one (mock: splits response by words)."""
        resp = await self.run(request)
        if resp.status != InferenceStatus.DONE:
            yield f"[ERROR] {resp.error}"
            return
        for word in resp.text.split():
            yield word + " "
            await asyncio.sleep(0.005)

    def _record(self, resp: InferenceResponse):
        self._history.append(resp)
        if len(self._history) > self._max_history:
            self._history.pop(0)

    def recent(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stage_stats(self) -> list[dict]:
        return [
            {
                "stage": h.stage.value,
                "enabled": h.enabled,
                "calls": h.call_count,
                "avg_ms": round(h.total_ms / max(h.call_count, 1), 2),
            }
            for h in self._stages
        ]

    def stats(self) -> dict:
        total = max(self._stats["requests"], 1)
        return {
            **self._stats,
            "success_rate": round(self._stats["success"] / total, 4),
            "cache_rate": round(self._stats["cached"] / total, 4),
            "stages": len(self._stages),
        }


def build_jarvis_inference_pipeline(timeout_s: float = 30.0) -> InferencePipeline:
    return InferencePipeline(timeout_s=timeout_s)


async def main():
    import sys
    import secrets

    pipeline = build_jarvis_inference_pipeline()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Inference pipeline demo...\n")

        requests = [
            InferenceRequest(
                request_id=secrets.token_hex(4),
                prompt="What GPU models are available on M1?",
                model="qwen3.5-9b",
                max_tokens=256,
            ),
            InferenceRequest(
                request_id=secrets.token_hex(4),
                prompt="",  # invalid — should fail validation
                model="qwen3.5-9b",
            ),
            InferenceRequest(
                request_id=secrets.token_hex(4),
                prompt="<think>reasoning</think>Analyze BTC price action.",
                model="deepseek-r1",
                max_tokens=512,
            ),
        ]

        responses = await pipeline.run_many(requests)
        for resp in responses:
            icon = "✅" if resp.status == InferenceStatus.DONE else "❌"
            print(
                f"  {icon} [{resp.request_id}] {resp.status.value:<8} "
                f"model={resp.model:<15} "
                f"total={resp.total_ms:.0f}ms tokens={resp.tokens_output}"
            )
            if resp.error:
                print(f"     error: {resp.error}")
            else:
                print(f"     {resp.text[:80]}")

        # Streaming demo
        req = InferenceRequest(
            request_id=secrets.token_hex(4),
            prompt="List three GPU models briefly.",
            stream=True,
        )
        print("\n  Streaming: ", end="", flush=True)
        async for token in pipeline.stream(req):
            print(token, end="", flush=True)
        print()

        print("\n  Stage stats:")
        for s in pipeline.stage_stats():
            print(f"    {s['stage']:<15} calls={s['calls']} avg={s['avg_ms']}ms")

        print(f"\nStats: {json.dumps(pipeline.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(pipeline.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
