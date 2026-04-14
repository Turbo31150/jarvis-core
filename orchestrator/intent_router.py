"""
Intent Router - Intelligent task classification and routing
Classifies incoming tasks and suggests appropriate actions
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Callable
import re
import logging


logger = logging.getLogger(__name__)


class TaskType(Enum):
    """Enumeration of task types"""
    PROSPECTION = "prospection"
    OPTIMIZATION = "optimization"
    ERROR_HANDLING = "error_handling"
    CONTENT_CREATION = "content_creation"
    ANALYTICS = "analytics"
    MAINTENANCE = "maintenance"
    UNCLEAR = "unclear"


@dataclass
class RoutingDecision:
    """Result of task routing analysis"""
    task_type: TaskType
    confidence: float
    suggested_actions: List[str]
    reasoning: str
    raw_context: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "task_type": self.task_type.value,
            "confidence": self.confidence,
            "suggested_actions": self.suggested_actions,
            "reasoning": self.reasoning,
            "parameters": self.parameters
        }


class IntentRouter:
    """
    Intelligent intent detection and task routing.
    
    Responsibilities:
    - Classify task intent from context
    - Generate confidence scores
    - Suggest appropriate actions
    - Generate task parameters
    
    Example:
        router = IntentRouter()
        decision = router.route_task(task_context)
        params = router.get_suggested_params(task_context)
    """
    
    def __init__(self, claude_client=None):
        """
        Initialize router.
        
        Args:
            claude_client: Optional Claude client for advanced analysis
        """
        self.claude = claude_client
        self._init_patterns()
    
    def _init_patterns(self):
        """Initialize regex patterns for intent detection"""
        self.patterns = {
            "email_prospect": {
                "keywords": ["partnership", "interested", "discuss", "opportunity", 
                            "inquiry", "proposal", "collaboration"],
                "source": "email",
                "weight": 0.4
            },
            "performance_issue": {
                "keywords": ["decreased", "dropped", "declined", "engagement", 
                            "metric", "performance", "traffic"],
                "source": "analytics",
                "weight": 0.4
            },
            "error": {
                "keywords": ["error", "failed", "timeout", "exception", "crash"],
                "source": ["execution_error", "log"],
                "weight": 0.5
            },
            "content": {
                "keywords": ["publish", "post", "share", "content", "article"],
                "source": ["manual", "scheduler"],
                "weight": 0.3
            }
        }
    
    def route_task(self, task_context: Dict[str, Any]) -> RoutingDecision:
        """
        Route a task to appropriate handler based on context.
        
        Args:
            task_context: Dict with task metadata and state
            
        Returns:
            RoutingDecision with classification and suggestions
        """
        if not task_context:
            return RoutingDecision(
                task_type=TaskType.UNCLEAR,
                confidence=0.0,
                suggested_actions=[],
                reasoning="Empty context provided"
            )
        
        # Try multiple detection strategies
        scores = {}
        
        # 1. Pattern-based detection
        pattern_scores = self._detect_by_patterns(task_context)
        scores.update(pattern_scores)
        
        # 2. Source-based detection
        source_scores = self._detect_by_source(task_context)
        scores.update(source_scores)
        
        # 3. Content analysis
        content_scores = self._analyze_content(task_context)
        scores.update(content_scores)
        
        # 4. State-based detection
        state_scores = self._detect_by_state(task_context)
        scores.update(state_scores)
        
        # Find best match
        if not scores:
            return RoutingDecision(
                task_type=TaskType.UNCLEAR,
                confidence=0.0,
                suggested_actions=["need_clarification"],
                reasoning="No clear intent detected"
            )
        
        best_type = max(scores, key=scores.get)
        confidence = scores[best_type]
        
        # Generate suggested actions
        actions = self._get_actions_for_type(best_type, task_context)
        reasoning = self._generate_reasoning(best_type, task_context, confidence)
        
        return RoutingDecision(
            task_type=TaskType(best_type),
            confidence=min(confidence, 1.0),
            suggested_actions=actions,
            reasoning=reasoning,
            raw_context=task_context
        )
    
    def _detect_by_patterns(self, context: Dict[str, Any]) -> Dict[str, float]:
        """Detect intent using keyword patterns"""
        scores = {}
        content = str(context.get("content", "")) + str(context.get("subject", ""))
        content_lower = content.lower()
        
        for pattern_name, pattern_data in self.patterns.items():
            keywords = pattern_data.get("keywords", [])
            matches = sum(1 for kw in keywords if kw in content_lower)
            
            if matches > 0:
                score = min(matches * 0.15, 1.0) * pattern_data["weight"]
                
                # Map pattern name to TaskType
                if "prospect" in pattern_name:
                    scores["prospection"] = score
                elif "performance" in pattern_name:
                    scores["optimization"] = score
                elif "error" in pattern_name:
                    scores["error_handling"] = score
                elif "content" in pattern_name:
                    scores["content_creation"] = score
        
        return scores
    
    def _detect_by_source(self, context: Dict[str, Any]) -> Dict[str, float]:
        """Detect intent based on source"""
        scores = {}
        source = context.get("source", "").lower()
        
        source_mapping = {
            "email": "prospection",
            "analytics": "optimization",
            "execution_error": "error_handling",
            "log": "error_handling",
            "scheduler": "content_creation",
            "api": "optimization"
        }
        
        if source in source_mapping:
            scores[source_mapping[source]] = 0.4
        
        return scores
    
    def _analyze_content(self, context: Dict[str, Any]) -> Dict[str, float]:
        """Analyze content for intent clues"""
        scores = {}
        
        # Check for error indicators
        if context.get("error_type") or context.get("error_message"):
            scores["error_handling"] = 0.7
        
        # Check for metric changes
        if "current_value" in context and "previous_value" in context:
            try:
                prev = float(context["previous_value"])
                curr = float(context["current_value"])
                change_pct = ((curr - prev) / prev * 100) if prev != 0 else 0
                
                if abs(change_pct) > 20:
                    scores["optimization"] = min(abs(change_pct) / 100, 0.9)
            except (ValueError, TypeError):
                pass
        
        # Check for CRM mentions
        if context.get("source") == "email" and not context.get("is_internal"):
            scores["prospection"] = 0.6
        
        return scores
    
    def _detect_by_state(self, context: Dict[str, Any]) -> Dict[str, float]:
        """Detect based on system state"""
        scores = {}
        system_state = context.get("system_state", {})
        
        # If we have historical data, analyze trends
        if system_state.get("recent_errors", 0) > 3:
            scores["error_handling"] = 0.5
        
        if system_state.get("crm_needs_update"):
            scores["prospection"] = 0.5
        
        return scores
    
    def _get_actions_for_type(self, task_type: str, 
                             context: Dict[str, Any]) -> List[str]:
        """Get suggested actions for task type"""
        actions = {
            "prospection": [
                "send_auto_reply",
                "add_to_crm",
                "schedule_follow_up",
                "send_to_sales_team"
            ],
            "optimization": [
                "analyze_metrics",
                "identify_root_cause",
                "suggest_adjustments",
                "create_action_plan"
            ],
            "error_handling": [
                "diagnose_error",
                "attempt_recovery",
                "log_incident",
                "notify_stakeholders"
            ],
            "content_creation": [
                "draft_content",
                "schedule_publication",
                "setup_distribution",
                "track_engagement"
            ],
            "analytics": [
                "collect_metrics",
                "generate_report",
                "identify_trends",
                "alert_stakeholders"
            ]
        }
        
        return actions.get(task_type, ["clarify_intent", "escalate"])
    
    def _generate_reasoning(self, task_type: str, context: Dict[str, Any], 
                          confidence: float) -> str:
        """Generate human-readable reasoning"""
        reasoning_templates = {
            "prospection": "Email from unknown/new contact with service inquiry",
            "optimization": "Performance metrics showing significant change",
            "error_handling": "Task execution encountered error condition",
            "content_creation": "Content publishing workflow detected",
            "analytics": "Analytics data collection and analysis needed"
        }
        
        base_reasoning = reasoning_templates.get(
            task_type, 
            "Task intent not clearly defined"
        )
        
        # Add confidence context
        if confidence > 0.85:
            base_reasoning += " (high confidence)"
        elif confidence > 0.65:
            base_reasoning += " (moderate confidence)"
        else:
            base_reasoning += " (low confidence - may need clarification)"
        
        return base_reasoning
    
    def get_suggested_params(self, task_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate suggested parameters for task execution.
        
        Args:
            task_context: Context information
            
        Returns:
            Dict of suggested parameters
        """
        params = {}
        
        # Route to get task type first
        decision = self.route_task(task_context)
        task_type = decision.task_type.value
        
        # Generate base parameters
        if task_type == "prospection":
            params.update(self._params_for_prospection(task_context))
        elif task_type == "optimization":
            params.update(self._params_for_optimization(task_context))
        elif task_type == "error_handling":
            params.update(self._params_for_error_handling(task_context))
        elif task_type == "content_creation":
            params.update(self._params_for_content(task_context))
        
        # Add common parameters
        params["priority"] = self._determine_priority(task_context, decision)
        params["execution_mode"] = self._determine_mode(task_context, decision)
        params["timeout"] = self._determine_timeout(task_type)
        
        return params
    
    def _params_for_prospection(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parameters for prospection tasks"""
        return {
            "recipient": context.get("sender", "unknown@domain.com"),
            "template_id": context.get("system_state", {}).get("email_template", "default"),
            "add_to_crm": True,
            "follow_up_days": 3,
            "priority": "high"
        }
    
    def _params_for_optimization(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parameters for optimization tasks"""
        return {
            "analysis_depth": "detailed",
            "generate_recommendations": True,
            "include_timeline": True,
            "notify_stakeholders": True
        }
    
    def _params_for_error_handling(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parameters for error handling"""
        return {
            "error_type": context.get("error_type", "unknown"),
            "retry_enabled": True,
            "max_retries": 3,
            "backoff_strategy": "exponential",
            "escalate_on_failure": True
        }
    
    def _params_for_content(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Parameters for content creation"""
        return {
            "platform": context.get("target_platform", "multi"),
            "draft_mode": True,
            "requires_approval": True,
            "schedule_optimal": True
        }
    
    def _determine_priority(self, context: Dict[str, Any], 
                           decision: RoutingDecision) -> str:
        """Determine task priority"""
        if decision.confidence > 0.85:
            return "high"
        elif decision.confidence > 0.6:
            return "medium"
        else:
            return "low"
    
    def _determine_mode(self, context: Dict[str, Any], 
                       decision: RoutingDecision) -> str:
        """Determine execution mode (sync/async)"""
        # High confidence errors should be sync
        if decision.task_type == TaskType.ERROR_HANDLING:
            return "sync"
        # Most others async
        return "async"
    
    def _determine_timeout(self, task_type: str) -> int:
        """Determine timeout in seconds"""
        timeouts = {
            "prospection": 60,
            "optimization": 300,
            "error_handling": 30,
            "content_creation": 120,
            "analytics": 240
        }
        return timeouts.get(task_type, 60)


__all__ = ["IntentRouter", "TaskType", "RoutingDecision"]
