"""
Feedback Loop - Learning and adaptation system
Tracks execution results, identifies patterns, and adapts decision thresholds.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import statistics
import logging


logger = logging.getLogger(__name__)


@dataclass
class ExecutionMetrics:
    """Metrics for task execution"""
    task_type: str
    success: bool
    duration: float  # seconds
    parameters_used: Dict[str, Any]
    confidence_level: float = 1.0
    was_escalated: bool = False
    result_quality: float = 1.0  # 0-1
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class LearningPattern:
    """Discovered pattern for learning"""
    pattern_type: str
    description: str
    evidence_count: int
    success_rate: float
    confidence: float
    parameters: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class FeedbackLoop:
    """
    Learning system that tracks execution and adapts decisions.
    
    Responsibilities:
    - Record execution results
    - Calculate success rates
    - Identify patterns
    - Generate improvement suggestions
    - Adapt confidence thresholds
    - Learn from feedback
    
    Example:
        feedback = FeedbackLoop()
        result = ExecutionResult(task_type="email", success=True)
        feedback.record_result(result)
        stats = feedback.get_stats("email")
        patterns = feedback.identify_patterns("email")
    """
    
    def __init__(self, min_samples_for_pattern: int = 10):
        """
        Initialize feedback loop.
        
        Args:
            min_samples_for_pattern: Minimum samples needed to identify patterns
        """
        self.results: List[Any] = []  # Execution results
        self.decisions: List[Dict[str, Any]] = []  # Decision records
        self.patterns: Dict[str, List[LearningPattern]] = defaultdict(list)
        self.min_samples = min_samples_for_pattern
        
        # Adaptive thresholds per intent
        self.confidence_thresholds = defaultdict(lambda: 0.6)
        
        # Pattern cache
        self._pattern_cache = {}
        self._cache_timestamp = None
    
    def record_result(self, result: Any):
        """
        Record task execution result.
        
        Args:
            result: ExecutionResult or similar with task_type, success fields
        """
        # Convert to common format if needed
        if not hasattr(result, "task_type"):
            # If it's a dict, convert to object-like
            result = type('Result', (), result)()
        
        self.results.append(result)
        logger.debug(f"Recorded result: {result.task_type} - {result.success}")
        
        # Invalidate cache
        self._pattern_cache.clear()
        self._cache_timestamp = None
    
    def record_decision(self, intent: str, confidence: float, was_correct: bool):
        """
        Record a decision and its correctness.
        
        Args:
            intent: Task intent
            confidence: Confidence of decision
            was_correct: Whether decision was ultimately correct
        """
        self.decisions.append({
            "intent": intent,
            "confidence": confidence,
            "was_correct": was_correct,
            "timestamp": datetime.now()
        })
    
    def get_stats(self, task_type: str, days_back: int = 30) -> Dict[str, Any]:
        """
        Calculate statistics for task type.
        
        Args:
            task_type: Type of task to analyze
            days_back: Number of days to look back
            
        Returns:
            Dict with success_rate, avg_duration, count, etc.
        """
        cutoff = datetime.now() - timedelta(days=days_back)
        
        # Filter results
        relevant = [
            r for r in self.results
            if r.task_type == task_type and r.timestamp > cutoff
        ]
        
        if not relevant:
            return {
                "task_type": task_type,
                "count": 0,
                "success_rate": 0,
                "avg_duration": 0,
                "status": "insufficient_data"
            }
        
        successes = sum(1 for r in relevant if r.success)
        success_rate = successes / len(relevant)
        
        durations = [r.duration for r in relevant if hasattr(r, "duration")]
        avg_duration = statistics.mean(durations) if durations else 0
        
        # Recent trend
        recent = relevant[-5:] if len(relevant) > 5 else relevant
        recent_success = sum(1 for r in recent if r.success) / len(recent)
        
        return {
            "task_type": task_type,
            "count": len(relevant),
            "success_rate": success_rate,
            "success_count": successes,
            "avg_duration": avg_duration,
            "recent_success_rate": recent_success,
            "trend": "improving" if recent_success > success_rate else "declining",
            "last_execution": relevant[-1].timestamp.isoformat() if relevant else None
        }
    
    def identify_patterns(self, task_type: str) -> List[LearningPattern]:
        """
        Identify success patterns for task type.
        
        Args:
            task_type: Task type to analyze
            
        Returns:
            List of LearningPattern objects
        """
        # Check cache
        if task_type in self._pattern_cache:
            cache_age = datetime.now() - self._cache_timestamp
            if cache_age < timedelta(minutes=5):
                return self._pattern_cache[task_type]
        
        # Filter relevant results
        relevant = [r for r in self.results if r.task_type == task_type]
        
        if len(relevant) < self.min_samples:
            logger.debug(f"Insufficient samples for {task_type}: {len(relevant)}/{self.min_samples}")
            return []
        
        patterns = []
        
        # Pattern 1: Parameter combinations
        patterns.extend(self._find_parameter_patterns(relevant, task_type))
        
        # Pattern 2: Time-based patterns
        patterns.extend(self._find_temporal_patterns(relevant, task_type))
        
        # Pattern 3: Success correlations
        patterns.extend(self._find_success_correlations(relevant, task_type))
        
        # Cache results
        self._pattern_cache[task_type] = patterns
        self._cache_timestamp = datetime.now()
        
        return patterns
    
    def _find_parameter_patterns(self, results: List[Any], task_type: str) -> List[LearningPattern]:
        """Find patterns in successful parameter combinations"""
        patterns = []
        
        # Group by parameters
        param_success = defaultdict(lambda: {"success": 0, "total": 0})
        
        for result in results:
            if not hasattr(result, "parameters_used"):
                continue
            
            params_key = str(sorted(result.parameters_used.items()))
            param_success[params_key]["total"] += 1
            if result.success:
                param_success[params_key]["success"] += 1
        
        # Find high-success parameter combos
        for params_str, counts in param_success.items():
            if counts["total"] >= 5:
                success_rate = counts["success"] / counts["total"]
                if success_rate > 0.8:
                    patterns.append(LearningPattern(
                        pattern_type="parameter_combo",
                        description=f"Parameter combination with {success_rate:.0%} success rate",
                        evidence_count=counts["total"],
                        success_rate=success_rate,
                        confidence=0.85,
                        parameters={"combo": params_str}
                    ))
        
        return patterns
    
    def _find_temporal_patterns(self, results: List[Any], task_type: str) -> List[LearningPattern]:
        """Find time-based patterns (morning vs evening, etc.)"""
        patterns = []
        
        # Group by hour of day
        hour_success = defaultdict(lambda: {"success": 0, "total": 0})
        
        for result in results:
            hour = result.timestamp.hour
            hour_success[hour]["total"] += 1
            if result.success:
                hour_success[hour]["success"] += 1
        
        # Find peak hours
        if hour_success:
            peak_hour = max(hour_success, key=lambda h: hour_success[h]["success"] / max(1, hour_success[h]["total"]))
            peak_success = (hour_success[peak_hour]["success"] / hour_success[peak_hour]["total"])
            
            if peak_success > 0.75:
                patterns.append(LearningPattern(
                    pattern_type="temporal",
                    description=f"Higher success rate around hour {peak_hour} ({peak_success:.0%})",
                    evidence_count=hour_success[peak_hour]["total"],
                    success_rate=peak_success,
                    confidence=0.8,
                    parameters={"optimal_hour": peak_hour}
                ))
        
        return patterns
    
    def _find_success_correlations(self, results: List[Any], task_type: str) -> List[LearningPattern]:
        """Find factors correlated with success"""
        patterns = []
        
        # Check duration correlation
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        if successful and failed:
            success_durations = [r.duration for r in successful if hasattr(r, "duration")]
            fail_durations = [r.duration for r in failed if hasattr(r, "duration")]
            
            if success_durations and fail_durations:
                avg_success_duration = statistics.mean(success_durations)
                avg_fail_duration = statistics.mean(fail_durations)
                
                if abs(avg_success_duration - avg_fail_duration) > 1.0:
                    difference = abs(avg_success_duration - avg_fail_duration)
                    patterns.append(LearningPattern(
                        pattern_type="duration_correlation",
                        description=f"Success duration {avg_success_duration:.1f}s vs fail {avg_fail_duration:.1f}s",
                        evidence_count=len(successful),
                        success_rate=len(successful) / len(results),
                        confidence=0.75,
                        parameters={
                            "optimal_duration": avg_success_duration,
                            "difference": difference
                        }
                    ))
        
        return patterns
    
    def get_improvement_suggestions(self, task_type: str) -> List[Dict[str, Any]]:
        """
        Generate improvement suggestions based on patterns.
        
        Args:
            task_type: Task type to analyze
            
        Returns:
            List of suggestions
        """
        suggestions = []
        stats = self.get_stats(task_type)
        patterns = self.identify_patterns(task_type)
        
        # Poor success rate
        if stats.get("success_rate", 0) < 0.7:
            suggestions.append({
                "priority": "high",
                "type": "improve_success_rate",
                "issue": f"Low success rate: {stats['success_rate']:.0%}",
                "recommendation": "Review error patterns and adjust parameters",
                "potential_impact": "Could improve success to 85%+"
            })
        
        # Declining trend
        if stats.get("trend") == "declining":
            suggestions.append({
                "priority": "high",
                "type": "reverse_decline",
                "issue": "Execution success is declining",
                "recommendation": "Identify recent changes that may have caused degradation",
                "potential_impact": "Restore previous success levels"
            })
        
        # Use patterns for specific suggestions
        for pattern in patterns:
            if pattern.pattern_type == "temporal" and pattern.success_rate > 0.85:
                suggestions.append({
                    "priority": "medium",
                    "type": "schedule_optimization",
                    "issue": f"Optimal timing discovered",
                    "recommendation": f"Schedule {task_type} tasks around hour {pattern.parameters.get('optimal_hour')}",
                    "potential_impact": "Increase success rate by 10-15%"
                })
            
            elif pattern.pattern_type == "duration_correlation":
                suggestions.append({
                    "priority": "medium",
                    "type": "duration_optimization",
                    "issue": f"Duration correlates with success",
                    "recommendation": f"Optimize for {pattern.parameters.get('optimal_duration'):.1f}s execution time",
                    "potential_impact": "Increase success rate by 5-10%"
                })
        
        return suggestions
    
    def get_adapted_threshold(self, intent: str) -> float:
        """
        Get adapted confidence threshold for intent.
        
        Uses decision accuracy history to lower thresholds
        for consistently correct low-confidence decisions.
        
        Args:
            intent: Task intent
            
        Returns:
            Adapted confidence threshold (0-1)
        """
        intent_decisions = [d for d in self.decisions if d["intent"] == intent]
        
        if not intent_decisions:
            return self.confidence_thresholds[intent]
        
        # Find low-confidence decisions that were correct
        low_conf_correct = sum(
            1 for d in intent_decisions
            if d["confidence"] < 0.6 and d["was_correct"]
        )
        
        low_conf_total = sum(1 for d in intent_decisions if d["confidence"] < 0.6)
        
        # If many low-confidence decisions are correct, lower threshold
        if low_conf_total > 0:
            low_conf_accuracy = low_conf_correct / low_conf_total
            if low_conf_accuracy > 0.8:
                new_threshold = 0.4
            elif low_conf_accuracy > 0.7:
                new_threshold = 0.5
            else:
                new_threshold = self.confidence_thresholds[intent]
        else:
            new_threshold = self.confidence_thresholds[intent]
        
        self.confidence_thresholds[intent] = new_threshold
        logger.info(f"Adapted threshold for {intent}: {new_threshold:.2f}")
        
        return new_threshold
    
    def generate_learning_report(self, task_type: str = None) -> Dict[str, Any]:
        """
        Generate comprehensive learning report.
        
        Args:
            task_type: Optional specific task type
            
        Returns:
            Detailed learning analysis
        """
        report = {
            "generated_at": datetime.now().isoformat(),
            "task_type": task_type or "all",
            "total_executions": len(self.results),
            "total_decisions": len(self.decisions)
        }
        
        if task_type:
            report["stats"] = self.get_stats(task_type)
            report["patterns"] = [
                {
                    "type": p.pattern_type,
                    "description": p.description,
                    "success_rate": p.success_rate,
                    "confidence": p.confidence,
                    "samples": p.evidence_count
                }
                for p in self.identify_patterns(task_type)
            ]
            report["suggestions"] = self.get_improvement_suggestions(task_type)
        else:
            # All task types
            all_types = set(r.task_type for r in self.results)
            report["task_types"] = {
                task: self.get_stats(task)
                for task in all_types
            }
        
        return report
    
    def export_patterns(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Export all discovered patterns.
        
        Returns:
            Dict mapping task types to pattern lists
        """
        export = {}
        
        all_types = set(r.task_type for r in self.results)
        for task_type in all_types:
            patterns = self.identify_patterns(task_type)
            export[task_type] = [
                {
                    "type": p.pattern_type,
                    "description": p.description,
                    "success_rate": p.success_rate,
                    "confidence": p.confidence,
                    "samples": p.evidence_count,
                    "parameters": p.parameters
                }
                for p in patterns
            ]
        
        return export


__all__ = ["FeedbackLoop", "ExecutionMetrics", "LearningPattern"]
