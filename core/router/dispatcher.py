"""JARVIS Task Dispatcher — Routes tasks to M1/M2/M3/BrowserOS/local."""

import logging
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import requests

from ..tasks.models import TaskRequest, TaskResult, TaskStatus

logger = logging.getLogger("jarvis.dispatcher")

# Node endpoints
NODES: Dict[str, str] = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
    "M3": "http://192.168.1.113:1234",
    "OL1": "http://127.0.0.1:11434",
}

# Task type → (primary node, model, fallback chain)
ROUTING_TABLE: Dict[str, List[dict]] = {
    "fast": [
        {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
        {"node": "M1", "model": "gemma-3-4b"},
        {"node": "OL1", "model": "qwen2.5:1.5b"},
    ],
    "deep": [
        {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
        {"node": "M1", "model": "deepseek-r1"},
        {"node": "OL1", "model": "deepseek-r1:7b"},
    ],
    "code": [
        {"node": "M2", "model": "deepseek-coder"},
        {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
        {"node": "M1", "model": "qwen3.5-9b"},
    ],
    "generic": [
        {"node": "M1", "model": "gemma-3-4b"},
        {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
    ],
    "analysis": [
        {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
        {"node": "M1", "model": "qwen3.5-9b"},
    ],
}

ANTI_THINK_PREFIX = "<think>\n</think>\n\n"


def _is_deepseek(model: str) -> bool:
    return "deepseek" in model.lower()


def _call_lmstudio(node: str, model: str, prompt: str, timeout: int) -> str:
    """Call LMStudio-compatible API."""
    content = (ANTI_THINK_PREFIX + prompt) if _is_deepseek(model) else prompt
    resp = requests.post(
        f"{NODES[node]}/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_ollama(model: str, prompt: str, timeout: int) -> str:
    """Call Ollama API."""
    content = (ANTI_THINK_PREFIX + prompt) if _is_deepseek(model) else prompt
    resp = requests.post(
        f"{NODES['OL1']}/api/generate",
        json={"model": model, "prompt": content, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def _call_node(node: str, model: str, prompt: str, timeout: int) -> str:
    if node == "OL1":
        return _call_ollama(model, prompt, timeout)
    return _call_lmstudio(node, model, prompt, timeout)


class TaskDispatcher:
    """Dispatch tasks to cluster nodes with fallback and consensus."""

    def dispatch(self, task: TaskRequest) -> TaskResult:
        """Route task to the appropriate node(s)."""
        start = time.time()

        # Explicit target override
        if task.target_node == "local" or task.task_type == "local":
            return self._run_local(task, start)
        if task.target_node == "browseros" or task.task_type == "browser":
            return self._run_browser(task, start)
        if task.task_type == "consensus":
            return self._run_consensus(task, start)

        # Route through table with fallback
        chain = ROUTING_TABLE.get(task.task_type, ROUTING_TABLE["generic"])
        if task.target_node:
            # Move requested node to front
            chain = sorted(chain, key=lambda r: r["node"] != task.target_node)

        for route in chain:
            try:
                output = _call_node(
                    route["node"], route["model"], task.prompt, task.timeout
                )
                return TaskResult(
                    request_id=task.id,
                    status=TaskStatus.COMPLETED,
                    output=output,
                    node=route["node"],
                    duration=time.time() - start,
                    confidence=1.0,
                )
            except Exception as e:
                logger.warning("Node %s failed: %s — trying fallback", route["node"], e)

        return TaskResult(
            request_id=task.id,
            status=TaskStatus.FAILED,
            error="All nodes exhausted",
            duration=time.time() - start,
        )

    def _run_local(self, task: TaskRequest, start: float) -> TaskResult:
        # SEC-001: shell=True avec task.prompt était une RCE — tokeniser sans shell
        try:
            cmd_parts = shlex.split(task.prompt)
        except ValueError as e:
            return TaskResult(
                request_id=task.id,
                status=TaskStatus.FAILED,
                error=f"Invalid command syntax: {e}",
                node="local",
                duration=time.time() - start,
            )
        if not cmd_parts:
            return TaskResult(
                request_id=task.id,
                status=TaskStatus.FAILED,
                error="Empty command after tokenization",
                node="local",
                duration=time.time() - start,
            )
        try:
            proc = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=task.timeout
            )
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as e:
            return TaskResult(
                request_id=task.id,
                status=TaskStatus.FAILED,
                error=str(e),
                node="local",
                duration=time.time() - start,
            )
        return TaskResult(
            request_id=task.id,
            status=TaskStatus.COMPLETED if proc.returncode == 0 else TaskStatus.FAILED,
            output=proc.stdout.strip(),
            error=proc.stderr.strip() or None,
            node="local",
            duration=time.time() - start,
        )

    def _run_browser(self, task: TaskRequest, start: float) -> TaskResult:
        proc = subprocess.run(
            ["browseros", "run", task.prompt],
            capture_output=True,
            text=True,
            timeout=task.timeout,
        )
        return TaskResult(
            request_id=task.id,
            status=TaskStatus.COMPLETED if proc.returncode == 0 else TaskStatus.FAILED,
            output=proc.stdout.strip(),
            error=proc.stderr.strip() or None,
            node="browseros",
            duration=time.time() - start,
        )

    def _run_consensus(self, task: TaskRequest, start: float) -> TaskResult:
        """Query M3 + M1 in parallel, return both answers."""
        routes = [
            {"node": "M3", "model": "deepseek-r1-qwen3-8b"},
            {"node": "M1", "model": "gemma-3-4b"},
        ]
        results = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    _call_node, r["node"], r["model"], task.prompt, task.timeout
                ): r["node"]
                for r in routes
            }
            for future in as_completed(futures):
                node = futures[future]
                try:
                    results[node] = future.result()
                except Exception as e:
                    results[node] = f"[ERROR] {e}"

        return TaskResult(
            request_id=task.id,
            status=TaskStatus.COMPLETED,
            output=results,
            node="consensus",
            duration=time.time() - start,
            confidence=0.8,
            metadata={"nodes_queried": list(results.keys())},
        )
