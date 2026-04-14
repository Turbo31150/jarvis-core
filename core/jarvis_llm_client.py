#!/usr/bin/env python3
"""JARVIS LLM Client — Multi-backend avec fix thinking, cache et fallback"""
import requests, json, time, hashlib
from typing import Optional

BACKENDS = {
    "m2-fast":   ("http://192.168.1.26:1234", "qwen/qwen3.5-9b"),
    "m2-big":    ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b"),
    "m2-reason": ("http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b"),
    "ol1-fast":  ("http://127.0.0.1:11434/api", "qwen3:1.7b"),
    "ol1-mid":   ("http://127.0.0.1:11434/api", "gemma3:4b"),
    "ol1-r1":    ("http://127.0.0.1:11434/api", "deepseek-r1:7b"),
    "or-free":    ("https://openrouter.ai/api/v1", "openrouter/free"),

}

SYS = "Tu es JARVIS. Réponds directement en français, sans préambule."

def _call_lmstudio(host: str, model: str, prompt: str, max_tokens: int = 1000, sys_prompt: str = SYS) -> Optional[str]:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False}
    }
    try:
        r = requests.post(f"{host}/v1/chat/completions", json=payload, timeout=90)
        d = r.json()
        msg = d["choices"][0]["message"]
        # FIX: always prefer content, fallback to reasoning_content
        return msg.get("content") or msg.get("reasoning_content") or None
    except:
        return None

def _call_ollama(model: str, prompt: str, max_tokens: int = 1000, sys_prompt: str = SYS) -> Optional[str]:
    payload = {"model": model, "prompt": f"{sys_prompt}\n\n{prompt}", "stream": False, "options": {"num_predict": max_tokens}}
    try:
        r = requests.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=90)
        return r.json().get("response") or None
    except:
        return None


def _call_openrouter(model: str, prompt: str, max_tokens: int = 1000, sys_prompt: str = SYS) -> Optional[str]:
    import os
    key = os.environ.get("OPENROUTER_API_KEY","")
    if not key:
        try:
            with open("/home/turbo/IA/Core/jarvis/config/secrets.env") as f:
                for line in f:
                    if "OPENROUTER_API_KEY=" in line:
                        key = line.split("=",1)[1].strip()
                        break
        except: pass
    if not key: return None
    payload = {"model": model, "messages": [{"role":"system","content":sys_prompt},{"role":"user","content":prompt}], "max_tokens": max_tokens}
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, timeout=60,
                         headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://jarvis.local"})
        d = r.json()
        msg = d["choices"][0]["message"]
        return msg.get("content") or msg.get("reasoning") or None
    except: return None

def ask(prompt: str, backend: str = "m2-fast", max_tokens: int = 1000, sys_prompt: str = SYS) -> Optional[str]:
    host, model = BACKENDS.get(backend, BACKENDS["m2-fast"])
    if "11434" in host:
        return _call_ollama(model, prompt, max_tokens, sys_prompt)
    if "openrouter.ai" in host:
        return _call_openrouter(model, prompt, max_tokens, sys_prompt)
    return _call_lmstudio(host, model, prompt, max_tokens, sys_prompt)

def ask_best(prompt: str, max_tokens: int = 500) -> tuple[str, str]:
    """Try backends in order until one responds. Returns (response, backend_used)"""
    for name in ["m2-fast", "m2-reason", "ol1-mid", "ol1-r1", "or-free"]:
        r = ask(prompt, name, max_tokens)
        if r and len(r.strip()) > 3:
            return r.strip(), name
    return "ERROR: tous backends ont échoué", "none"

if __name__ == "__main__":
    r, b = ask_best("Quelle est la capitale de la France? 1 mot.")
    print(f"[{b}] {r}")
