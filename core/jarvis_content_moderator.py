import re
from dataclasses import dataclass
from enum import Enum

class ContentCategory(Enum):
    SAFE = "SAFE"
    ADULT = "ADULT"
    VIOLENCE = "VIOLENCE"
    HATE = "HATE"
    SELF_HARM = "SELF_HARM"
    SPAM = "SPAM"
    PII = "PII"
    LEGAL_RISK = "LEGAL_RISK"

class Action(Enum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    BLOCK = "BLOCK"
    REDACT = "REDACT"

@dataclass
class ModerationResult:
    categories: dict
    flagged: bool
    action: Action
    reason: str

class ContentModerator:
    def __init__(self, policy='moderate'):
        self.policy = policy
        self.pii_patterns = [
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',  # Email
            r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',  # Phone number
            r'\b\d{3}-\d{2}-\d{4}\b',  # SSN
            r'\b(?:\d[ -]*?){13,16}\b'  # Credit card
        ]
        self.toxic_words = [
            "hate", "kill", "asshole", "stupid", "idiot"
        ]
        self.spam_patterns = [
            r'(.)\1{4,}',  # Repeated character more than 4 times
            r'.{200,}'  # Long text
        ]
        self.legal_keywords = [
            "GDPR", "copyright", "trademark"
        ]

    def check_pii(self, text):
        pii_detected = False
        for pattern in self.pii_patterns:
            if re.search(pattern, text):
                pii_detected = True
                break
        return pii_detected

    def check_toxicity(self, text):
        toxicity_detected = any(word in text.lower() for word in self.toxic_words)
        return toxicity_detected

    def check_spam(self, text):
        spam_detected = False
        for pattern in self.spam_patterns:
            if re.search(pattern, text):
                spam_detected = True
                break
        return spam_detected

    def check_legal(self, text):
        legal_risk_detected = any(keyword.lower() in text.lower() for keyword in self.legal_keywords)
        return legal_risk_detected

    def redact(self, text):
        for pattern in self.pii_patterns:
            text = re.sub(pattern, '[REDACTED]', text)
        return text

    def moderate(self, text):
        categories = {}
        flagged = False
        reason = []

        pii_detected = self.check_pii(text)
        if pii_detected:
            categories[ContentCategory.PII] = True
            flagged = True
            reason.append("PII detected")

        toxicity_detected = self.check_toxicity(text)
        if toxicity_detected:
            categories[ContentCategory.HATE] = True
            flagged = True
            reason.append("Toxic content detected")

        spam_detected = self.check_spam(text)
        if spam_detected:
            categories[ContentCategory.SPAM] = True
            flagged = True
            reason.append("Spam detected")

        legal_risk_detected = self.check_legal(text)
        if legal_risk_detected:
            categories[ContentCategory.LEGAL_RISK] = True
            flagged = True
            reason.append("Legal risk content detected")

        action = Action.ALLOW
        if pii_detected and self.policy == 'strict':
            action = Action.REDACT
        elif flagged:
            action = Action.WARN

        return ModerationResult(categories, flagged, action, ", ".join(reason))

def build_jarvis_content_moderator(policy='moderate'):
    return ContentModerator(policy)

if __name__ == "__main__":
    moderator = build_jarvis_content_moderator('strict')
    text = "Hello, my email is example@example.com and I hate you. Please call me at 123-456-7890."
    result = moderator.moderate(text)
    print(f"Flagged: {result.flagged}")
    print(f"Action: {result.action.value}")
    print(f"Reason: {result.reason}")
    if result.action == Action.REDACT:
        print("Redacted Text:", moderator.redact(text))