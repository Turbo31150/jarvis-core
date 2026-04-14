"""
LLM Orchestrator Router - Quick Start Guide & Examples

This guide demonstrates how to use the LLM Orchestrator system.
"""

import json
import logging
from datetime import datetime

# Import orchestrator components
from orchestrator import (
    LLMOrchestrator,
    OrchestrationConfig,
    OrchestrationExecutor
)
from intent_router import TaskType
from context_manager import ExecutionResult


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def example_1_basic_routing():
    """Example 1: Basic task routing"""
    print("\n" + "="*60)
    print("EXAMPLE 1: Basic Task Routing")
    print("="*60)
    
    # Initialize orchestrator
    config = OrchestrationConfig(enable_claude=False)  # No Claude needed for demo
    orchestrator = LLMOrchestrator(config)
    
    # Example: Email from prospect
    task_context = {
        "source": "email",
        "sender": "john@techstartup.com",
        "subject": "Partnership Opportunity",
        "content": "Hi, we'd like to discuss a strategic partnership with your team.",
        "system_state": {"crm_count": 150}
    }
    
    result = orchestrator.route_and_execute(
        task_context=task_context,
        user_id="sales_team"
    )
    
    print(f"\nResult:")
    print(json.dumps(result, indent=2, default=str))


def example_2_context_tracking():
    """Example 2: Context and session management"""
    print("\n" + "="*60)
    print("EXAMPLE 2: Context and Session Management")
    print("="*60)
    
    orchestrator = LLMOrchestrator(OrchestrationConfig(enable_claude=False))
    
    # Create session
    session = orchestrator.context_mgr.create_session("user_alice")
    session_id = session.id
    print(f"\nCreated session: {session_id}")
    
    # Update state
    orchestrator.context_mgr.update_state({
        "crm_count": 250,
        "posts_published": 15,
        "engagement_rate": 7.5
    })
    
    # Add events to history
    for i in range(3):
        orchestrator.context_mgr.add_to_history(session_id, {
            "task": f"email_task_{i}",
            "status": "completed",
            "duration": 1.5 + i * 0.5
        })
    
    # Get context for LLM
    context = orchestrator.get_session_context(session_id)
    print(f"\nSession Context:")
    print(json.dumps(context, indent=2, default=str))
    
    # Get summary
    summary = orchestrator.context_mgr.get_session_summary(session_id)
    print(f"\nSession Summary:")
    print(json.dumps(summary, indent=2, default=str))


def example_3_error_diagnosis():
    """Example 3: Error handling and diagnosis"""
    print("\n" + "="*60)
    print("EXAMPLE 3: Error Handling and Diagnosis")
    print("="*60)
    
    orchestrator = LLMOrchestrator(OrchestrationConfig(enable_claude=False))
    
    # Simulate error context
    error_context = {
        "source": "execution_error",
        "error_type": "api_timeout",
        "error_message": "Connection timeout after 30 seconds",
        "failed_task": "publish_post",
        "system_state": {"retry_count": 0}
    }
    
    # Route error
    result = orchestrator.route_and_execute(
        task_context=error_context,
        user_id="error_handler"
    )
    
    print(f"\nError Handling Result:")
    print(json.dumps(result, indent=2, default=str))


def example_4_decision_engine():
    """Example 4: Decision engine with rules"""
    print("\n" + "="*60)
    print("EXAMPLE 4: Decision Engine Rules")
    print("="*60)
    
    from decision_engine import Rule, RuleType
    
    engine = orchestrator.engine if 'orchestrator' in locals() else LLMOrchestrator(
        OrchestrationConfig(enable_claude=False)
    ).engine
    
    # Define custom rule
    custom_rule = Rule(
        name="high_priority_prospect",
        condition=lambda s: s.get("prospect_value", 0) > 50000,
        action="immediate_response",
        threshold=0.9,
        rule_type=RuleType.THRESHOLD_BASED
    )
    
    # Add rule
    engine.add_rule("prospection", custom_rule)
    print(f"\nAdded rule: {custom_rule.name}")
    
    # Make decision
    state = {"prospect_value": 75000}
    decision = engine.make_decision("prospection", 0.85, options=["send_email", "schedule_call"], state=state)
    
    print(f"\nDecision:")
    print(json.dumps(decision, indent=2))


