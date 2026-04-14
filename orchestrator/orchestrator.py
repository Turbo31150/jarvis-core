"""
LLM Orchestrator - Main orchestration engine
Combines all components for intelligent task routing and execution.
"""

import logging
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime
from dataclasses import dataclass

from intent_router import IntentRouter, RoutingDecision, TaskType
from context_manager import ContextManager, SessionState, ExecutionResult
from claude_client import ClaudeClient, ClaudeResponse
from decision_engine import DecisionEngine, Rule
from feedback_loop import FeedbackLoop


logger = logging.getLogger(__name__)


@dataclass
class OrchestrationConfig:
    """Configuration for orchestrator"""
    enable_claude: bool = True
    claude_api_key: str = None
    max_auto_correction_confidence: float = 0.95
    min_escalation_confidence: float = 0.6
    learning_enabled: bool = True
    session_timeout_minutes: int = 30
    log_level: str = "INFO"


class LLMOrchestrator:
    """
    Main orchestrator combining all components.
    
    Responsibilities:
    - Route incoming tasks
    - Manage execution context
    - Coordinate with Claude API
    - Apply decision rules
    - Track learning
    - Handle multi-step workflows
    
    Example:
        orchestrator = LLMOrchestrator()
        result = orchestrator.route_and_execute(
            task_context={"source": "email", ...},
            executor_fn=my_executor
        )
    """
    
    def __init__(self, config: OrchestrationConfig = None):
        """
        Initialize orchestrator.
        
        Args:
            config: Orchestration configuration
        """
        self.config = config or OrchestrationConfig()
        self._setup_logging()
        
        # Initialize components
        self.router = IntentRouter()
        self.context_mgr = ContextManager(
            max_history_size=100
        )
        self.claude = ClaudeClient(
            api_key=self.config.claude_api_key
        ) if self.config.enable_claude else None
        self.engine = DecisionEngine()
        self.feedback = FeedbackLoop()
        
        logger.info("LLM Orchestrator initialized")
    
    def _setup_logging(self):
        """Configure logging"""
        level = getattr(logging, self.config.log_level, logging.INFO)
        logging.basicConfig(
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    def route_and_execute(self, 
                         task_context: Dict[str, Any],
                         executor_fn: Callable = None,
                         session_id: str = None,
                         user_id: str = None) -> Dict[str, Any]:
        """
        Main workflow: route task and execute.
        
        Args:
            task_context: Task metadata and state
            executor_fn: Function to execute the action
            session_id: Optional existing session
            user_id: Optional user identifier
            
        Returns:
            Dict with execution result and metadata
        """
        try:
            # 1. Get or create session
            if not session_id:
                if not user_id:
                    user_id = "anonymous"
                session = self.context_mgr.create_session(user_id)
                session_id = session.id
            else:
                session = self.context_mgr.get_session(session_id)
                if not session:
                    logger.warning(f"Session not found: {session_id}")
                    session = self.context_mgr.create_session(user_id or "unknown")
                    session_id = session.id
            
            # 2. Route task intent
            logger.info(f"Routing task from session {session_id}")
            routing_decision = self.router.route_task(task_context)
            
            # Add to history
            self.context_mgr.add_to_history(session_id, {
                "event_type": "task_routed",
                "task_type": routing_decision.task_type.value,
                "confidence": routing_decision.confidence
            })
            
            # 3. Get task parameters
            params = self.router.get_suggested_params(task_context)
            
            # Try Claude enhancement if enabled
            if self.claude:
                try:
                    claude_params = self.claude.suggest_parameters(
                        routing_decision.task_type.value,
                        task_context
                    )
                    if claude_params:
                        params.update(claude_params)
                except Exception as e:
                    logger.warning(f"Claude parameter suggestion failed: {e}")
            
            # 4. Make decision
            decision = self.engine.make_decision(
                intent=routing_decision.task_type.value,
                confidence=routing_decision.confidence,
                options=routing_decision.suggested_actions,
                state=self.context_mgr.get_state()
            )
            
            logger.info(f"Decision: {decision['action']} (confidence: {decision['confidence']})")
            
            # Add decision to history
            self.context_mgr.add_to_history(session_id, {
                "event_type": "decision_made",
                "action": decision["action"],
                "confidence": decision["confidence"],
                "escalate": decision.get("escalate", False)
            })
            
            # 5. Escalation check
            if decision.get("escalate"):
                return {
                    "status": "escalated",
                    "reason": decision.get("reason"),
                    "confidence": decision["confidence"],
                    "routing_decision": routing_decision.to_dict(),
                    "decision": decision,
                    "session_id": session_id,
                    "requires_human_review": True
                }
            
            # 6. Execute action
            execution_result = None
            if executor_fn:
                logger.info(f"Executing action: {decision['action']}")
                try:
                    execution_result = executor_fn(
                        action=decision["action"],
                        parameters=params,
                        context=task_context,
                        session_id=session_id
                    )
                except Exception as e:
                    logger.error(f"Execution error: {e}")
                    
                    # Try Claude error diagnosis
                    if self.claude:
                        try:
                            diagnosis = self.claude.diagnose_error(
                                error_type=type(e).__name__,
                                error_message=str(e),
                                failed_task=decision["action"],
                                context=task_context
                            )
                            logger.info(f"Error diagnosis: {diagnosis}")
                        except Exception as diag_error:
                            logger.warning(f"Diagnosis failed: {diag_error}")
                    
                    # Record failure
                    self.engine.record_failure(decision["action"])
                    
                    execution_result = {
                        "status": "error",
                        "error": str(e),
                        "error_type": type(e).__name__
                    }
            else:
                execution_result = {
                    "status": "queued",
                    "action": decision["action"],
                    "parameters": params
                }
            
            # 7. Record execution for learning
            success = execution_result.get("status") in ["completed", "queued"]
            
            if self.config.learning_enabled:
                result_obj = ExecutionResult(
                    task_id=f"{session_id}_{datetime.now().timestamp()}",
                    task_type=routing_decision.task_type.value,
                    success=success,
                    duration=execution_result.get("duration", 0),
                    parameters_used=params,
                    error=execution_result.get("error") if not success else None,
                    output=execution_result.get("output")
                )
                self.feedback.record_result(result_obj)
                self.feedback.record_decision(
                    routing_decision.task_type.value,
                    routing_decision.confidence,
                    success
                )
            
            # Record success
            if success:
                self.engine.record_success(decision["action"])
            
            # 8. Build response
            return {
                "status": "completed" if success else "failed",
                "session_id": session_id,
                "routing_decision": routing_decision.to_dict(),
                "decision": decision,
                "parameters": params,
                "execution_result": execution_result,
                "requires_human_review": False,
                "timestamp": datetime.now().isoformat()
            }
        
        except Exception as e:
            logger.error(f"Orchestration error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "session_id": session_id,
                "requires_human_review": True
            }
    
    def get_session_context(self, session_id: str) -> Dict[str, Any]:
        """Get current session context"""
        return self.context_mgr.get_context_for_prompt(session_id)
    
    def start_workflow(self, session_id: str, workflow_type: str, 
                      steps: List[str]) -> Dict[str, Any]:
        """
        Start multi-step workflow.
        
        Args:
            session_id: Session identifier
            workflow_type: Type of workflow
            steps: List of step names
            
        Returns:
            Workflow state
        """
        workflow_id = f"wf_{session_id}_{datetime.now().timestamp()}"
        return self.context_mgr.start_workflow(
            session_id,
            workflow_id,
            workflow_type,
            steps
        )
    
    def execute_workflow_step(self, session_id: str, workflow_id: str,
                            step_name: str, executor_fn: Callable,
                            context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute single workflow step.
        
        Args:
            session_id: Session identifier
            workflow_id: Workflow identifier
            step_name: Step name to execute
            executor_fn: Function to execute step
            context: Step context
            
        Returns:
            Step execution result
        """
        try:
            result = executor_fn(
                session_id=session_id,
                workflow_id=workflow_id,
                step=step_name,
                context=context or {}
            )
            
            self.context_mgr.complete_workflow_step(
                session_id,
                workflow_id,
                step_name,
                result
            )
            
            return {
                "status": "completed",
                "step": step_name,
                "result": result
            }
        
        except Exception as e:
            logger.error(f"Workflow step error: {e}")
            
            self.context_mgr.add_to_history(session_id, {
                "event_type": "workflow_step_failed",
                "workflow_id": workflow_id,
                "step": step_name,
                "error": str(e)
            })
            
            return {
                "status": "failed",
                "step": step_name,
                "error": str(e)
            }
    
    def get_learning_stats(self, task_type: str = None) -> Dict[str, Any]:
        """
        Get learning statistics.
        
        Args:
            task_type: Optional task type
            
        Returns:
            Learning and performance statistics
        """
        return {
            "feedback_stats": self.feedback.get_stats(task_type),
            "decision_stats": self.engine.get_decision_stats(task_type),
            "patterns": [
                {
                    "type": p.pattern_type,
                    "description": p.description,
                    "success_rate": p.success_rate,
                    "confidence": p.confidence
                }
                for p in self.feedback.identify_patterns(task_type)
            ] if task_type else [],
            "suggestions": self.feedback.get_improvement_suggestions(task_type) if task_type else []
        }
    
    def get_full_report(self) -> Dict[str, Any]:
        """
        Get comprehensive system report.
        
        Returns:
            Full system status and metrics
        """
        return {
            "timestamp": datetime.now().isoformat(),
            "sessions_active": len(self.context_mgr.sessions),
            "total_decisions": len(self.engine.decision_history),
            "total_executions": len(self.feedback.results),
            "circuit_breakers": {
                name: cb for name, cb in self.engine.circuit_breakers.items()
            },
            "learning_report": self.feedback.generate_learning_report(),
            "rule_suggestions": self.engine.suggest_rule_adjustments()
        }
    
    def cleanup_sessions(self) -> int:
        """Clean up expired sessions"""
        return self.context_mgr.cleanup_expired_sessions()


class OrchestrationExecutor:
    """Helper for standard task execution"""
    
    @staticmethod
    def execute_email_task(action: str, parameters: Dict[str, Any],
                          context: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Execute email-related task"""
        # This would integrate with actual email service
        return {
            "status": "completed",
            "action": action,
            "message_id": f"msg_{session_id}",
            "duration": 1.2
        }
    
    @staticmethod
    def execute_analytics_task(action: str, parameters: Dict[str, Any],
                             context: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Execute analytics task"""
        return {
            "status": "completed",
            "action": action,
            "report_id": f"rpt_{session_id}",
            "duration": 2.5
        }
    
    @staticmethod
    def execute_error_recovery(action: str, parameters: Dict[str, Any],
                             context: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Execute error recovery action"""
        return {
            "status": "completed",
            "action": action,
            "attempts": parameters.get("max_retries", 3),
            "duration": 0.8
        }


__all__ = [
    "LLMOrchestrator",
    "OrchestrationConfig",
    "OrchestrationExecutor"
]
