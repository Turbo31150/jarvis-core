#!/usr/bin/env python3
"""
jarvis_cost_estimator — Pre-flight cost estimation for LLM requests
Estimates tokens, latency, and cost before executing requests
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cost_estimator")

REDIS_PREFIX = "jarvis:cost:"

CHARS_PER_TOKEN = 4

# Cost per 1M tokens USD
COST_TABLE: dict[str, dict] = {
    "qwen/qwen3.5-9b": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 25},
    "qwen/qwen3.5-27b-claude": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 65},
    "qwen/qwen3.5-35b-a3b": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 90},
    "deepseek-r1-0528": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 150},
    "glm-4.7-flash-claude": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 15},
    "claude-opus-4": {"input": 15.0, "output": 75.0, "latency_ms_per_tok": 50},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "latency_ms_per_tok": 30},
    "claude-haiku-4": {"input": 0.8, "output": 4.0, "latency_ms_per_tok": 15},
    "gpt-4o": {"input": 5.0, "output": 15.0, "latency_ms_per_tok": 35},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6, "latency_ms_per_tok": 20},
    "default": {"input": 0.0, "output": 0.0, "latency_ms_per_tok": 50},
}


def _count_tokens(text: str) -> int:
    """Rough token estimate using char/4 heuristic + adjustments."""
    if not text:
        return 0
    # Code has denser tokenization
    code_blocks = len(re.findall(r"```[\s\S]*?```", text))
    base = len(text) // CHARS_PER_TOKEN
    return int(base * (0.85 if code_blocks else 1.0)) + 4  # +4 for message overhead


def _count_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _count_tokens(part.get("text", ""))
        else:
            total += _count_tokens(str(content))
        total += 4  # role + separators overhead
    return total + 3  # conversation framing


@dataclass
class CostEstimate:
    model: str
    input_tokens: int
    output_tokens_est: int
    input_cost_usd: float
    output_cost_usd_est: float
    total_cost_usd_est: float
    latency_ms_est: float
    fits_in_context: bool
    context_limit: int
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens_est(self) -> int:
        return self.input_tokens + self.output_tokens_est

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens_est": self.output_tokens_est,
            "total_tokens_est": self.total_tokens_est,
            "input_cost_usd": round(self.input_cost_usd, 6),
            "output_cost_usd_est": round(self.output_cost_usd_est, 6),
            "total_cost_usd_est": round(self.total_cost_usd_est, 6),
            "latency_ms_est": round(self.latency_ms_est, 0),
            "fits_in_context": self.fits_in_context,
            "warnings": self.warnings,
        }


# Context limits per model
CONTEXT_LIMITS: dict[str, int] = {
    "qwen/qwen3.5-9b": 32768,
    "qwen/qwen3.5-27b-claude": 32768,
    "qwen/qwen3.5-35b-a3b": 32768,
    "deepseek-r1-0528": 65536,
    "glm-4.7-flash-claude": 8192,
    "claude-opus-4": 200000,
    "claude-sonnet-4": 200000,
    "claude-haiku-4": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "default": 8192,
}


class CostEstimator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._history: list[dict] = []
        self._model_corrections: dict[str, float] = {}  # learned correction factors

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _get_costs(self, model: str) -> dict:
        for key, costs in COST_TABLE.items():
            if key.lower() in model.lower() or model.lower() in key.lower():
                return costs
        return COST_TABLE["default"]

    def _get_context_limit(self, model: str) -> int:
        for key, limit in CONTEXT_LIMITS.items():
            if key.lower() in model.lower() or model.lower() in key.lower():
                return limit
        return CONTEXT_LIMITS["default"]

    def estimate(
        self,
        model: str,
        messages: list[dict] | None = None,
        prompt: str = "",
        max_tokens: int = 512,
        system: str = "",
    ) -> CostEstimate:
        costs = self._get_costs(model)
        context_limit = self._get_context_limit(model)
        warnings = []

        # Count input tokens
        if messages:
            input_tokens = _count_messages_tokens(messages)
        else:
            input_tokens = _count_tokens(prompt)
            if system:
                input_tokens += _count_tokens(system) + 4

        # Apply learned correction if available
        correction = self._model_corrections.get(model, 1.0)
        input_tokens = int(input_tokens * correction)

        # Estimate output tokens (assume 80% of max_tokens on average)
        output_tokens_est = int(max_tokens * 0.8)

        # Cost calculation
        input_cost = input_tokens * costs["input"] / 1_000_000
        output_cost_est = output_tokens_est * costs["output"] / 1_000_000

        # Latency estimate: base 200ms + per-token latency
        latency_base_ms = 200.0
        latency_est = latency_base_ms + output_tokens_est * costs["latency_ms_per_tok"]

        # Context check
        total_input = input_tokens + output_tokens_est
        fits = total_input <= context_limit

        # Warnings
        if not fits:
            warnings.append(f"Exceeds context: {total_input} > {context_limit} tokens")
        if input_tokens > context_limit * 0.8:
            warnings.append(
                f"Input uses {input_tokens / context_limit * 100:.0f}% of context"
            )
        if latency_est > 30000:
            warnings.append(f"High latency estimate: {latency_est / 1000:.1f}s")

        return CostEstimate(
            model=model,
            input_tokens=input_tokens,
            output_tokens_est=output_tokens_est,
            input_cost_usd=input_cost,
            output_cost_usd_est=output_cost_est,
            total_cost_usd_est=input_cost + output_cost_est,
            latency_ms_est=latency_est,
            fits_in_context=fits,
            context_limit=context_limit,
            warnings=warnings,
        )

    def compare_models(
        self,
        models: list[str],
        messages: list[dict] | None = None,
        prompt: str = "",
        max_tokens: int = 512,
    ) -> list[dict]:
        estimates = []
        for model in models:
            est = self.estimate(
                model, messages=messages, prompt=prompt, max_tokens=max_tokens
            )
            estimates.append({"model": model, **est.to_dict()})
        return sorted(estimates, key=lambda x: x["latency_ms_est"])

    def learn(self, model: str, estimated_tokens: int, actual_tokens: int):
        """Update correction factor based on observed vs estimated tokens."""
        if estimated_tokens <= 0 or actual_tokens <= 0:
            return
        alpha = 0.2
        current = self._model_corrections.get(model, 1.0)
        correction = actual_tokens / estimated_tokens
        self._model_corrections[model] = alpha * correction + (1 - alpha) * current

    def cheapest_for_budget(
        self,
        max_cost_usd: float,
        messages: list[dict] | None = None,
        prompt: str = "",
        max_tokens: int = 512,
    ) -> list[str]:
        models = list(COST_TABLE.keys())
        models.remove("default")
        return [
            m
            for m in models
            if self.estimate(
                m, messages=messages, prompt=prompt, max_tokens=max_tokens
            ).total_cost_usd_est
            <= max_cost_usd
        ]

    def stats(self) -> dict:
        return {
            "models_in_table": len(COST_TABLE) - 1,
            "learned_corrections": len(self._model_corrections),
            "corrections": {m: round(c, 3) for m, c in self._model_corrections.items()},
        }


async def main():
    import sys

    estimator = CostEstimator()
    await estimator.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "system", "content": "You are JARVIS, an advanced AI assistant."},
            {
                "role": "user",
                "content": "Explain how Redis Cluster achieves high availability with automatic failover. Include details about master-replica replication, sentinel vs cluster modes, and hash slot redistribution.",
            },
        ]
        models = [
            "qwen/qwen3.5-9b",
            "qwen/qwen3.5-27b-claude",
            "deepseek-r1-0528",
            "claude-sonnet-4",
            "gpt-4o-mini",
        ]

        print(
            f"{'Model':<35} {'Input':>8} {'Out~':>6} {'Cost~$':>10} {'Latency~ms':>12} {'Fits'}"
        )
        print("-" * 80)
        for est_dict in estimator.compare_models(
            models, messages=messages, max_tokens=1024
        ):
            fits = "✅" if est_dict["fits_in_context"] else "❌"
            cost = est_dict["total_cost_usd_est"]
            cost_str = f"${cost:.4f}" if cost > 0 else "free"
            print(
                f"  {est_dict['model']:<35} {est_dict['input_tokens']:>8} {est_dict['output_tokens_est']:>6} {cost_str:>10} {est_dict['latency_ms_est']:>11.0f}ms {fits}"
            )

    elif cmd == "stats":
        print(json.dumps(estimator.stats(), indent=2))

    elif cmd == "estimate" and len(sys.argv) > 3:
        model = sys.argv[2]
        prompt = " ".join(sys.argv[3:])
        est = estimator.estimate(model, prompt=prompt)
        print(json.dumps(est.to_dict(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
