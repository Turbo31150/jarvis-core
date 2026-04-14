from dataclasses import dataclass, field
from enum import Enum
import time

# Define the LearningSignal enum
class LearningSignal(Enum):
    EXPLICIT_FEEDBACK = 1
    IMPLICIT_ENGAGEMENT = 2
    ERROR_CORRECTION = 3
    PREFERENCE = 4
    OUTCOME = 5

# Define the LearningEvent dataclass
@dataclass
class LearningEvent:
    event_id: int
    signal_type: LearningSignal
    context: dict
    prediction: any
    outcome: any
    reward: float
    timestamp: float = field(default_factory=time.time)

# Define the AdaptationState dataclass
@dataclass
class AdaptationState:
    n_events: int = 0
    avg_reward: float = 0.0
    last_adapted_at: float = time.time()
    weights: dict = field(default_factory=dict)

# Define the OnlineLearner class
class OnlineLearner:
    def __init__(self, learning_rate=0.01, window=1000):
        self.learning_rate = learning_rate
        self.window = window
        self.events = []
        self.state = AdaptationState()

    def record_event(self, signal_type, context, prediction, outcome, reward):
        event_id = len(self.events) + 1
        event = LearningEvent(event_id, signal_type, context, prediction, outcome, reward)
        self.events.append(event)
        return event

    def update_weights(self, event):
        # Exponential decay on old weights
        current_time = time.time()
        for key in list(self.state.weights.keys()):
            self.state.weights[key] *= 0.95 ** ((current_time - self.state.last_adapted_at) / self.window)

        # Update weights based on the reward
        if event.signal_type == LearningSignal.EXPLICIT_FEEDBACK:
            weight_change = self.learning_rate * event.reward
            for key in event.context.keys():
                if key not in self.state.weights:
                    self.state.weights[key] = 0.0
                self.state.weights[key] += weight_change

        # Update adaptation state
        self.state.n_events += 1
        self.state.avg_reward = (self.state.avg_reward * (self.state.n_events - 1) + event.reward) / self.state.n_events
        self.state.last_adapted_at = current_time

    def get_weights(self):
        return self.state.weights

    def adapt_strategy(self, threshold=0.5):
        if self.state.avg_reward < threshold:
            # Perform adaptation logic here
            pass

    def get_recommendation(self, context):
        recommendation = {}
        for key in context.keys():
            if key in self.state.weights:
                recommendation[key] = self.state.weights[key]
        return recommendation

    def export_state(self):
        return {
            'n_events': self.state.n_events,
            'avg_reward': self.state.avg_reward,
            'last_adapted_at': self.state.last_adapted_at,
            'weights': self.state.weights
        }

    def import_state(self, state):
        self.state = AdaptationState(**state)

# Factory function to build a JarvisOnlineLearner instance
def build_jarvis_online_learner(learning_rate=0.01, window=1000):
    return OnlineLearner(learning_rate, window)

# Main demo
if __name__ == "__main__":
    learner = build_jarvis_online_learner()
    for i in range(50):
        event = learner.record_event(
            signal_type=LearningSignal.EXPLICIT_FEEDBACK,
            context={'feature1': i % 2, 'feature2': (i + 1) % 3},
            prediction=i * 2,
            outcome=i * 3,
            reward=float(i % 5)
        )
        learner.update_weights(event)

    print("Final Weights:", learner.get_weights())
    print("Recommendation for context {'feature1': 0, 'feature2': 1}:", learner.get_recommendation({'feature1': 0, 'feature2': 1}))