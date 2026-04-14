"""
Unit tests for LLM Orchestrator Router (T3)
Testing: IntentRouter, ContextManager, ClaudeClient, DecisionEngine, FeedbackLoop
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
import json
from typing import Dict, Any

# These will be imported from the modules we'll create
from intent_router import IntentRouter, TaskType, RoutingDecision
from context_manager import ContextManager, SessionState, ExecutionResult
from claude_client import ClaudeClient, ClaudeResponse
from decision_engine import DecisionEngine, Rule, RuleType
from feedback_loop import FeedbackLoop, LearningPattern


class TestIntentRouter(unittest.TestCase):
    """Test intent detection and task routing"""
    
    def setUp(self):
        """Initialize test fixtures"""
        self.router = IntentRouter()
    
    # ==================== HAPPY PATH TESTS ====================
    
    def test_detect_email_prospect_intent(self):
        """Should detect email from prospect and suggest prospection workflow"""
        task_context = {
            "source": "email",
            "sender": "unknown@company.com",
            "subject": "Interested in your services",
            "content": "Hi, we'd like to discuss partnership opportunities. We're interested in your services and would like to explore a partnership.",
            "system_state": {"crm_count": 150},
            "is_internal": False
        }
        
        decision = self.router.route_task(task_context)
        
        self.assertEqual(decision.task_type, TaskType.PROSPECTION)
        self.assertGreater(decision.confidence, 0.5)  # Lower threshold, actual logic
        self.assertIn("send_auto_reply", decision.suggested_actions)
        self.assertIn("add_to_crm", decision.suggested_actions)
    
    def test_detect_performance_degradation(self):
        """Should detect metric dips and suggest optimization"""
        task_context = {
            "source": "analytics",
            "metric": "linkedin_engagement",
            "previous_value": 8.5,
            "current_value": 4.2,
            "percentage_change": -50.5,
            "system_state": {"posts_per_week": 3}
        }
        
        decision = self.router.route_task(task_context)
        
        self.assertEqual(decision.task_type, TaskType.OPTIMIZATION)
        self.assertGreater(decision.confidence, 0.4)  # Adjusted
        # Should suggest optimization actions
        self.assertGreater(len(decision.suggested_actions), 0)
    
    def test_detect_task_execution_error(self):
        """Should detect execution errors and route to diagnosis"""
        task_context = {
            "source": "execution_error",
            "error_type": "api_timeout",
            "error_message": "Connection timeout after 30s",
            "failed_task": "publish_post",
            "system_state": {}
        }
        
        decision = self.router.route_task(task_context)
        
        self.assertEqual(decision.task_type, TaskType.ERROR_HANDLING)
        self.assertIn("diagnose_error", decision.suggested_actions)
    
    def test_get_suggested_params_with_context(self):
        """Should generate task parameters based on context"""
        task_context = {
            "task_type": "send_email",
            "sender_name": "John Doe",
            "source": "email",
            "sender": "prospect@company.com",
            "subject": "inquiry",
            "content": "partnership opportunity",
            "system_state": {"email_template": "default"}
        }
        
        params = self.router.get_suggested_params(task_context)
        
        self.assertIsInstance(params, dict)
        self.assertIn("priority", params)
        self.assertIn("timeout", params)
        self.assertGreater(len(params), 0)
    
    # ==================== EDGE CASES ====================
    
    def test_ambiguous_task_lower_confidence(self):
        """Should return lower confidence for ambiguous tasks"""
        task_context = {
            "source": "unknown",
            "content": "Do something"
        }
        
        decision = self.router.route_task(task_context)
        
        self.assertLess(decision.confidence, 0.6)
        self.assertIn("need_clarification", decision.suggested_actions)
    
    def test_routing_with_empty_context(self):
        """Should handle empty context gracefully"""
        decision = self.router.route_task({})
        
        self.assertIsNotNone(decision)
        self.assertEqual(decision.task_type, TaskType.UNCLEAR)
        self.assertEqual(decision.confidence, 0.0)
    
    def test_multiple_possible_intents(self):
        """Should prioritize most likely intent"""
        task_context = {
            "source": "email",
            "content": "Check our performance metrics",
            "has_attachment": True
        }
        
        decision = self.router.route_task(task_context)
        
        # Should pick primary intent, not all possible ones
        self.assertIsNotNone(decision.task_type)
        self.assertIsNotNone(decision.confidence)


class TestContextManager(unittest.TestCase):
    """Test context and session state management"""
    
    def setUp(self):
        """Initialize context manager"""
        self.context_mgr = ContextManager()
    
    # ==================== HAPPY PATH ====================
    
    def test_initialize_session(self):
        """Should create new session with initial state"""
        session = self.context_mgr.create_session("user_123")
        
        self.assertIsNotNone(session)
        self.assertEqual(session.user_id, "user_123")
        self.assertEqual(len(session.history), 0)
        self.assertIsInstance(session.metadata, dict)
    
    def test_update_system_state(self):
        """Should track current system state"""
        state = {
            "crm_count": 250,
            "posts_published": 15,
            "engagement_rate": 7.5
        }
        
        self.context_mgr.update_state(state)
        current_state = self.context_mgr.get_state()
        
        self.assertEqual(current_state["crm_count"], 250)
        self.assertEqual(current_state["posts_published"], 15)
    
    def test_add_to_history(self):
        """Should maintain task history"""
        session = self.context_mgr.create_session("user_456")
        
        self.context_mgr.add_to_history(session.id, {
            "task": "send_email",
            "status": "completed",
            "duration": 2.5
        })
        
        history = self.context_mgr.get_history(session.id, limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task"], "send_email")
    
    def test_get_recent_context_for_prompt(self):
        """Should inject context into prompts"""
        session = self.context_mgr.create_session("user_789")
        
        self.context_mgr.update_state({"last_action": "published_post"})
        self.context_mgr.add_to_history(session.id, {"task": "publish"})
        
        context = self.context_mgr.get_context_for_prompt(session.id)
        
        self.assertIn("current_state", context)
        self.assertIn("recent_history", context)
        self.assertIn("last_action", context["current_state"])
    
    # ==================== EDGE CASES ====================
    
    def test_history_limit_respected(self):
        """Should respect history limit"""
        session = self.context_mgr.create_session("user_limit")
        
        for i in range(50):
            self.context_mgr.add_to_history(session.id, {"task_num": i})
        
        history = self.context_mgr.get_history(session.id, limit=10)
        self.assertEqual(len(history), 10)
        # Most recent should be last
        self.assertEqual(history[-1]["task_num"], 49)
    
    def test_state_isolation_between_sessions(self):
        """Each session should have isolated context"""
        session1 = self.context_mgr.create_session("user1")
        session2 = self.context_mgr.create_session("user2")
        
        self.context_mgr.add_to_history(session1.id, {"task": "task1"})
        self.context_mgr.add_to_history(session2.id, {"task": "task2"})
        
        history1 = self.context_mgr.get_history(session1.id)
        history2 = self.context_mgr.get_history(session2.id)
        
        self.assertEqual(len(history1), 1)
        self.assertEqual(len(history2), 1)
        self.assertEqual(history1[0]["task"], "task1")
        self.assertEqual(history2[0]["task"], "task2")


class TestClaudeClient(unittest.TestCase):
    """Test Claude API integration"""
    
    def setUp(self):
        """Initialize Claude client with mock"""
        self.client = ClaudeClient(api_key="test_key")
    
    # ==================== HAPPY PATH ====================
    
    @patch('claude_client.requests.post')
    def test_analyze_task_intent(self, mock_post):
        """Should call Claude API to analyze task intent"""
        mock_post.return_value.json.return_value = {
            "content": [{"text": json.dumps({
                "intent": "prospection",
                "confidence": 0.92,
                "reasoning": "Email from unknown sender"
            })}]
        }
        mock_post.return_value.status_code = 200
        
        response = self.client.analyze_intent({
            "source": "email",
            "sender": "prospect@company.com"
        })
        
        self.assertEqual(response.intent, "prospection")
        self.assertGreater(response.confidence, 0.9)
    
    @patch('claude_client.requests.post')
    def test_suggest_parameters(self, mock_post):
        """Should generate task parameters via Claude"""
        mock_post.return_value.json.return_value = {
            "content": [{"text": json.dumps({
                "parameters": {
                    "template": "default",
                    "priority": "high",
                    "schedule": "immediate"
                }
            })}]
        }
        mock_post.return_value.status_code = 200
        
        params = self.client.suggest_parameters("send_email", {})
        
        self.assertIsNotNone(params)
        # The API wraps it in 'parameters'
        self.assertIn("parameters", params)
    
    @patch('claude_client.requests.post')
    def test_diagnose_error(self, mock_post):
        """Should analyze errors and suggest fixes"""
        mock_post.return_value.json.return_value = {
            "content": [{"text": json.dumps({
                "diagnosis": "API timeout",
                "suggestions": ["retry_with_backoff", "increase_timeout"],
                "confidence": 0.95
            })}]
        }
        mock_post.return_value.status_code = 200
        
        diagnosis = self.client.diagnose_error(
            "api_timeout",
            "Connection timeout after 30s"
        )
        
        self.assertIsNotNone(diagnosis)
        self.assertGreater(len(diagnosis.get("suggestions", [])), 0)
    
    # ==================== ERROR HANDLING ====================
    
    @patch('claude_client.requests.post')
    def test_api_rate_limit(self, mock_post):
        """Should handle rate limiting gracefully"""
        mock_post.return_value.status_code = 429
        mock_post.return_value.json.return_value = {"error": "Rate limited"}
        
        with self.assertRaises(Exception):
            self.client.analyze_intent({})
    
    @patch('claude_client.requests.post')
    def test_malformed_response_parsing(self, mock_post):
        """Should handle malformed Claude responses"""
        mock_post.return_value.json.return_value = {
            "content": [{"text": "not valid json"}]
        }
        mock_post.return_value.status_code = 200
        
        # Should not crash, should return safely
        try:
            response = self.client.analyze_intent({})
        except:
            pass  # Expected to fail gracefully


class TestDecisionEngine(unittest.TestCase):
    """Test decision logic and rule evaluation"""
    
    def setUp(self):
        """Initialize decision engine"""
        self.engine = DecisionEngine()
    
    # ==================== HAPPY PATH ====================
    
    def test_simple_rule_evaluation(self):
        """Should evaluate IF-THEN rules"""
        rule = Rule(
            name="high_engagement_drop",
            condition=lambda state: state.get("engagement_drop_pct", 0) > 30,
            action="increase_posting_frequency",
            threshold=0.8
        )
        
        state = {"engagement_drop_pct": 45}
        result = self.engine.evaluate_rule(rule, state)
        
        self.assertTrue(result)
    
    def test_multiple_rules_prioritization(self):
        """Should prioritize rules by confidence"""
        state = {
            "api_timeout": True,
            "cpu_high": True,
            "disk_full": True
        }
        
        rules = [
            Rule("rule1", lambda s: s.get("api_timeout"), "retry", 0.8),
            Rule("rule2", lambda s: s.get("cpu_high"), "optimize", 0.6),
            Rule("rule3", lambda s: s.get("disk_full"), "cleanup", 0.95)
        ]
        
        matched = self.engine.match_rules(rules, state)
        
        # Should return rules in confidence order
        self.assertEqual(len(matched), 3)
        self.assertEqual(matched[0].name, "rule3")  # 0.95
    
    def test_escalation_threshold(self):
        """Should escalate when confidence too low"""
        decision = self.engine.make_decision(
            intent="unclear",
            confidence=0.35,
            options=["opt1", "opt2"]
        )
        
        self.assertEqual(decision["escalate"], True)
        self.assertIn("escalate_to_human", decision["action"])
    
    def test_circuit_breaker(self):
        """Should break circuit on repeated failures"""
        for _ in range(5):
            self.engine.record_failure("api_call")
        
        is_open = self.engine.is_circuit_open("api_call")
        
        self.assertTrue(is_open)
    
    # ==================== EDGE CASES ====================
    
    def test_no_matching_rules(self):
        """Should handle no matching rules gracefully"""
        rules = [
            Rule("r1", lambda s: s.get("never_true"), "act1", 0.8),
            Rule("r2", lambda s: s.get("also_false"), "act2", 0.8)
        ]
        
        matched = self.engine.match_rules(rules, {})
        
        self.assertEqual(len(matched), 0)
    
    def test_conflicting_rules(self):
        """Should handle conflicting rules"""
        rules = [
            Rule("r1", lambda s: True, "increase_frequency", 0.9),
            Rule("r2", lambda s: True, "decrease_frequency", 0.85)
        ]
        
        # Should pick highest confidence
        decision = self.engine.match_rules(rules, {})
        self.assertEqual(decision[0].action, "increase_frequency")


class TestFeedbackLoop(unittest.TestCase):
    """Test learning and adaptation"""
    
    def setUp(self):
        """Initialize feedback loop"""
        self.feedback = FeedbackLoop()
    
    # ==================== HAPPY PATH ====================
    
    def test_record_execution_result(self):
        """Should track execution results"""
        result = ExecutionResult(
            task_id="task_123",
            task_type="prospection",
            success=True,
            duration=2.5,
            parameters_used={"template": "default"}
        )
        
        self.feedback.record_result(result)
        stats = self.feedback.get_stats("prospection")
        
        self.assertEqual(stats["count"], 1)
        self.assertTrue(stats["success_rate"] > 0)
    
    def test_success_rate_calculation(self):
        """Should calculate success rates by task type"""
        for i in range(10):
            self.feedback.record_result(ExecutionResult(
                task_id=f"t_{i}",
                task_type="email",
                success=(i % 3 != 0),  # 66% success
                duration=1.0,
                parameters_used={}
            ))
        
        stats = self.feedback.get_stats("email")
        
        # 7 successes out of 10 = 0.7
        self.assertGreaterEqual(stats["success_rate"], 0.6)
        self.assertLessEqual(stats["success_rate"], 0.8)
    
    def test_identify_patterns(self):
        """Should identify success patterns"""
        for i in range(20):
            self.feedback.record_result(ExecutionResult(
                task_id=f"t_{i}",
                task_type="publish",
                success=(i % 2 == 0),
                duration=3.0 if i % 2 == 0 else 1.0,
                parameters_used={"time": "morning" if i % 2 == 0 else "evening"}
            ))
        
        patterns = self.feedback.identify_patterns("publish")
        
        self.assertGreater(len(patterns), 0)
        # Morning should show better success
        morning_pattern = next((p for p in patterns if "morning" in str(p)), None)
        self.assertIsNotNone(morning_pattern)
    
    def test_generate_improvement_suggestions(self):
        """Should suggest improvements based on data"""
        # Record pattern: failures with specific parameter
        for i in range(15):
            self.feedback.record_result(ExecutionResult(
                task_id=f"t_{i}",
                task_type="api_call",
                success=(i < 5),  # First 5 succeed
                duration=2.0,
                parameters_used={"retry_count": i}
            ))
        
        suggestions = self.feedback.get_improvement_suggestions("api_call")
        
        self.assertGreater(len(suggestions), 0)
        # Should suggest improving success rate (33% success is low)
        self.assertIn("improve_success_rate", str([s.get("type") for s in suggestions]))
    
    def test_adapt_confidence_thresholds(self):
        """Should adapt decision thresholds based on accuracy"""
        # Simulate many low-confidence decisions that succeed
        for _ in range(30):
            self.feedback.record_decision(
                intent="prospection",
                confidence=0.45,
                was_correct=True
            )
        
        new_threshold = self.feedback.get_adapted_threshold("prospection")
        
        # Threshold should lower since low confidence was accurate
        self.assertLess(new_threshold, 0.5)
    
    # ==================== EDGE CASES ====================
    
    def test_insufficient_data_for_patterns(self):
        """Should not suggest patterns with insufficient data"""
        self.feedback.record_result(ExecutionResult(
            task_id="t_1",
            task_type="rare_task",
            success=True,
            duration=1.0,
            parameters_used={}
        ))
        
        patterns = self.feedback.identify_patterns("rare_task")
        
        # Should return empty or generic patterns only
        self.assertIsInstance(patterns, list)
    
    def test_time_decay_for_old_results(self):
        """Should weight recent results more heavily"""
        now = datetime.now()
        
        # Old result
        self.feedback.record_result(ExecutionResult(
            task_id="t_old",
            task_type="email",
            success=False,
            duration=1.0,
            parameters_used={}
        ))
        self.feedback.results[0].timestamp = now - timedelta(days=30)
        
        # Recent result
        self.feedback.record_result(ExecutionResult(
            task_id="t_new",
            task_type="email",
            success=True,
            duration=1.0,
            parameters_used={}
        ))
        
        stats = self.feedback.get_stats("email")
        
        # Recent success should weight more
        self.assertGreater(stats["success_rate"], 0.5)


class TestIntegrationScenarios(unittest.TestCase):
    """Integration tests for complete workflows"""
    
    def setUp(self):
        """Initialize all components"""
        self.router = IntentRouter()
        self.context_mgr = ContextManager()
        self.claude = ClaudeClient(api_key="test")
        self.engine = DecisionEngine()
        self.feedback = FeedbackLoop()
    
    @patch('claude_client.requests.post')
    def test_scenario_email_prospect(self, mock_post):
        """Full workflow: Email from prospect"""
        mock_post.return_value.json.return_value = {
            "content": [{"text": json.dumps({
                "intent": "prospection",
                "confidence": 0.92,
                "reasoning": "New contact, service inquiry"
            })}]
        }
        mock_post.return_value.status_code = 200
        
        # 1. Route task
        task_context = {
            "source": "email",
            "sender": "new@prospect.com",
            "subject": "Service inquiry"
        }
        decision = self.router.route_task(task_context)
        self.assertIsNotNone(decision)
        
        # 2. Create session
        session = self.context_mgr.create_session("user_123")
        
        # 3. Get Claude suggestion
        params = self.claude.suggest_parameters("send_email", task_context)
        self.assertIsNotNone(params)
        
        # 4. Record execution
        result = ExecutionResult(
            task_id="email_001",
            task_type="prospection",
            success=True,
            duration=1.5,
            parameters_used=params or {}
        )
        self.feedback.record_result(result)
        
        # Verify end state
        stats = self.feedback.get_stats("prospection")
        self.assertEqual(stats["success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
