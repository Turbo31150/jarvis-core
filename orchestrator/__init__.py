"""
LLM Orchestrator Router - Intelligent Task Routing and Decision Engine
Version: 1.0.0

Core components:
- IntentRouter: Classify task intent and suggest actions
- ContextManager: Manage session state and execution history
- ClaudeClient: API wrapper for Claude-powered analysis
- DecisionEngine: Rule evaluation and decision making
- FeedbackLoop: Learning and adaptation system
- LLMOrchestrator: Main orchestration engine
"""

try:
    from .intent_router import (
        IntentRouter,
        TaskType,
        RoutingDecision
    )

    from .context_manager import (
        ContextManager,
        SessionState,
        ExecutionResult
    )

    from .claude_client import (
        ClaudeClient,
        ClaudeResponse,
        PromptBuilder
    )

    from .decision_engine import (
        DecisionEngine,
        Rule,
        RuleType,
        DecisionResult,
        ConflictResolver
    )

    from .feedback_loop import (
        FeedbackLoop,
        ExecutionMetrics,
        LearningPattern
    )

    from .orchestrator import (
        LLMOrchestrator,
        OrchestrationConfig,
        OrchestrationExecutor
    )
except ImportError:
    # Fallback for direct execution
    from intent_router import (
        IntentRouter,
        TaskType,
        RoutingDecision
    )

    from context_manager import (
        ContextManager,
        SessionState,
        ExecutionResult
    )

    from claude_client import (
        ClaudeClient,
        ClaudeResponse,
        PromptBuilder
    )

    from decision_engine import (
        DecisionEngine,
        Rule,
        RuleType,
        DecisionResult,
        ConflictResolver
    )

    from feedback_loop import (
        FeedbackLoop,
        ExecutionMetrics,
        LearningPattern
    )

    from orchestrator import (
        LLMOrchestrator,
        OrchestrationConfig,
        OrchestrationExecutor
    )

__version__ = "1.0.0"
__author__ = "JARVIS Cluster - OMEGA-DEV"

__all__ = [
    # Router
    "IntentRouter",
    "TaskType",
    "RoutingDecision",
    
    # Context
    "ContextManager",
    "SessionState",
    "ExecutionResult",
    
    # Claude
    "ClaudeClient",
    "ClaudeResponse",
    "PromptBuilder",
    
    # Decision
    "DecisionEngine",
    "Rule",
    "RuleType",
    "DecisionResult",
    "ConflictResolver",
    
    # Feedback
    "FeedbackLoop",
    "ExecutionMetrics",
    "LearningPattern",
    
    # Orchestrator
    "LLMOrchestrator",
    "OrchestrationConfig",
    "OrchestrationExecutor"
]
