#!/usr/bin/env python3
"""JARVIS Consensus Engine — Multi-model consensus for critical decisions"""

import redis
import requests
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:consensus"

BACKENDS = {
    "m2_35b":    {"url": "http://192.168.1.26:1234/v1/chat/completions", "model": "qwen/qwen3.5-35b-a3b", "weight": 0.5, "timeout": 30},
    "ol1_gemma3":{"url": "http://127.0.0.1:11434/api/generate",          "model": "gemma3:4b",            "weight": 0.3, "timeout": 20},
}

CONSENSUS_MODES = {
    "majority":  "At least 50%+1 backends agree",
    "unanimous": "All backends must agree",
    "weighted":  "Weighted score >= threshold",
}


def _query_backend(name: str, prompt: str) -> str | None:
    cfg = BACKENDS[name]
    try:
        if "11434" in cfg["url"]:
            resp = requests.post(cfg["url"], json={"model": cfg["model"], "prompt": prompt, "stream": False, "options": {"num_predict": 100}}, timeout=cfg["timeout"])
            return resp.json().get("response", "").strip()
        else:
            resp = requests.post(cfg["url"], json={"model": cfg["model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 100, "temperature": 0.05}, timeout=cfg["timeout"])
            msg = resp.json()["choices"][0]["message"]
            return (msg.get("content") or "").strip()
    except Exception as e:
        return None


def _extract_yes_no(response: str) -> str | None:
    if not response:
        return None
    lower = response.lower()
    if any(w in lower[:50] for w in ["yes", "oui", "true", "correct", "affirmative"]):
        return "yes"
    if any(w in lower[:50] for w in ["no", "non", "false", "incorrect", "negative"]):
        return "no"
    return None


def query_consensus(question: str, mode: str = "weighted", threshold: float = 0.6) -> dict:
    """Ask multiple backends and compute consensus"""
    prompt = f"""Answer ONLY 'yes' or 'no' (nothing else): {question}"""

    responses = {}
    votes = {"yes": 0.0, "no": 0.0, "abstain": 0.0}

    for name, cfg in BACKENDS.items():
        raw = _query_backend(name, prompt)
        answer = _extract_yes_no(raw or "")
        weight = cfg["weight"]
        responses[name] = {"raw": (raw or "")[:80], "answer": answer, "weight": weight}
        if answer in votes:
            votes[answer] += weight
        else:
            votes["abstain"] += weight

    total_weight = sum(votes.values())
    if total_weight > 0:
        for k in votes:
            votes[k] = round(votes[k] / total_weight, 3)

    # Determine consensus
    if mode == "majority":
        consensus = "yes" if sum(1 for v in responses.values() if v["answer"] == "yes") > len(responses) / 2 else "no"
        confident = True
    elif mode == "unanimous":
        answers = [v["answer"] for v in responses.values() if v["answer"]]
        consensus = answers[0] if answers and all(a == answers[0] for a in answers) else "no_consensus"
        confident = consensus != "no_consensus"
    else:  # weighted
        consensus = "yes" if votes.get("yes", 0) >= threshold else "no"
        confident = votes.get("yes", 0) >= threshold or votes.get("no", 0) >= threshold

    result = {
        "ts": datetime.now().isoformat()[:19],
        "question": question[:100],
        "mode": mode,
        "consensus": consensus,
        "confident": confident,
        "votes": votes,
        "responses": responses,
    }
    r.lpush(f"{PREFIX}:history", json.dumps(result))
    r.ltrim(f"{PREFIX}:history", 0, 49)
    return result


def recent_decisions(n: int = 10) -> list:
    raw = r.lrange(f"{PREFIX}:history", 0, n - 1)
    return [json.loads(d) for d in raw]


if __name__ == "__main__":
    questions = [
        ("Is Python a good language for AI development?", "weighted"),
        ("Should we restart the API gateway if it returns 500 errors?", "majority"),
    ]
    for q, mode in questions:
        print(f"\nQ: {q}")
        res = query_consensus(q, mode)
        print(f"Consensus ({mode}): {res['consensus']} (confident={res['confident']})")
        print(f"Votes: {res['votes']}")
        for bname, r_info in res["responses"].items():
            print(f"  {bname}: {r_info['answer']} — {r_info['raw'][:50]}")
