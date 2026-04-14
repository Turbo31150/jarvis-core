#!/usr/bin/env python3
"""
jarvis_resilience_tester — Automated resilience tests: retry, circuit breaker, fallback
Run scenario suites to validate system behavior under failure conditions
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.resilience_tester")


class ScenarioKind(str, Enum):
    RETRY = "retry"
    CIRCUIT_BREAKER = "circuit_breaker"
    FALLBACK = "fallback"
    BULKHEAD = "bulkhead"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CASCADE_FAILURE = "cascade_failure"


class TestResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass
class ScenarioStep:
    name: str
    action: Callable
    expected_result: str = "success"  # "success" | "failure" | "any"
    expected_value: Any = None
    timeout_s: float = 10.0


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    kind: ScenarioKind
    result: TestResult
    steps_total: int = 0
    steps_passed: int = 0
    steps_failed: int = 0
    duration_ms: float = 0.0
    error: str = ""
    details: list[dict] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def pass_rate(self) -> float:
        return self.steps_passed / max(self.steps_total, 1)

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "kind": self.kind.value,
            "result": self.result.value,
            "steps_total": self.steps_total,
            "steps_passed": self.steps_passed,
            "steps_failed": self.steps_failed,
            "pass_rate": round(self.pass_rate, 4),
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "details": self.details,
        }


@dataclass
class Scenario:
    name: str
    kind: ScenarioKind
    steps: list[ScenarioStep]
    description: str = ""
    tags: list[str] = field(default_factory=list)


class ResilienceTester:
    def __init__(self):
        self._scenarios: list[Scenario] = []
        self._results: list[ScenarioResult] = []
        self._stats: dict[str, int] = {
            "scenarios_run": 0,
            "scenarios_passed": 0,
            "scenarios_failed": 0,
            "total_steps": 0,
        }

    def add_scenario(self, scenario: Scenario):
        self._scenarios.append(scenario)

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        import secrets

        scenario_id = secrets.token_hex(6)
        start = time.time()
        details: list[dict] = []
        steps_passed = 0
        steps_failed = 0
        overall = TestResult.PASS

        for step in scenario.steps:
            step_start = time.time()
            step_detail: dict[str, Any] = {
                "step": step.name,
                "expected": step.expected_result,
            }
            try:
                coro = step.action()
                if asyncio.iscoroutine(coro):
                    value = await asyncio.wait_for(coro, timeout=step.timeout_s)
                else:
                    value = coro

                step_detail["actual"] = "success"
                step_detail["value"] = str(value)[:100] if value is not None else None
                step_detail["duration_ms"] = round((time.time() - step_start) * 1000, 2)

                if step.expected_result in ("success", "any"):
                    if step.expected_value is not None and value != step.expected_value:
                        step_detail["result"] = "fail"
                        step_detail["reason"] = (
                            f"expected value {step.expected_value!r}, got {value!r}"
                        )
                        steps_failed += 1
                        overall = TestResult.FAIL
                    else:
                        step_detail["result"] = "pass"
                        steps_passed += 1
                else:
                    # Expected failure but got success
                    step_detail["result"] = "fail"
                    step_detail["reason"] = "expected failure but step succeeded"
                    steps_failed += 1
                    overall = TestResult.FAIL

            except asyncio.TimeoutError:
                step_detail["actual"] = "timeout"
                step_detail["duration_ms"] = round((time.time() - step_start) * 1000, 2)
                if step.expected_result in ("failure", "any"):
                    step_detail["result"] = "pass"
                    steps_passed += 1
                else:
                    step_detail["result"] = "fail"
                    step_detail["reason"] = "unexpected timeout"
                    steps_failed += 1
                    overall = TestResult.FAIL

            except Exception as e:
                step_detail["actual"] = "error"
                step_detail["error"] = str(e)
                step_detail["duration_ms"] = round((time.time() - step_start) * 1000, 2)
                if step.expected_result in ("failure", "any"):
                    step_detail["result"] = "pass"
                    steps_passed += 1
                else:
                    step_detail["result"] = "fail"
                    step_detail["reason"] = f"unexpected error: {e}"
                    steps_failed += 1
                    overall = TestResult.FAIL

            details.append(step_detail)

        dur = (time.time() - start) * 1000
        result = ScenarioResult(
            scenario_id=scenario_id,
            name=scenario.name,
            kind=scenario.kind,
            result=overall,
            steps_total=len(scenario.steps),
            steps_passed=steps_passed,
            steps_failed=steps_failed,
            duration_ms=dur,
            details=details,
        )
        self._results.append(result)
        self._stats["scenarios_run"] += 1
        self._stats["total_steps"] += len(scenario.steps)
        if overall == TestResult.PASS:
            self._stats["scenarios_passed"] += 1
        else:
            self._stats["scenarios_failed"] += 1

        icon = "✅" if overall == TestResult.PASS else "❌"
        log.info(
            f"{icon} Scenario {scenario.name!r} → {overall.value} ({steps_passed}/{len(scenario.steps)} steps) {dur:.0f}ms"
        )
        return result

    async def run_all(
        self,
        kinds: list[ScenarioKind] | None = None,
        tags: list[str] | None = None,
    ) -> list[ScenarioResult]:
        scenarios = self._scenarios
        if kinds:
            scenarios = [s for s in scenarios if s.kind in kinds]
        if tags:
            scenarios = [s for s in scenarios if any(t in s.tags for t in tags)]

        results = []
        for s in scenarios:
            r = await self.run_scenario(s)
            results.append(r)
        return results

    def summary(self) -> dict:
        passed = [r for r in self._results if r.result == TestResult.PASS]
        failed = [r for r in self._results if r.result == TestResult.FAIL]
        return {
            "total": len(self._results),
            "passed": len(passed),
            "failed": len(failed),
            "pass_rate": round(len(passed) / max(len(self._results), 1), 4),
            "failed_scenarios": [r.name for r in failed],
        }

    def stats(self) -> dict:
        return {**self._stats, **self.summary()}


def build_jarvis_resilience_tester() -> ResilienceTester:
    tester = ResilienceTester()

    # --- Retry scenario ---
    attempt_count = {"n": 0}

    async def flaky_service():
        attempt_count["n"] += 1
        if attempt_count["n"] < 3:
            raise ConnectionError("service unavailable")
        return {"ok": True}

    async def retry_with_backoff():
        for i in range(5):
            try:
                return await flaky_service()
            except ConnectionError:
                if i < 4:
                    await asyncio.sleep(0.01)
        raise RuntimeError("all retries exhausted")

    tester.add_scenario(
        Scenario(
            name="retry-on-transient-failure",
            kind=ScenarioKind.RETRY,
            description="Service fails 2 times then succeeds",
            steps=[
                ScenarioStep(
                    "retry_succeeds", retry_with_backoff, expected_result="success"
                ),
            ],
            tags=["retry", "basic"],
        )
    )

    # --- Circuit breaker scenario ---
    cb_open = {"open": False, "failures": 0}

    async def cb_call():
        if cb_open["open"]:
            raise RuntimeError("circuit open")
        cb_open["failures"] += 1
        if cb_open["failures"] >= 3:
            cb_open["open"] = True
        raise ConnectionError("downstream error")

    async def verify_cb_opens():
        for _ in range(3):
            try:
                await cb_call()
            except Exception:
                pass
        # Now circuit should be open
        try:
            await cb_call()
            return False
        except RuntimeError as e:
            return "circuit open" in str(e)

    tester.add_scenario(
        Scenario(
            name="circuit-breaker-opens",
            kind=ScenarioKind.CIRCUIT_BREAKER,
            description="CB opens after 3 consecutive failures",
            steps=[
                ScenarioStep(
                    "cb_opens_after_failures",
                    verify_cb_opens,
                    expected_result="success",
                    expected_value=True,
                ),
            ],
            tags=["circuit_breaker"],
        )
    )

    # --- Fallback scenario ---
    primary_down = {"down": True}

    async def primary_service():
        if primary_down["down"]:
            raise RuntimeError("primary down")
        return {"source": "primary"}

    async def fallback_service():
        return {"source": "fallback", "degraded": True}

    async def call_with_fallback():
        try:
            return await primary_service()
        except Exception:
            return await fallback_service()

    tester.add_scenario(
        Scenario(
            name="fallback-on-primary-failure",
            kind=ScenarioKind.FALLBACK,
            description="Falls back to secondary when primary is down",
            steps=[
                ScenarioStep(
                    "fallback_returns_data",
                    call_with_fallback,
                    expected_result="success",
                ),
            ],
            tags=["fallback", "basic"],
        )
    )

    # --- Timeout scenario ---
    async def slow_service():
        await asyncio.sleep(5.0)
        return {"ok": True}

    async def call_with_timeout():
        try:
            return await asyncio.wait_for(slow_service(), timeout=0.1)
        except asyncio.TimeoutError:
            raise

    tester.add_scenario(
        Scenario(
            name="timeout-fast-fail",
            kind=ScenarioKind.TIMEOUT,
            description="Request times out and raises TimeoutError",
            steps=[
                ScenarioStep(
                    "timeout_raised", call_with_timeout, expected_result="failure"
                ),
            ],
            tags=["timeout"],
        )
    )

    return tester


async def main():
    import sys

    tester = build_jarvis_resilience_tester()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Resilience tester demo...\n")
        results = await tester.run_all()
        for r in results:
            icon = "✅" if r.result == TestResult.PASS else "❌"
            print(
                f"  {icon} {r.name:<40} {r.result.value:<6} {r.duration_ms:.0f}ms steps={r.steps_passed}/{r.steps_total}"
            )
            for step in r.details:
                s_icon = "  ✓" if step.get("result") == "pass" else "  ✗"
                print(
                    f"    {s_icon} {step['step']}: {step.get('result', '?')} {step.get('reason', '')}"
                )

        print(f"\nSummary: {json.dumps(tester.summary(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tester.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
