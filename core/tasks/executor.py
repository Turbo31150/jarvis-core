"""JARVIS Task Executor — Timeout, retry, cancel, audit logging."""
import asyncio
import logging
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Callable, Dict, Optional

import requests

from .models import TaskRequest, TaskResult, TaskStatus

logger = logging.getLogger("jarvis.executor")

DB_PATH = Path.home() / "IA" / "Core" / "jarvis" / "jarvis-master.db"
_executor_pool = ThreadPoolExecutor(max_workers=8)


def _ensure_db():
    """Create audit table if missing."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY,
            task_type TEXT,
            prompt TEXT,
            target_node TEXT,
            status TEXT,
            output TEXT,
            error TEXT,
            duration REAL,
            confidence REAL,
            created_at REAL,
            completed_at REAL
        )
    """)
    conn.commit()
    conn.close()


def _audit_log(result: TaskResult, request: TaskRequest):
    """Persist task result to SQLite."""
    try:
        _ensure_db()
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO workflow_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.id, request.task_type, request.prompt,
                request.target_node, result.status.value,
                str(result.output)[:4000] if result.output else None,
                result.error, result.duration, result.confidence,
                request.created_at, result.completed_at,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Audit log failed: %s", e)


# --- Handlers by task_type ---

def _handle_generic(task: TaskRequest) -> TaskResult:
    """Default handler — call M1 LMStudio."""
    resp = requests.post(
        "http://127.0.0.1:1234/v1/chat/completions",
        json={"model": "gemma-3-4b", "messages": [{"role": "user", "content": task.prompt}]},
        timeout=task.timeout,
    )
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return TaskResult(request_id=task.id, status=TaskStatus.COMPLETED, output=content, node="M1")


def _handle_local(task: TaskRequest) -> TaskResult:
    """Run a local subprocess command."""
    proc = subprocess.run(
        task.prompt, shell=True, capture_output=True, text=True, timeout=task.timeout
    )
    status = TaskStatus.COMPLETED if proc.returncode == 0 else TaskStatus.FAILED
    return TaskResult(
        request_id=task.id, status=status,
        output=proc.stdout.strip(), error=proc.stderr.strip() or None, node="local",
    )


def _handle_network(task: TaskRequest) -> TaskResult:
    """HTTP GET/POST via requests."""
    url = task.metadata.get("url", task.prompt)
    method = task.metadata.get("method", "GET").upper()
    resp = requests.request(method, url, timeout=task.timeout,
                            json=task.metadata.get("body"))
    return TaskResult(
        request_id=task.id, status=TaskStatus.COMPLETED,
        output=resp.text[:4000], node="network",
        metadata={"status_code": resp.status_code},
    )


def _handle_browser(task: TaskRequest) -> TaskResult:
    """Delegate to BrowserOS CLI."""
    proc = subprocess.run(
        ["browseros", "run", task.prompt],
        capture_output=True, text=True, timeout=task.timeout,
    )
    status = TaskStatus.COMPLETED if proc.returncode == 0 else TaskStatus.FAILED
    return TaskResult(
        request_id=task.id, status=status,
        output=proc.stdout.strip(), error=proc.stderr.strip() or None, node="browseros",
    )


HANDLERS: Dict[str, Callable] = {
    "generic": _handle_generic,
    "local": _handle_local,
    "network": _handle_network,
    "browser": _handle_browser,
}


class TaskExecutor:
    """Execute tasks with timeout, retry, cancel, and audit."""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._cancel_flags: Dict[str, threading.Event] = {}

    def cancel(self, task_id: str):
        """Signal cancellation for a running task."""
        if task_id in self._cancel_flags:
            self._cancel_flags[task_id].set()

    def execute(self, task: TaskRequest) -> TaskResult:
        """Main entry — run task with retry and timeout."""
        if task.dry_run:
            return TaskResult(
                request_id=task.id, status=TaskStatus.COMPLETED,
                output=f"[DRY RUN] Would execute {task.task_type}: {task.prompt[:80]}",
                node=task.target_node or "dry_run",
            )

        cancel_evt = threading.Event()
        self._cancel_flags[task.id] = cancel_evt
        handler = HANDLERS.get(task.task_type, _handle_generic)
        last_error = None
        start = time.time()

        for attempt in range(self.max_retries):
            if cancel_evt.is_set():
                result = TaskResult(
                    request_id=task.id, status=TaskStatus.CANCELLED,
                    error="Cancelled by user", duration=time.time() - start,
                )
                _audit_log(result, task)
                return result

            try:
                future = _executor_pool.submit(handler, task)
                result = future.result(timeout=task.timeout)
                result.duration = time.time() - start
                _audit_log(result, task)
                return result
            except FuturesTimeout:
                last_error = f"Timeout after {task.timeout}s (attempt {attempt + 1})"
                logger.warning("Task %s: %s", task.id, last_error)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e} (attempt {attempt + 1})"
                logger.warning("Task %s: %s", task.id, last_error)

            # Exponential backoff
            if attempt < self.max_retries - 1:
                time.sleep(min(2 ** attempt, 8))

        self._cancel_flags.pop(task.id, None)
        result = TaskResult(
            request_id=task.id, status=TaskStatus.TIMEOUT if "Timeout" in (last_error or "") else TaskStatus.FAILED,
            error=last_error, duration=time.time() - start,
        )
        _audit_log(result, task)
        return result
