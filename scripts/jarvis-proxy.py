#!/usr/bin/env python3
"""
jarvis-proxy — Parallel LLM Router :18800
Race M1/qwen + M1/r1 + M2/qwen + M2/r1 + OL1 — premier non-vide gagne.
Compatible OpenAI API (/v1/chat/completions).
"""

import json
import threading
import urllib.request
import urllib.error
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

BACKENDS = [
    ("http://192.168.1.85:1234", "qwen/qwen3.5-9b", "openai"),
    ("http://192.168.1.85:1234", "deepseek/deepseek-r1-0528-qwen3-8b", "openai"),
    ("http://192.168.1.26:1234", "qwen/qwen3.5-9b", "openai"),
    ("http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", "openai"),
    ("http://127.0.0.1:11434", "gemma3:4b", "ollama"),
]


def call_openai(base, model, messages, max_tokens, temperature):
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"] or "", model, base
    except Exception:
        return "", model, base


def call_ollama(base, model, messages, max_tokens, temperature):
    prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
            return d.get("response", ""), model, base
    except Exception:
        return "", model, base


def race(messages, max_tokens=4096, temperature=0.2):
    result = {"text": "", "model": "", "base": ""}
    done = threading.Event()

    def worker(base, model, api):
        if done.is_set():
            return
        if api == "ollama":
            text, m, b = call_ollama(base, model, messages, max_tokens, temperature)
        else:
            text, m, b = call_openai(base, model, messages, max_tokens, temperature)
        if text.strip() and not done.is_set():
            result["text"] = text
            result["model"] = m
            result["base"] = b
            done.set()

    threads = []
    for base, model, api in BACKENDS:
        t = threading.Thread(target=worker, args=(base, model, api), daemon=True)
        t.start()
        threads.append(t)

    done.wait(timeout=120)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": "auto", "object": "model", "owned_by": "jarvis-proxy"}
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        messages = body.get("messages", [])
        max_tokens = body.get("max_tokens", 4096)
        temperature = body.get("temperature", 0.2)

        t0 = time.time()
        r = race(messages, max_tokens, temperature)
        ms = int((time.time() - t0) * 1000)

        if not r["text"]:
            resp = json.dumps({"error": "all backends failed"}).encode()
            self.send_response(503)
        else:
            resp = json.dumps(
                {
                    "id": f"proxy-{ms}",
                    "object": "chat.completion",
                    "model": f"jarvis-proxy/{r['model']}",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": r["text"]},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(resp))
        self.end_headers()
        self.wfile.write(resp)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PROXY_PORT", 18800))
    print(f"[jarvis-proxy] Écoute :{port} — race M1×2 + M2×2 + OL1", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
