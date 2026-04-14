import re
from dataclasses import dataclass
from enum import Enum

class Verdict(Enum):
    LIKELY_FACTUAL = "LIKELY_FACTUAL"
    UNCERTAIN = "UNCERTAIN"
    LIKELY_HALLUCINATION = "LIKELY_HALLUCINATION"

@dataclass
class HallucinationResult:
    score: float
    flags: list[str]
    verdict: Verdict

class HallucinationDetector:
    def check_consistency(self, response: str, context: str) -> float:
        # Placeholder for consistency checking logic
        return 0.5

    def check_specificity(self, text: str) -> float:
        # Detect invented dates/precise numbers
        date_pattern = r"\b\d{4}-\d{2}-\d{2}\b"
        number_pattern = r"\b\d+\.\d{3,}\b"
        
        if re.search(date_pattern, text):
            return 0.8
        elif re.search(number_pattern, text):
            return 0.7
        else:
            return 0.3

    def check_hedging(self, text: str) -> float:
        # Count uncertainty markers
        hedging_words = ["apparently", "seems", "supposedly", "allegedly", "reportedly"]
        count = sum(text.lower().count(word) for word in hedging_words)
        return min(1.0, count * 0.2)

    def check_contradiction(self, text: str) -> float:
        # Detect self-contradictions
        contradictions = [
            ("good", "bad"),
            ("happy", "sad"),
            ("yes", "no"),
            ("true", "false")
        ]
        
        for a, b in contradictions:
            if (a in text.lower() and b in text.lower()):
                return 0.9
        return 0.1

    def check_source_grounding(self, text: str, sources: list[str]) -> float:
        # Placeholder for source grounding logic
        return 0.5

    def detect(self, response: str, context: str = '', sources: list[str] = []) -> HallucinationResult:
        consistency_score = self.check_consistency(response, context)
        specificity_score = self.check_specificity(response)
        hedging_score = self.check_hedging(response)
        contradiction_score = self.check_contradiction(response)
        source_grounding_score = self.check_source_grounding(response, sources)

        overall_score = (consistency_score + specificity_score + hedging_score +
                         contradiction_score + source_grounding_score) / 5

        flags = []
        if consistency_score < 0.6:
            flags.append("INCONSISTENT")
        if specificity_score > 0.7:
            flags.append("SPECIFICITY_HIGH")
        if hedging_score > 0.3:
            flags.append("UNCERTAIN_LANGUAGE")
        if contradiction_score > 0.5:
            flags.append("CONTRADICTION_FOUND")
        if source_grounding_score < 0.6:
            flags.append("SOURCE_NOT_GROUNDED")

        verdict = Verdict.LIKELY_FACTUAL
        if overall_score < 0.4:
            verdict = Verdict.LIKELY_HALLUCINATION
        elif overall_score < 0.7:
            verdict = Verdict.UNCERTAIN

        return HallucinationResult(score=overall_score, flags=flags, verdict=verdict)

def build_jarvis_hallucination_detector():
    return HallucinationDetector()

if __name__ == "__main__":
    detector = build_jarvis_hallucination_detector()
    
    response = "The event happened on 2023-10-15 and was attended by 1,234 people."
    context = "The event took place in a small town."
    sources = ["Local News", "Town Hall Records"]

    result = detector.detect(response, context, sources)
    
    print(f"Score: {result.score}")
    print(f"Flags: {', '.join(result.flags)}")
    print(f"Verdict: {result.verdict.value}")