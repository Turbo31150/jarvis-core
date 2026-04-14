#!/usr/bin/env python3
"""
jarvis-proxy — Parallel LLM Router :18800
Race M1/qwen + M1/r1 + M2/qwen + M2/r1 + OL1 — premier non-vide gagne.
Compatible OpenAI API (/v1/chat/completions).
"""

import json
import logging
import subprocess
import sys
import threading
import urllib.request
import urllib.error
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

# ── Quality Hub (optionnel — ne bloque pas si absent) ───────────────────────
sys.path.insert(0, "/home/turbo/IA/Core/jarvis/core")
try:
    from jarvis_quality_hub import build_jarvis_quality_hub

    _hub = build_jarvis_quality_hub()
    logger.info("Quality Hub active")
except ImportError:
    _hub = None
    logger.info("Quality Hub not installed — moderation disabled")
except Exception as e:
    _hub = None
    logger.error("Quality Hub failed to initialize: %s — moderation DISABLED", e)

LM_GUARD = "/home/turbo/IA/Core/jarvis/scripts/lm_guard.py"


def _guard_check():
    """Lance lm_guard check en background — enforce si critique."""
    try:
        r = subprocess.run(
            ["python3", LM_GUARD, "check"], capture_output=True, text=True, timeout=5
        )
        if r.returncode == 2:
            logger.warning("lm_guard: critical VRAM state — launching enforce")
            subprocess.Popen(
                ["python3", LM_GUARD, "enforce"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except FileNotFoundError:
        logger.error("lm_guard not found at %s — VRAM guard DISABLED", LM_GUARD)
    except subprocess.TimeoutExpired:
        logger.warning("lm_guard check timed out")
    except Exception as e:
        logger.error("lm_guard check failed: %s", e)


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

        # Guard VRAM en background (non-bloquant)
        threading.Thread(target=_guard_check, daemon=True).start()

        # Quality Hub — pré-filtre injection/modération
        prompt_text = " ".join(
            m.get("content", "") for m in messages if m.get("role") == "user"
        )
        if _hub and prompt_text:
            try:
                guard = _hub.analyze_input(prompt_text)
                if isinstance(guard, dict) and guard.get("action") == "BLOCK":
                    resp = json.dumps(
                        {
                            "error": "blocked",
                            "reason": guard.get("reason", "quality_hub"),
                        }
                    ).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", len(resp))
                    self.end_headers()
                    self.wfile.write(resp)
                    return
            except Exception as e:
                logger.error(
                    "Quality Hub analyze_input failed: %s — request allowed through", e
                )

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
