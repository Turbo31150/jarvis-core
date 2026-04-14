"""
Decision Engine - Rule evaluation and decision logic
Handles IF-THEN rules, LLM-based decisions, and escalation logic.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Callable, Optional
from enum import Enum
from datetime import datetime, timedelta
import logging


logger = logging.getLogger(__name__)


class RuleType(Enum):
    """Types of decision rules"""
    CONDITION_BASED = "condition_based"  # IF condition THEN action
    THRESHOLD_BASED = "threshold_based"  # IF metric > threshold THEN action
    TIME_BASED = "time_based"  # IF time condition THEN action
    STATE_BASED = "state_based"  # IF state matches THEN action
    PATTERN_BASED = "pattern_based"  # IF pattern detected THEN action
    HYBRID = "hybrid"  # Multiple conditions


@dataclass
class Rule:
    """Decision rule"""
    name: str
    condition: Callable[[Dict[str, Any]], bool]
    action: str
    threshold: float = 0.8  # Confidence threshold
    priority: int = 1  # 1=highest, 10=lowest
    rule_type: RuleType = RuleType.CONDITION_BASED
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __lt__(self, other):
        """For sorting by priority"""
        return self.threshold > other.threshold


@dataclass
class DecisionResult:
    """Result of decision making"""
    action: str
    confidence: float
    reasoning: str
    escalate: bool = False
    rules_matched: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class DecisionEngine:
    """
    Decision engine for intelligent routing and action selection.
    
    Responsibilities:
    - Evaluate IF-THEN rules
    - Handle conflicting rules
    - Escalate ambiguous decisions
    - Track circuit breaker state
    - Learn from results
    
    Example:
        engine = DecisionEngine()
        decision = engine.make_decision(
            intent="prospection",
            confidence=0.85,
            options=["send_email", "add_crm"]
        )
    """
    
    # Escalation thresholds
    ESCALATION_CONFIDENCE_THRESHOLD = 0.6
    AUTO_CORRECTION_THRESHOLD = 0.95
    
    # Circuit breaker settings
    CIRCUIT_BREAKER_THRESHOLD = 5  # failures before opening
    CIRCUIT_BREAKER_RESET_MINUTES = 5
    
    def __init__(self):
        """Initialize decision engine"""
        self.rules: Dict[str, List[Rule]] = {}
        self.decision_history: List[Dict[str, Any]] = []
        self.circuit_breakers: Dict[str, Dict[str, Any]] = {}
        self._init_default_rules()
    
    def _init_default_rules(self):
        """Initialize default decision rules"""
        self._register_rules("error_handling", [
            Rule(
                name="api_timeout",
                condition=lambda s: s.get("error_type") == "api_timeout",
                action="retry_with_backoff",
                threshold=0.8,
                rule_type=RuleType.CONDITION_BASED
            ),
            Rule(
                name="rate_limit",
                condition=lambda s: s.get("error_type") == "rate_limit",
                action="queue_and_delay",
                threshold=0.9,
                rule_type=RuleType.CONDITION_BASED
            ),
            Rule(
                name="auth_error",
                condition=lambda s: s.get("error_type") == "auth_error",
                action="escalate_to_human",
                threshold=0.95,
                rule_type=RuleType.CONDITION_BASED
            ),
        ])
        
        self._register_rules("performance", [
            Rule(
                name="high_engagement_drop",
                condition=lambda s: s.get("engagement_drop_pct", 0) > 30,
                action="increase_posting_frequency",
                threshold=0.85,
                rule_type=RuleType.THRESHOLD_BASED
            ),
            Rule(
                name="moderate_drop",
                condition=lambda s: 10 < s.get("engagement_drop_pct", 0) <= 30,
                action="adjust_content_type",
                threshold=0.75,
                rule_type=RuleType.THRESHOLD_BASED
            ),
        ])
    
    def _register_rules(self, category: str, rules: List[Rule]):
        """Register rules for a category"""
        if category not in self.rules:
            self.rules[category] = []
        self.rules[category].extend(rules)
    
    def evaluate_rule(self, rule: Rule, state: Dict[str, Any]) -> bool:
        """
        Evaluate single rule against state.
        
        Args:
            rule: Rule to evaluate
            state: Current state
            
        Returns:
            True if rule matches
        """
        try:
            return rule.condition(state)
        except Exception as e:
            logger.error(f"Error evaluating rule {rule.name}: {e}")
            return False
    
    def match_rules(self, rules: List[Rule], state: Dict[str, Any]) -> List[Rule]:
        """
        Find matching rules in order of confidence.
        
        Args:
            rules: Rules to evaluate
            state: Current state
            
        Returns:
            List of matching rules sorted by threshold (highest first)
        """
        matched = [r for r in rules if self.evaluate_rule(r, state)]
        
        # Sort by threshold (confidence) descending
        matched.sort(key=lambda r: r.threshold, reverse=True)
        
        return matched
    
    def make_decision(self, intent: str, confidence: float, 
                     options: List[str] = None, state: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Make decision based on intent and confidence.
        
        Args:
            intent: Detected intent
            confidence: Confidence score (0-1)
            options: Available action options
            state: Current system state
            
        Returns:
            Dict with action, escalate flag, etc.
        """
        state = state or {}
        options = options or []
        
        # Check circuit breaker
        if self.is_circuit_open(intent):
            return {
                "action": "circuit_breaker_open",
                "escalate": True,
                "reason": f"Circuit breaker open for {intent}",
                "confidence": 1.0
            }
        
        # Low confidence → escalate
        if confidence < self.ESCALATION_CONFIDENCE_THRESHOLD:
            return {
                "action": "escalate_to_human",
                "escalate": True,
                "reason": f"Low confidence ({confidence:.2f})",
                "confidence": confidence
            }
        
        # Check category-specific rules
        if intent in self.rules:
            matched = self.match_rules(self.rules[intent], state)
            
            if matched:
                rule = matched[0]
                
                # Very high confidence → auto correct
                if rule.threshold >= self.AUTO_CORRECTION_THRESHOLD:
                    decision = {
                        "action": rule.action,
                        "escalate": False,
                        "reason": f"Auto-correction (confidence {rule.threshold})",
                        "confidence": rule.threshold,
                        "rule_matched": rule.name,
                        "alternatives": [r.action for r in matched[1:3]]
                    }
                else:
                    decision = {
                        "action": rule.action,
                        "escalate": False,
                        "reason": f"Matched rule: {rule.name}",
                        "confidence": rule.threshold,
                        "rule_matched": rule.name,
                        "alternatives": [r.action for r in matched[1:3]]
                    }
                
                self._record_decision(decision, intent, confidence)
                return decision
        
        # No rules matched, use confidence-based decision
        decision = {
            "action": options[0] if options else "escalate_to_human",
            "escalate": confidence < 0.8,
            "reason": "No matching rules, using confidence-based decision",
            "confidence": confidence,
            "alternatives": options[1:] if len(options) > 1 else []
        }
        
        self._record_decision(decision, intent, confidence)
        return decision
    
    def _record_decision(self, decision: Dict[str, Any], intent: str, confidence: float):
        """Record decision for learning"""
        self.decision_history.append({
            "timestamp": datetime.now().isoformat(),
            "intent": intent,
            "confidence": confidence,
            "decision": decision
        })
        
        # Keep only recent decisions
        if len(self.decision_history) > 1000:
            self.decision_history = self.decision_history[-1000:]
    
    def record_failure(self, context: str, count: int = 1):
        """
        Record failure for circuit breaker.
        
        Args:
            context: Context/category of failure
            count: Number of failures
        """
        if context not in self.circuit_breakers:
            self.circuit_breakers[context] = {
                "failures": 0,
                "opened_at": None,
                "last_failure": None
            }
        
        cb = self.circuit_breakers[context]
        cb["failures"] += count
        cb["last_failure"] = datetime.now()
        
        logger.warning(f"Failure recorded for {context}: {cb['failures']} total")
    
    def record_success(self, context: str):
        """
        Record success and reset failures.
        
        Args:
            context: Context/category
        """
        if context in self.circuit_breakers:
            self.circuit_breakers[context]["failures"] = 0
    
    def is_circuit_open(self, context: str) -> bool:
        """
        Check if circuit breaker is open.
        
        Args:
            context: Context to check
            
        Returns:
            True if circuit is open
        """
        if context not in self.circuit_breakers:
            return False
        
        cb = self.circuit_breakers[context]
        
        # Not open yet
        if cb["failures"] < self.CIRCUIT_BREAKER_THRESHOLD:
            return False
        
        # Open, check if should reset
        if cb["opened_at"] is None:
            cb["opened_at"] = datetime.now()
            logger.warning(f"Circuit breaker opened for {context}")
            return True
        
        # Check if reset period elapsed
        age = datetime.now() - cb["opened_at"]
        if age > timedelta(minutes=self.CIRCUIT_BREAKER_RESET_MINUTES):
            logger.info(f"Resetting circuit breaker for {context}")
            self.circuit_breakers[context] = {
                "failures": 0,
                "opened_at": None,
                "last_failure": None
            }
            return False
        
        return True
    
    def add_rule(self, category: str, rule: Rule):
        """
        Add custom rule.
        
        Args:
            category: Rule category
            rule: Rule to add
        """
        if category not in self.rules:
            self.rules[category] = []
        
        self.rules[category].append(rule)
        logger.info(f"Added rule {rule.name} to category {category}")
    
    def remove_rule(self, category: str, rule_name: str) -> bool:
        """
        Remove rule by name.
        
        Args:
            category: Rule category
            rule_name: Name of rule to remove
            
        Returns:
            True if removed, False if not found
        """
        if category not in self.rules:
            return False
        
        before = len(self.rules[category])
        self.rules[category] = [r for r in self.rules[category] if r.name != rule_name]
        
        removed = len(self.rules[category]) < before
        if removed:
            logger.info(f"Removed rule {rule_name} from category {category}")
        
        return removed
    
    def get_decision_stats(self, intent: str = None) -> Dict[str, Any]:
        """
        Get statistics on past decisions.
        
        Args:
            intent: Optional specific intent to filter
            
        Returns:
            Dict with decision statistics
        """
        decisions = self.decision_history
        
        if intent:
            decisions = [d for d in decisions if d["intent"] == intent]
        
        if not decisions:
            return {"total": 0}
        
        escalations = sum(1 for d in decisions if d["decision"].get("escalate"))
        auto_corrections = sum(1 for d in decisions 
                             if d["decision"].get("confidence", 0) >= self.AUTO_CORRECTION_THRESHOLD)
        
        avg_confidence = sum(d["confidence"] for d in decisions) / len(decisions)
        
        return {
            "total_decisions": len(decisions),
            "escalations": escalations,
            "escalation_rate": escalations / len(decisions) if decisions else 0,
            "auto_corrections": auto_corrections,
            "average_confidence": avg_confidence,
            "recent_decisions": decisions[-10:]
        }
    
    def handle_decision_feedback(self, decision_id: int, was_correct: bool, 
                                feedback: str = ""):
        """
        Record feedback on a decision for learning.
        
        Args:
            decision_id: Index in decision history
            was_correct: Whether decision was correct
            feedback: Optional feedback text
        """
        if decision_id < len(self.decision_history):
            self.decision_history[decision_id]["feedback"] = {
                "correct": was_correct,
                "timestamp": datetime.now().isoformat(),
                "comment": feedback
            }
            logger.info(f"Recorded feedback for decision {decision_id}: {was_correct}")
    
    def suggest_rule_adjustments(self) -> List[Dict[str, Any]]:
        """
        Suggest rule adjustments based on decision history.
        
        Returns:
            List of suggested adjustments
        """
        suggestions = []
        
        # Analyze escalations
        high_escalation_intents = {}
        for decision in self.decision_history:
            intent = decision["intent"]
            if decision["decision"].get("escalate"):
                high_escalation_intents[intent] = high_escalation_intents.get(intent, 0) + 1
        
        for intent, count in high_escalation_intents.items():
            escalation_rate = count / max(1, len([d for d in self.decision_history if d["intent"] == intent]))
            if escalation_rate > 0.3:
                suggestions.append({
                    "type": "lower_threshold",
                    "intent": intent,
                    "reason": f"High escalation rate: {escalation_rate:.1%}",
                    "suggested_action": f"Lower confidence threshold for {intent}"
                })
        
        return suggestions


class ConflictResolver:
    """Resolves conflicts when multiple rules match"""
    
    @staticmethod
    def resolve_by_priority(rules: List[Rule]) -> Rule:
        """Pick highest priority rule"""
        return min(rules, key=lambda r: r.priority)
    
    @staticmethod
    def resolve_by_confidence(rules: List[Rule]) -> Rule:
        """Pick highest confidence rule"""
        return max(rules, key=lambda r: r.threshold)
    
    @staticmethod
    def resolve_by_specificity(rules: List[Rule]) -> Rule:
        """Pick most specific rule (guess based on metadata)"""
        scored = [(r, r.metadata.get("specificity", 0)) for r in rules]
        return max(scored, key=lambda x: x[1])[0]


__all__ = ["DecisionEngine", "Rule", "RuleType", "DecisionResult", "ConflictResolver"]