def example_5_learning_loop():
    """Example 5: Learning from execution patterns"""
    print("\n" + "="*60)
    print("EXAMPLE 5: Learning and Pattern Recognition")
    print("="*60)
    
    feedback = LLMOrchestrator(OrchestrationConfig(enable_claude=False)).feedback
    
    # Simulate multiple executions
    for i in range(20):
        result = ExecutionResult(
            task_id=f"email_{i}",
            task_type="email",
            success=(i % 3 != 0),  # 66% success rate
            duration=1.5 + (i % 5) * 0.2,
            parameters_used={"template": "default" if i < 10 else "premium"}
        )
        feedback.record_result(result)
    
    # Get statistics
    stats = feedback.get_stats("email")
    print(f"\nExecution Statistics:")
    print(json.dumps(stats, indent=2))
    
    # Identify patterns
    patterns = feedback.identify_patterns("email")
    print(f"\nIdentified Patterns:")
    for pattern in patterns:
        print(f"  - {pattern.pattern_type}: {pattern.description}")
    
    # Get improvement suggestions
    suggestions = feedback.get_improvement_suggestions("email")
    print(f"\nImprovement Suggestions:")
    for suggestion in suggestions:
        print(f"  - {suggestion['type']}: {suggestion['recommendation']}")


def example_6_workflow():
    """Example 6: Multi-step workflow execution"""
    print("\n" + "="*60)
    print("EXAMPLE 6: Multi-Step Workflow")
    print("="*60)
    
    orchestrator = LLMOrchestrator(OrchestrationConfig(enable_claude=False))
    session = orchestrator.context_mgr.create_session("workflow_user")
    session_id = session.id
    
    # Start workflow
    workflow = orchestrator.start_workflow(
        session_id,
        workflow_type="email_campaign",
        steps=["draft", "review", "schedule", "publish", "monitor"]
    )
    print(f"\nStarted workflow: {workflow['workflow_id']}")
    print(f"Steps: {workflow['steps']}")
    
    # Execute steps
    for step in workflow["steps"][:2]:  # Execute first 2 steps
        result = orchestrator.execute_workflow_step(
            session_id,
            workflow["workflow_id"],
            step,
            executor_fn=lambda **kwargs: {"status": "done", "step": kwargs["step"]},
            context={"campaign_id": "camp_123"}
        )
        print(f"\nCompleted step: {step}")
        print(f"Result: {result}")


def example_7_full_scenario():
    """Example 7: Complete real-world scenario"""
    print("\n" + "="*60)
    print("EXAMPLE 7: Complete Prospection Workflow")
    print("="*60)
    
    # Initialize orchestrator
    config = OrchestrationConfig(
        enable_claude=False,  # Disabled for demo
        learning_enabled=True,
        log_level="INFO"
    )
    orchestrator = LLMOrchestrator(config)
    
    # Scenario: Email from prospect arrives
    prospect_email = {
        "source": "email",
        "sender": "boss@aicorp.com",
        "subject": "Exploring AI Solutions for Our Team",
        "content": """Hi JARVIS Team,
        
I'm the VP of Operations at AI Corp, and I'm impressed by your AI orchestration platform.
We're looking to implement an intelligent routing system for our customer support.

Would you be available for a call this week?

Best regards,
Jane Smith
AI Corp""",
        "system_state": {
            "crm_count": 385,
            "posts_published": 45,
            "engagement_rate": 8.2
        }
    }
    
    # Custom executor for demo
    def demo_executor(action, parameters, context, session_id):
        """Simulate task execution"""
        return {
            "status": "completed",
            "action": action,
            "sent_to": context.get("sender", "unknown"),
            "duration": 1.5
        }
    
    # Execute routing
    print("\nRouting incoming email...")
    result = orchestrator.route_and_execute(
        task_context=prospect_email,
        executor_fn=demo_executor,
        user_id="crm_system"
    )
    
    # Display result
    print(f"\nRouting Result:")
    print(f"  Status: {result['status']}")
    print(f"  Intent: {result['routing_decision']['task_type']}")
    print(f"  Confidence: {result['routing_decision']['confidence']:.2f}")
    print(f"  Suggested Actions: {result['routing_decision']['suggested_actions']}")
    print(f"  Decision: {result['decision']['action']}")
    
    if result.get('execution_result'):
        print(f"\nExecution Details:")
        print(f"  Action: {result['execution_result'].get('action')}")
        print(f"  Duration: {result['execution_result'].get('duration')}s")
    
    # Get learning stats
    print(f"\nLearning Statistics:")
    stats = orchestrator.get_learning_stats("prospection")
    print(f"  Total Executions: {len(orchestrator.feedback.results)}")
    print(f"  Decisions Made: {len(orchestrator.engine.decision_history)}")


def main():
    """Run all examples"""
    print("\n" + "█" * 60)
    print("LLM ORCHESTRATOR ROUTER - QUICK START GUIDE")
    print("█" * 60)
    
    examples = [
        example_1_basic_routing,
        example_2_context_tracking,
        example_3_error_diagnosis,
        example_4_decision_engine,
        example_5_learning_loop,
        example_6_workflow,
        example_7_full_scenario
    ]
    
    for example_fn in examples:
        try:
            example_fn()
        except Exception as e:
            logger.error(f"Example error: {e}", exc_info=True)
    
    print("\n" + "█" * 60)
    print("Examples completed!")
    print("█" * 60 + "\n")


if __name__ == "__main__":
    main()
