"""
Context Manager - Session and state tracking
Maintains execution context, history, and state for intelligent decision making
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import uuid
import logging


logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of task execution"""
    task_id: str
    task_type: str
    success: bool
    duration: float  # seconds
    parameters_used: Dict[str, Any]
    error: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class SessionState:
    """User session state"""
    id: str
    user_id: str
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    history: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = datetime.now()


class ContextManager:
    """
    Manages execution context, session state, and history.
    
    Responsibilities:
    - Create and manage sessions
    - Track system state
    - Maintain execution history
    - Provide context for LLM prompts
    - Support multi-step workflows
    
    Example:
        ctx_mgr = ContextManager()
        session = ctx_mgr.create_session("user_123")
        ctx_mgr.update_state({"status": "active"})
        context = ctx_mgr.get_context_for_prompt(session.id)
    """
    
    def __init__(self, max_history_size: int = 100):
        """
        Initialize context manager.
        
        Args:
            max_history_size: Maximum history entries per session
        """
        self.sessions: Dict[str, SessionState] = {}
        self.global_state: Dict[str, Any] = {}
        self.max_history_size = max_history_size
        self.session_timeout_minutes = 30
    
    def create_session(self, user_id: str, metadata: Dict[str, Any] = None) -> SessionState:
        """
        Create new user session.
        
        Args:
            user_id: User identifier
            metadata: Optional session metadata
            
        Returns:
            New SessionState
        """
        session_id = str(uuid.uuid4())
        session = SessionState(
            id=session_id,
            user_id=user_id,
            metadata=metadata or {}
        )
        self.sessions[session_id] = session
        logger.info(f"Created session {session_id} for user {user_id}")
        return session
    
    def get_session(self, session_id: str) -> Optional[SessionState]:
        """
        Retrieve session by ID.
        
        Args:
            session_id: Session identifier
            
        Returns:
            SessionState or None if not found
        """
        return self.sessions.get(session_id)
    
    def update_state(self, state_dict: Dict[str, Any]):
        """
        Update global system state.
        
        Args:
            state_dict: State updates to merge
        """
        self.global_state.update(state_dict)
        self.global_state["last_update"] = datetime.now().isoformat()
        logger.debug(f"Updated global state: {list(state_dict.keys())}")
    
    def get_state(self) -> Dict[str, Any]:
        """
        Get current global state.
        
        Returns:
            Current system state dictionary
        """
        return self.global_state.copy()
    
    def add_to_history(self, session_id: str, event: Dict[str, Any]):
        """
        Add event to session history.
        
        Args:
            session_id: Session identifier
            event: Event to record
        """
        session = self.get_session(session_id)
        if not session:
            logger.warning(f"Session {session_id} not found")
            return
        
        # Add metadata
        event["timestamp"] = datetime.now().isoformat()
        event["event_id"] = str(uuid.uuid4())
        
        session.history.append(event)
        session.update_activity()
        
        # Trim history if needed
        if len(session.history) > self.max_history_size:
            session.history = session.history[-self.max_history_size:]
        
        logger.debug(f"Added event to session {session_id}: {event.get('task', 'unknown')}")
    
    def get_history(self, session_id: str, limit: int = 10, 
                   days_back: int = None) -> List[Dict[str, Any]]:
        """
        Retrieve session history.
        
        Args:
            session_id: Session identifier
            limit: Maximum entries to return
            days_back: Optional filter to last N days
            
        Returns:
            List of history events
        """
        session = self.get_session(session_id)
        if not session:
            return []
        
        history = session.history.copy()
        
        # Filter by date if specified
        if days_back:
            cutoff = datetime.now() - timedelta(days=days_back)
            history = [
                e for e in history 
                if datetime.fromisoformat(e.get("timestamp", "")) > cutoff
            ]
        
        # Return most recent, up to limit
        return history[-limit:]
    
    def get_context_for_prompt(self, session_id: str) -> Dict[str, Any]:
        """
        Get context injection for LLM prompts.
        
        Combines session state, global state, and recent history
        for comprehensive context.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dict with current_state, recent_history, patterns, etc.
        """
        session = self.get_session(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}
        
        recent_history = self.get_history(session_id, limit=5)
        
        # Aggregate history for patterns
        history_patterns = self._extract_patterns(recent_history)
        
        return {
            "session_id": session_id,
            "user_id": session.user_id,
            "session_created": session.created_at.isoformat(),
            "current_state": self.get_state(),
            "recent_history": recent_history,
            "history_patterns": history_patterns,
            "session_duration_minutes": (
                (datetime.now() - session.created_at).total_seconds() / 60
            ),
            "activity_frequency": self._calculate_activity_frequency(recent_history),
            "metadata": session.metadata
        }
    
    def _extract_patterns(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract patterns from history"""
        if not history:
            return {}
        
        patterns = {}
        
        # Task frequency
        task_counts = {}
        for event in history:
            task = event.get("task", "unknown")
            task_counts[task] = task_counts.get(task, 0) + 1
        patterns["task_frequency"] = task_counts
        
        # Success rate
        successful = sum(1 for e in history if e.get("status") == "completed")
        patterns["success_rate"] = successful / len(history) if history else 0
        
        # Average duration
        durations = [e.get("duration", 0) for e in history if "duration" in e]
        patterns["avg_duration"] = sum(durations) / len(durations) if durations else 0
        
        return patterns
    
    def _calculate_activity_frequency(self, history: List[Dict[str, Any]]) -> str:
        """Calculate activity frequency"""
        if not history:
            return "none"
        
        if len(history) >= 5:
            return "high"
        elif len(history) >= 2:
            return "medium"
        else:
            return "low"
    
    def get_multi_step_workflow_state(self, session_id: str, 
                                     workflow_id: str) -> Dict[str, Any]:
        """
        Get state for multi-step workflow.
        
        Args:
            session_id: Session identifier
            workflow_id: Workflow identifier
            
        Returns:
            Workflow state with completed steps and current position
        """
        session = self.get_session(session_id)
        if not session:
            return {}
        
        # Filter history for this workflow
        workflow_events = [
            e for e in session.history
            if e.get("workflow_id") == workflow_id
        ]
        
        return {
            "workflow_id": workflow_id,
            "total_steps": len(workflow_events),
            "completed": sum(1 for e in workflow_events if e.get("status") == "completed"),
            "failed": sum(1 for e in workflow_events if e.get("status") == "failed"),
            "events": workflow_events
        }
    
    def start_workflow(self, session_id: str, workflow_id: str, 
                      workflow_type: str, steps: List[str]) -> Dict[str, Any]:
        """
        Start tracking a multi-step workflow.
        
        Args:
            session_id: Session identifier
            workflow_id: Workflow identifier
            workflow_type: Type of workflow
            steps: List of step names
            
        Returns:
            Workflow state
        """
        self.add_to_history(session_id, {
            "event_type": "workflow_start",
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "step_count": len(steps),
            "steps": steps,
            "status": "started"
        })
        
        return {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "steps": steps,
            "current_step": 0
        }
    
    def complete_workflow_step(self, session_id: str, workflow_id: str, 
                              step_name: str, result: Any):
        """
        Record completion of workflow step.
        
        Args:
            session_id: Session identifier
            workflow_id: Workflow identifier
            step_name: Step name
            result: Step result/output
        """
        self.add_to_history(session_id, {
            "event_type": "workflow_step",
            "workflow_id": workflow_id,
            "step": step_name,
            "status": "completed",
            "result": result
        })
    
    def cleanup_expired_sessions(self):
        """Remove sessions that have timed out"""
        now = datetime.now()
        expired = []
        
        for session_id, session in self.sessions.items():
            age = now - session.last_activity
            if age > timedelta(minutes=self.session_timeout_minutes):
                expired.append(session_id)
        
        for session_id in expired:
            del self.sessions[session_id]
            logger.info(f"Cleaned up expired session: {session_id}")
        
        return len(expired)
    
    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """
        Get human-readable session summary.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Session summary with stats and recent activity
        """
        session = self.get_session(session_id)
        if not session:
            return {}
        
        history = session.history
        return {
            "session_id": session_id,
            "user_id": session.user_id,
            "duration_minutes": (
                (datetime.now() - session.created_at).total_seconds() / 60
            ),
            "total_events": len(history),
            "recent_tasks": [
                h.get("task", "unknown") for h in history[-5:]
            ],
            "status": "active",
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat()
        }


__all__ = ["ContextManager", "SessionState", "ExecutionResult"]
