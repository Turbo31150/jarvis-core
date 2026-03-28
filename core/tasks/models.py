"""JARVIS Core Task Models — Standard request/result types for all agents."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time, uuid

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

class TaskPriority(Enum):
    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3

@dataclass
class TaskRequest:
    prompt: str
    task_type: str = "generic"  # generic, code, analysis, network, sql, voice, browser
    priority: TaskPriority = TaskPriority.MEDIUM
    target_node: Optional[str] = None  # M1, M2, M3, browseros, local
    timeout: int = 30
    dry_run: bool = False
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: float = field(default_factory=time.time)

@dataclass
class TaskResult:
    request_id: str
    status: TaskStatus
    output: Any = None
    error: Optional[str] = None
    node: Optional[str] = None
    duration: float = 0.0
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)
    completed_at: float = field(default_factory=time.time)
