#!/usr/bin/env python3
"""
jarvis_prompt_router — Route prompts to optimal models based on task classification
Complexity scoring, domain detection, cost-aware routing, fallback chains
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("jarvis.prompt_router")


class TaskDomain(str, Enum):
    CODE = "code"
    MATH = "math"
    REASONING = "reasoning"
    CREATIVE = "creative"
    FACTUAL = "factual"
    CONVERSATION = "conversation"
    TRADING = "trading"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ComplexityLevel(str, Enum):
    SIMPLE = "simple"  # short, single-turn, low reasoning
    MEDIUM = "medium"  # multi-step, some context
    COMPLEX = "complex"  # deep reasoning, long context
    EXPERT = "expert"  # requires best model


@dataclass
class ModelProfile:
    name: str
    node: str
    endpoint: str
    domains: list[TaskDomain]  # domains this model excels at
    max_complexity: ComplexityLevel
    context_window: int = 8192  # tokens
    tokens_per_second: float = 50.0
    cost_per_1k_tokens: float = 0.0  # 0 = free (local)
    available: bool = True
    priority: int = 50  # lower = preferred


@dataclass
class RoutingDecision:
    model: ModelProfile
    domain: TaskDomain
    complexity: ComplexityLevel
    confidence: float
    reason: str
    fallback_chain: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "model": self.model.name,
            "node": self.model.node,
            "endpoint": self.model.endpoint,
            "domain": self.domain.value,
            "complexity": self.complexity.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "fallback_chain": self.fallback_chain,
        }


# --- Domain detection ---

_DOMAIN_PATTERNS: dict[TaskDomain, list[str]] = {
    TaskDomain.CODE: [
        r"\b(code|function|class|def |import |bug|error|exception|debug|compile|syntax|"
        r"python|javascript|typescript|rust|golang|bash|shell|script|api|endpoint|"
        r"database|sql|query|regex|algorithm|implement|refactor)\b",
    ],
    TaskDomain.MATH: [
        r"\b(calculate|compute|equation|formula|integral|derivative|matrix|"
        r"probability|statistics|algebra|geometry|proof|theorem|solve|result)\b",
        r"[\d+\-*/=^()]+",
    ],
    TaskDomain.REASONING: [
        r"\b(why|because|therefore|hence|implication|consequence|cause|effect|"
        r"analyze|evaluate|compare|contrast|argument|logic|deduce|infer|"
        r"plan|strategy|decision|tradeoff|pros|cons)\b",
    ],
    TaskDomain.CREATIVE: [
        r"\b(write|story|poem|creative|fiction|imagine|describe|narrative|"
        r"character|plot|scene|dialogue|essay|blog|content|post|caption)\b",
    ],
    TaskDomain.TRADING: [
        r"\b(trade|trading|crypto|bitcoin|btc|eth|futures|spot|margin|"
        r"rsi|macd|bollinger|signal|buy|sell|market|price|candle|chart|"
        r"backtest|strategy|pnl|profit|loss|position|leverage|liquidation)\b",
    ],
    TaskDomain.SYSTEM: [
        r"\b(server|cluster|node|gpu|vram|cpu|memory|ram|disk|process|"
        r"service|systemd|docker|container|deploy|restart|health|monitor|"
        r"log|metric|alert|incident|performance|latency|throughput)\b",
    ],
    TaskDomain.FACTUAL: [
        r"\b(what is|who is|when did|where is|how many|history|fact|"
        r"explain|definition|meaning|describe|overview|summary)\b",
    ],
    TaskDomain.CONVERSATION: [
        r"\b(hello|hi|hey|thanks|thank you|please|sorry|help me|"
        r"can you|could you|would you|I want|I need|I have a)\b",
    ],
}


def _detect_domain(prompt: str) -> tuple[TaskDomain, float]:
    """Returns (domain, confidence)."""
    scores: dict[TaskDomain, int] = {}
    lower = prompt.lower()

    for domain, patterns in _DOMAIN_PATTERNS.items():
        total = 0
        for pat in patterns:
            matches = re.findall(pat, lower)
            total += len(matches)
        if total > 0:
            scores[domain] = total

    if not scores:
        return TaskDomain.UNKNOWN, 0.3

    best = max(scores, key=lambda d: scores[d])
    total_matches = sum(scores.values())
    confidence = scores[best] / max(total_matches, 1)
    return best, min(0.95, confidence + 0.2)


# --- Complexity scoring ---

_CODE_COMPLEXITY_SIGNALS = re.compile(
    r"\b(optimize|implement|architect|design|refactor|complex|algorithm|"
    r"distributed|concurrent|async|performance|security|scale)\b",
    re.I,
)
_THINK_SIGNALS = re.compile(
    r"\b(step by step|chain of thought|reason through|analyze deeply|"
    r"comprehensive|thorough|detailed|extensive)\b",
    re.I,
)


def _score_complexity(prompt: str) -> tuple[ComplexityLevel, float]:
    """Heuristic complexity detection."""
    word_count = len(prompt.split())
    code_matches = len(_CODE_COMPLEXITY_SIGNALS.findall(prompt))
    think_matches = len(_THINK_SIGNALS.findall(prompt))
    has_code = bool(re.search(r"```|def |class |function ", prompt))
    question_count = prompt.count("?")

    score = 0.0
    score += min(word_count / 200, 1.0) * 2  # length signal
    score += code_matches * 0.5
    score += think_matches * 0.8
    score += 1.0 if has_code else 0.0
    score += min(question_count * 0.3, 1.0)

    if score < 1.0:
        return ComplexityLevel.SIMPLE, 0.85
    elif score < 2.5:
        return ComplexityLevel.MEDIUM, 0.75
    elif score < 4.5:
        return ComplexityLevel.COMPLEX, 0.70
    else:
        return ComplexityLevel.EXPERT, 0.80


_COMPLEXITY_RANK = {
    ComplexityLevel.SIMPLE: 0,
    ComplexityLevel.MEDIUM: 1,
    ComplexityLevel.COMPLEX: 2,
    ComplexityLevel.EXPERT: 3,
}


class PromptRouter:
    def __init__(self):
        self._models: list[ModelProfile] = []
        self._stats: dict[str, int] = {
            "routed": 0,
            "fallbacks": 0,
            "unavailable": 0,
        }
        self._domain_stats: dict[str, int] = {}
        self._model_stats: dict[str, int] = {}

    def register_model(self, profile: ModelProfile):
        self._models.append(profile)
        self._models.sort(key=lambda m: (m.cost_per_1k_tokens, m.priority))

    def set_available(self, model_name: str, available: bool):
        for m in self._models:
            if m.name == model_name:
                m.available = available
                return

    def route(
        self,
        prompt: str,
        force_domain: TaskDomain | None = None,
        force_complexity: ComplexityLevel | None = None,
        max_cost: float | None = None,
        preferred_node: str | None = None,
    ) -> RoutingDecision | None:
        self._stats["routed"] += 1

        domain, domain_conf = (
            _detect_domain(prompt) if not force_domain else (force_domain, 1.0)
        )
        complexity, comp_conf = (
            _score_complexity(prompt)
            if not force_complexity
            else (force_complexity, 1.0)
        )
        confidence = (domain_conf + comp_conf) / 2

        self._domain_stats[domain.value] = self._domain_stats.get(domain.value, 0) + 1

        complexity_rank = _COMPLEXITY_RANK[complexity]

        candidates = [
            m
            for m in self._models
            if m.available
            and _COMPLEXITY_RANK[m.max_complexity] >= complexity_rank
            and (max_cost is None or m.cost_per_1k_tokens <= max_cost)
        ]

        if not candidates:
            self._stats["unavailable"] += 1
            log.warning(
                f"No available model for domain={domain} complexity={complexity}"
            )
            return None

        # Score candidates: prefer domain match, then lowest cost, then priority
        def candidate_score(m: ModelProfile) -> float:
            domain_match = 2.0 if domain in m.domains else 0.0
            node_match = 1.0 if preferred_node and m.node == preferred_node else 0.0
            cost_penalty = m.cost_per_1k_tokens * 0.1
            return domain_match + node_match - cost_penalty - m.priority * 0.01

        best = max(candidates, key=candidate_score)
        fallback = [m.name for m in candidates if m.name != best.name][:3]

        self._model_stats[best.name] = self._model_stats.get(best.name, 0) + 1

        reason_parts = [f"domain={domain.value}", f"complexity={complexity.value}"]
        if domain in best.domains:
            reason_parts.append("domain_match")
        if preferred_node and best.node == preferred_node:
            reason_parts.append("preferred_node")

        log.debug(
            f"Routed → {best.name}@{best.node} "
            f"({domain.value}/{complexity.value} conf={confidence:.2f})"
        )

        return RoutingDecision(
            model=best,
            domain=domain,
            complexity=complexity,
            confidence=confidence,
            reason=", ".join(reason_parts),
            fallback_chain=fallback,
        )

    def route_batch(self, prompts: list[str], **kwargs) -> list[RoutingDecision | None]:
        return [self.route(p, **kwargs) for p in prompts]

    def available_models(self) -> list[str]:
        return [m.name for m in self._models if m.available]

    def stats(self) -> dict:
        return {
            **self._stats,
            "models_registered": len(self._models),
            "models_available": len([m for m in self._models if m.available]),
            "domain_distribution": self._domain_stats,
            "model_distribution": self._model_stats,
        }


def build_jarvis_prompt_router() -> PromptRouter:
    router = PromptRouter()

    router.register_model(
        ModelProfile(
            name="qwen3.5-9b",
            node="m1",
            endpoint="http://192.168.1.85:1234",
            domains=[
                TaskDomain.CONVERSATION,
                TaskDomain.FACTUAL,
                TaskDomain.CODE,
                TaskDomain.SYSTEM,
            ],
            max_complexity=ComplexityLevel.MEDIUM,
            context_window=8192,
            tokens_per_second=80,
            cost_per_1k_tokens=0.0,
            priority=10,
        )
    )

    router.register_model(
        ModelProfile(
            name="qwen3.5-35b",
            node="m1",
            endpoint="http://192.168.1.85:1234",
            domains=[
                TaskDomain.CODE,
                TaskDomain.REASONING,
                TaskDomain.MATH,
                TaskDomain.SYSTEM,
            ],
            max_complexity=ComplexityLevel.COMPLEX,
            context_window=32768,
            tokens_per_second=25,
            cost_per_1k_tokens=0.0,
            priority=20,
        )
    )

    router.register_model(
        ModelProfile(
            name="deepseek-r1",
            node="m2",
            endpoint="http://192.168.1.26:1234",
            domains=[
                TaskDomain.REASONING,
                TaskDomain.MATH,
                TaskDomain.CODE,
                TaskDomain.TRADING,
            ],
            max_complexity=ComplexityLevel.EXPERT,
            context_window=65536,
            tokens_per_second=15,
            cost_per_1k_tokens=0.0,
            priority=30,
        )
    )

    router.register_model(
        ModelProfile(
            name="gemma3:4b",
            node="ol1",
            endpoint="http://127.0.0.1:11434",
            domains=[TaskDomain.CONVERSATION, TaskDomain.FACTUAL],
            max_complexity=ComplexityLevel.SIMPLE,
            context_window=4096,
            tokens_per_second=120,
            cost_per_1k_tokens=0.0,
            priority=5,
        )
    )

    return router


def main():
    import sys

    router = build_jarvis_prompt_router()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Prompt router demo...\n")

        prompts = [
            "Hi, how are you?",
            "What is the capital of France?",
            "Write a Python function to implement quicksort with O(n log n) complexity.",
            "Analyze the market structure of BTC/USDT and suggest entry/exit points.",
            "Reason step by step: why does adding more GPUs not always improve throughput?",
            "Implement a distributed consensus algorithm with fault tolerance for 3 nodes.",
        ]

        for prompt in prompts:
            decision = router.route(prompt)
            if decision:
                print(
                    f"  [{decision.domain.value:12}|{decision.complexity.value:8}] "
                    f"→ {decision.model.name:<20} "
                    f"conf={decision.confidence:.2f} | {decision.reason}"
                )
                if decision.fallback_chain:
                    print(f"    fallback: {decision.fallback_chain}")
            else:
                print(f"  No route found for: {prompt[:50]}")

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    main()
