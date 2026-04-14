#!/usr/bin/env python3
"""JARVIS Intent Classifier — Classify user intent to route to correct agent"""
import redis, json, re
from datetime import datetime

r = redis.Redis(decode_responses=True)

INTENTS = {
    "trading":      r"trade|btc|eth|crypto|futures|long|short|perp|signal|scalp|chart|candl",
    "code":         r"bug|fix|code|function|class|import|error|debug|refactor|implement",
    "security":     r"security|hack|vuln|audit|firewall|port|cve|inject|xss|sql.inject",
    "cluster":      r"gpu|cluster|node|m1|m2|m3|vram|temperature|service|health",
    "data":         r"database|sql|query|sqlite|redis|backup|sync|migrate|index",
    "content":      r"linkedin|post|content|article|write|publish|social|message",
    "ops":          r"deploy|restart|cron|systemd|service|install|update|backup",
    "research":     r"search|find|research|compare|analyze|explain|what is|how does",
    "voice":        r"voice|speak|tts|stt|audio|microphone|listen|say",
    "freelance":    r"codeur|client|proposal|mission|bid|freelance|projet",
}

AGENT_MAP = {
    "trading":   "omega-trading-agent",
    "code":      "omega-dev-agent",
    "security":  "omega-security-agent",
    "cluster":   "jarvis-system-agent",
    "data":      "services-data",
    "content":   "social-growth",
    "ops":       "omega-system-agent",
    "research":  "omega-analysis-agent",
    "voice":     "omega-voice-agent",
    "freelance": "business-ops",
}

def classify(text: str) -> dict:
    text_lower = text.lower()
    scores = {}
    for intent, pattern in INTENTS.items():
        matches = len(re.findall(pattern, text_lower))
        if matches > 0:
            scores[intent] = matches
    
    if not scores:
        return {"intent": "general", "agent": None, "confidence": 0.0, "alternatives": []}
    
    sorted_intents = sorted(scores.items(), key=lambda x: -x[1])
    top_intent, top_score = sorted_intents[0]
    total = sum(scores.values())
    confidence = top_score / total
    
    result = {
        "intent": top_intent,
        "agent": AGENT_MAP.get(top_intent),
        "confidence": round(confidence, 2),
        "alternatives": [{"intent": i, "score": s} for i, s in sorted_intents[1:3]],
        "ts": datetime.now().isoformat()[:19]
    }
    
    # Cache recent classifications
    r.lpush("jarvis:intent_log", json.dumps(result))
    r.ltrim("jarvis:intent_log", 0, 99)
    return result

if __name__ == "__main__":
    tests = [
        "analyze BTC signal for long trade",
        "fix bug in my Python function",
        "check GPU temperature",
        "write LinkedIn post about AI",
        "hello how are you",
    ]
    for t in tests:
        r2 = classify(t)
        print(f"  '{t[:35]}' → {r2['intent']} ({r2['confidence']:.0%}) → {r2['agent']}")
