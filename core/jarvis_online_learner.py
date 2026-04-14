"""
JARVIS Core Module: Online Learner
Version: 1.1.0
Role: Real-time feedback processing and strategy adaptation for JARVIS OMEGA.
"""

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.quality.online_learner")

class LearningSignal(Enum):
    EXPLICIT_FEEDBACK = "explicit_feedback"
    IMPLICIT_ENGAGEMENT = "implicit_engagement"
    ERROR_CORRECTION = "error_correction"
    PREFERENCE = "preference"
    OUTCOME = "outcome"

@dataclass
class LearningEvent:
    event_id: str
    signal_type: LearningSignal
    context: str
    prediction: str
    outcome: str
    reward: float
    timestamp: float = field(default_factory=time.time)

@dataclass
class AdaptationState:
    n_events: int = 0
    avg_reward: float = 0.0
    last_adapted_at: float = field(default_factory=time.time)
    weights: Dict[str, float] = field(default_factory=lambda: {
        "model_selection": 0.5,
        "context_depth": 0.5,
        "creativity": 0.5,
        "strictness": 0.5
    })

class OnlineLearner:
    """
    Continuous Learning Engine that adjusts system parameters based on feedback signals.
    Uses a simple gradient-descent like approach for weight adaptation.
    """

    def __init__(self, learning_rate: float = 0.01, window_size: int = 100):
        self.learning_rate = learning_rate
        self.window_size = window_size
        self.events: List[LearningEvent] = []
        self.state = AdaptationState()
        
    def record_event(self, signal_type: LearningSignal, context: str, 
                     prediction: str, outcome: str, reward: float) -> LearningEvent:
        """Records a new learning event and updates the state."""
        event = LearningEvent(
            event_id=str(uuid.uuid4()),
            signal_type=signal_type,
            context=context,
            prediction=prediction,
            outcome=outcome,
            reward=reward
        )
        
        self.events.append(event)
        if len(self.events) > self.window_size:
            self.events.pop(0)
            
        self._update_state(event)
        return event

    def _update_state(self, event: LearningEvent):
        """Updates internal weights based on the new reward."""
        self.state.n_events += 1
        
        # Update running average reward
        self.state.avg_reward = (self.state.avg_reward * (self.state.n_events - 1) + event.reward) / self.state.n_events
        
        # Adapt weights based on context keywords (simplistic simulation)
        # In a real scenario, this would be a more complex mapping
        ctx = event.context.lower()
        reward_delta = (event.reward - 0.5) * self.learning_rate # Centered at 0.5
        
        if "code" in ctx or "python" in ctx:
            self.state.weights["model_selection"] += reward_delta
        if "detail" in ctx or "long" in ctx:
            self.state.weights["context_depth"] += reward_delta
        if "creative" in ctx or "story" in ctx:
            self.state.weights["creativity"] += reward_delta
        if "secure" in ctx or "private" in ctx:
            self.state.weights["strictness"] += reward_delta
            
        # Clamp weights between 0 and 1
        for k in self.state.weights:
            self.state.weights[k] = max(0.0, min(1.0, self.state.weights[k]))
            
        self.state.last_adapted_at = time.time()

    def get_weights(self) -> Dict[str, float]:
        """Returns the current adapted weights."""
        return self.state.weights

    def get_recommendation(self, context: str) -> Dict[str, Any]:
        """Provides parameter recommendations based on current weights and context."""
        weights = self.get_weights()
        ctx = context.lower()
        
        # Logic to decide best model/parameters
        recommendation = {
            "temperature": 0.2 + (weights["creativity"] * 0.8),
            "max_tokens": 512 + int(weights["context_depth"] * 3584),
            "top_p": 0.9 + (weights["creativity"] * 0.1),
            "preferred_model": "qwen3.5-9b" if weights["model_selection"] > 0.6 else "gemma3"
        }
        
        if weights["strictness"] > 0.7:
            recommendation["safety_level"] = "high"
            
        return recommendation

    def export_state(self) -> str:
        """Exports the learner state to JSON."""
        return json.dumps(asdict(self.state), indent=2)

    def import_state(self, state_json: str):
        """Imports state from JSON."""
        try:
            data = json.loads(state_json)
            self.state = AdaptationState(**data)
            logger.info("Learner state imported successfully")
        except Exception as e:
            logger.error(f"Failed to import learner state: {e}")

def build_jarvis_online_learner(learning_rate: float = 0.01, window: int = 1000) -> OnlineLearner:
    """Factory function."""
    return OnlineLearner(learning_rate=learning_rate, window_size=window)

async def demo():
    learner = build_jarvis_online_learner(learning_rate=0.05)
    
    print("\n--- SIMULATING LEARNING (50 EVENTS) ---")
    
    # Simulate a user who likes creative coding stories but wants them short
    for i in range(50):
        # Scenario: Coding assistance
        context = "Write a python script for a creative story."
        # LLM performs well on creativity (0.9 reward) but maybe too long (0.2 reward for depth)
        reward = 0.8 if i % 2 == 0 else 0.3
        
        learner.record_event(
            LearningSignal.EXPLICIT_FEEDBACK,
            context,
            "prediction_stub",
            "outcome_stub",
            reward
        )
        
        if i % 10 == 0:
            print(f"Step {i}: Avg Reward = {learner.state.avg_reward:.3f} | Weights = {learner.get_weights()}")
            
    print("\n--- FINAL ADAPTATION STATE ---")
    print(learner.export_state())
    
    print("\n--- PARAMETER RECOMMENDATION FOR 'NEW STORY' ---")
    print(json.dumps(learner.get_recommendation("Tell me a new space story"), indent=2))

if __name__ == "__main__":
    asyncio.run(demo())
