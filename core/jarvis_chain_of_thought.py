#!/usr/bin/env python3
"""
jarvis_chain_of_thought — Chain-of-thought reasoning orchestrator
Step decomposition, intermediate reasoning tracking, self-correction loops
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger("jarvis.chain_of_thought")


class StepKind(str, Enum):
    DECOMPOSE = "decompose"  # break problem into sub-tasks
    REASON = "reason"  # apply reasoning to a sub-task
    VERIFY = "verify"  # check intermediate result
    SYNTHESIZE = "synthesize"  # combine results
    CORRECT = "correct"  # self-correction pass
    CONCLUDE = "conclude"  # final answer


class ReasoningStrategy(str, Enum):
    LINEAR = "linear"  # step-by-step sequential
    TREE = "tree"  # branch on alternatives
    SCRATCHPAD = "scratchpad"  # free-form reasoning then extract
    SELF_ASK = "self_ask"  # generate sub-questions, answer each


@dataclass
class ThoughtStep:
    step_id: str
    kind: StepKind
    content: str
    result: str = ""
    confidence: float = 0.0  # 0–1
    duration_ms: float = 0.0
    verified: bool = False
    children: list[str] = field(default_factory=list)  # child step_ids
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "kind": self.kind.value,
            "content": self.content[:200],
            "result": self.result[:200] if self.result else "",
            "confidence": round(self.confidence, 3),
            "duration_ms": round(self.duration_ms, 2),
            "verified": self.verified,
        }


@dataclass
class ReasoningTrace:
    trace_id: str
    problem: str
    strategy: ReasoningStrategy
    steps: list[ThoughtStep] = field(default_factory=list)
    final_answer: str = ""
    total_tokens: int = 0
    duration_ms: float = 0.0
    self_corrections: int = 0
    confidence: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "problem": self.problem[:200],
            "strategy": self.strategy.value,
            "steps": [s.to_dict() for s in self.steps],
            "final_answer": self.final_answer[:500],
            "total_tokens": self.total_tokens,
            "duration_ms": round(self.duration_ms, 2),
            "self_corrections": self.self_corrections,
            "confidence": round(self.confidence, 3),
            "step_count": len(self.steps),
        }


def _extract_think_blocks(text: str) -> tuple[str, str]:
    """Extract <think>...</think> from response, return (thinking, answer)."""
    think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    if think_match:
        thinking = think_match.group(1).strip()
        answer = text[think_match.end() :].strip()
        return thinking, answer
    return "", text.strip()


def _extract_steps(text: str) -> list[str]:
    """Extract numbered steps from text."""
    steps = re.findall(
        r"(?:^|\n)\s*(?:\d+[\.\):]|\*|-)\s+(.+?)(?=\n\s*(?:\d+[\.\):]|\*|-)\s|$)",
        text,
        re.DOTALL,
    )
    return [s.strip() for s in steps if s.strip()]


def _estimate_confidence(text: str) -> float:
    """Heuristic confidence from linguistic markers."""
    high_markers = ["therefore", "clearly", "it follows", "obviously", "thus", "hence"]
    low_markers = [
        "maybe",
        "perhaps",
        "i think",
        "possibly",
        "not sure",
        "uncertain",
        "might",
    ]
    text_lower = text.lower()
    high = sum(1 for m in high_markers if m in text_lower)
    low = sum(1 for m in low_markers if m in text_lower)
    base = 0.7
    score = base + high * 0.05 - low * 0.08
    return max(0.1, min(1.0, score))


class ChainOfThought:
    def __init__(self, llm_fn: Callable | None = None):
        """
        llm_fn: async (prompt: str) -> str — your LLM call.
        If None, uses a mock that returns structured placeholder responses.
        """
        self._llm = llm_fn or self._mock_llm
        self._traces: list[ReasoningTrace] = []
        self._max_correction_loops = 2
        self._stats: dict[str, int] = {
            "traces": 0,
            "steps_total": 0,
            "self_corrections": 0,
            "verifications_failed": 0,
        }

    async def _mock_llm(self, prompt: str) -> str:
        await asyncio.sleep(0.01)
        if "decompose" in prompt.lower() or "break" in prompt.lower():
            return "1. Understand the problem\n2. Identify key components\n3. Apply reasoning\n4. Verify the result"
        if "verify" in prompt.lower() or "check" in prompt.lower():
            return "The reasoning appears correct. Confidence: high."
        if "self-ask" in prompt.lower():
            return "Q: What is the core question?\nA: We need to determine the answer.\nQ: What approach works best?\nA: Step-by-step analysis."
        return f"After careful analysis of the problem, the answer is: [reasoned response to: {prompt[:80]}]"

    async def _step(
        self,
        trace: ReasoningTrace,
        kind: StepKind,
        content: str,
        prompt: str,
    ) -> ThoughtStep:
        import secrets

        step_id = secrets.token_hex(4)
        t0 = time.time()
        raw = await self._llm(prompt)
        dur = (time.time() - t0) * 1000
        thinking, result = _extract_think_blocks(raw)
        confidence = _estimate_confidence(result or raw)

        step = ThoughtStep(
            step_id=step_id,
            kind=kind,
            content=content,
            result=result or raw,
            confidence=confidence,
            duration_ms=dur,
        )
        trace.steps.append(step)
        trace.total_tokens += len((prompt + raw).split())
        self._stats["steps_total"] += 1
        return step

    async def reason_linear(self, problem: str) -> ReasoningTrace:
        """Sequential chain-of-thought: decompose → reason each step → synthesize."""
        import secrets

        trace = ReasoningTrace(
            trace_id=secrets.token_hex(8),
            problem=problem,
            strategy=ReasoningStrategy.LINEAR,
        )
        t0 = time.time()

        # Step 1: Decompose
        decompose_step = await self._step(
            trace,
            StepKind.DECOMPOSE,
            "Break problem into steps",
            f"Break this problem into clear numbered steps:\n{problem}",
        )
        sub_steps = _extract_steps(decompose_step.result) or [decompose_step.result]

        # Step 2: Reason each sub-step
        for i, sub in enumerate(sub_steps[:5]):  # max 5 sub-steps
            await self._step(
                trace,
                StepKind.REASON,
                f"Step {i + 1}: {sub}",
                f"Problem: {problem}\n\nStep {i + 1}: {sub}\n\nReason through this step carefully.",
            )

        # Step 3: Verify
        verify_step = await self._step(
            trace,
            StepKind.VERIFY,
            "Verify reasoning",
            f"Problem: {problem}\n\nSteps taken:\n"
            + "\n".join(s.result[:100] for s in trace.steps[-len(sub_steps) :])
            + "\n\nVerify this reasoning is correct and complete.",
        )

        # Self-correction if confidence is low
        corrections = 0
        if verify_step.confidence < 0.5 and corrections < self._max_correction_loops:
            corrections += 1
            trace.self_corrections += 1
            self._stats["self_corrections"] += 1
            await self._step(
                trace,
                StepKind.CORRECT,
                "Self-correction",
                f"Problem: {problem}\n\nPrevious reasoning had issues: {verify_step.result}\n\nProvide corrected reasoning.",
            )

        # Step 4: Synthesize
        synthesis_step = await self._step(
            trace,
            StepKind.SYNTHESIZE,
            "Synthesize final answer",
            f"Problem: {problem}\n\nBased on the reasoning steps above, provide the final clear answer.",
        )

        trace.final_answer = synthesis_step.result
        trace.confidence = synthesis_step.confidence
        trace.duration_ms = (time.time() - t0) * 1000
        self._traces.append(trace)
        self._stats["traces"] += 1
        return trace

    async def reason_scratchpad(self, problem: str) -> ReasoningTrace:
        """Scratchpad: free-form thinking then extract clean answer."""
        import secrets

        trace = ReasoningTrace(
            trace_id=secrets.token_hex(8),
            problem=problem,
            strategy=ReasoningStrategy.SCRATCHPAD,
        )
        t0 = time.time()

        scratch_step = await self._step(
            trace,
            StepKind.REASON,
            "Scratchpad reasoning",
            f"<think>\nWork through this problem step by step. Show all your thinking.\n</think>\n\n{problem}",
        )

        conclude_step = await self._step(
            trace,
            StepKind.CONCLUDE,
            "Extract answer",
            f"Based on this reasoning:\n{scratch_step.result[:500]}\n\nProvide a concise final answer to: {problem}",
        )

        trace.final_answer = conclude_step.result
        trace.confidence = conclude_step.confidence
        trace.duration_ms = (time.time() - t0) * 1000
        self._traces.append(trace)
        self._stats["traces"] += 1
        return trace

    async def reason_self_ask(self, problem: str) -> ReasoningTrace:
        """Self-ask: generate sub-questions, answer each, combine."""
        import secrets

        trace = ReasoningTrace(
            trace_id=secrets.token_hex(8),
            problem=problem,
            strategy=ReasoningStrategy.SELF_ASK,
        )
        t0 = time.time()

        qa_step = await self._step(
            trace,
            StepKind.DECOMPOSE,
            "Generate sub-questions",
            f"self-ask style: For this problem, generate 3 sub-questions and answer each.\nProblem: {problem}",
        )

        # Parse Q/A pairs
        qa_pairs = re.findall(
            r"Q:\s*(.+?)\nA:\s*(.+?)(?=\nQ:|\Z)", qa_step.result, re.DOTALL
        )
        for q, a in qa_pairs[:3]:
            step = ThoughtStep(
                step_id=f"qa-{len(trace.steps)}",
                kind=StepKind.REASON,
                content=f"Q: {q.strip()}",
                result=f"A: {a.strip()}",
                confidence=_estimate_confidence(a),
            )
            trace.steps.append(step)
            self._stats["steps_total"] += 1

        conclude_step = await self._step(
            trace,
            StepKind.CONCLUDE,
            "Combine answers",
            f"Problem: {problem}\n\nSub-answers:\n{qa_step.result[:400]}\n\nFinal answer:",
        )

        trace.final_answer = conclude_step.result
        trace.confidence = conclude_step.confidence
        trace.duration_ms = (time.time() - t0) * 1000
        self._traces.append(trace)
        self._stats["traces"] += 1
        return trace

    async def reason(
        self,
        problem: str,
        strategy: ReasoningStrategy = ReasoningStrategy.LINEAR,
    ) -> ReasoningTrace:
        if strategy == ReasoningStrategy.SCRATCHPAD:
            return await self.reason_scratchpad(problem)
        elif strategy == ReasoningStrategy.SELF_ASK:
            return await self.reason_self_ask(problem)
        return await self.reason_linear(problem)

    def recent_traces(self, limit: int = 10) -> list[dict]:
        return [t.to_dict() for t in self._traces[-limit:]]

    def stats(self) -> dict:
        avg_conf = (
            sum(t.confidence for t in self._traces) / len(self._traces)
            if self._traces
            else 0.0
        )
        return {
            **self._stats,
            "avg_confidence": round(avg_conf, 3),
        }


def build_jarvis_chain_of_thought(llm_fn: Callable | None = None) -> ChainOfThought:
    return ChainOfThought(llm_fn=llm_fn)


async def main():
    import sys

    cot = build_jarvis_chain_of_thought()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Chain-of-thought demo...\n")

        problems = [
            (
                "If a train travels 120 km in 2 hours, what is its average speed?",
                ReasoningStrategy.LINEAR,
            ),
            (
                "What are the trade-offs between microservices and monoliths?",
                ReasoningStrategy.SCRATCHPAD,
            ),
            (
                "Should I use Redis or PostgreSQL for caching?",
                ReasoningStrategy.SELF_ASK,
            ),
        ]

        for problem, strategy in problems:
            trace = await cot.reason(problem, strategy)
            print(f"\n  [{strategy.value}] {problem[:60]!r}")
            print(
                f"  Steps: {len(trace.steps)}  Confidence: {trace.confidence:.2f}  Time: {trace.duration_ms:.0f}ms"
            )
            print(f"  Answer: {trace.final_answer[:100]}")
            for step in trace.steps[:3]:
                print(
                    f"    [{step.kind.value}] {step.content[:50]} → {step.result[:50]}"
                )

        print(f"\nStats: {json.dumps(cot.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cot.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
