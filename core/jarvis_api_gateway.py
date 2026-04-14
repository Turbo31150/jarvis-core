#!/usr/bin/env python3
"""JARVIS API Gateway — Unified REST API for all JARVIS modules on :8767"""

from flask import Flask, jsonify, request
import sys
import os
import redis
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
app = Flask("jarvis-api")
r_client = redis.Redis(decode_responses=True)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})


@app.route("/metrics")
def metrics():
    try:
        from jarvis_metric_exporter import export

        return jsonify(export())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/score")
def score():
    raw = r_client.get("jarvis:score")
    return jsonify(json.loads(raw) if raw else {"total": 0})


@app.route("/events")
def events():
    count = int(request.args.get("n", 20))
    raw = r_client.lrange("jarvis:event_log", 0, count - 1)
    return jsonify([json.loads(e) for e in raw])


@app.route("/llm/ask", methods=["POST"])
def llm_ask():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    try:
        from jarvis_llm_router import ask

        result = ask(prompt, data.get("type"))
        return jsonify({"response": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/enqueue", methods=["POST"])
def queue_enqueue():
    data = request.json or {}
    try:
        from jarvis_task_queue import enqueue

        tid = enqueue(
            data.get("type", "generic"),
            data.get("payload", {}),
            data.get("priority", 5),
        )
        return jsonify({"id": tid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/queue/stats")
def queue_stats():
    from jarvis_task_queue import stats

    return jsonify(stats())


@app.route("/circuit/status")
def circuit_status():
    from jarvis_circuit_breaker import status_all

    return jsonify(status_all())


@app.route("/cache/stats")
def cache_stats():
    from jarvis_context_cache import stats

    return jsonify(stats())


@app.route("/llm/router/stats")
def llm_router_stats():
    try:
        from jarvis_llm_router import stats

        return jsonify(stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/canary")
def canary():
    raw = r_client.get("jarvis:canary:last")
    return jsonify(json.loads(raw) if raw else {"error": "no data"})


@app.route("/predictions")
def predictions():
    raw = r_client.get("jarvis:predictions")
    return jsonify(json.loads(raw) if raw else {"alerts": []})


@app.route("/rate/stats")
def rate_stats():
    from jarvis_rate_limiter import stats

    return jsonify(stats())


@app.route("/gpu/routing")
def gpu_routing():
    raw = r_client.get("jarvis:gpu_routing")
    if not raw:
        from jarvis_gpu_balancer import publish_routing

        return jsonify(publish_routing())
    return jsonify(json.loads(raw))


@app.route("/models")
def models():
    raw = r_client.get("jarvis:model_registry")
    if not raw:
        from jarvis_model_registry import discover_models

        return jsonify(discover_models())
    return jsonify(json.loads(raw))


@app.route("/flags")
def flags():
    from jarvis_feature_flags import all_flags

    return jsonify(all_flags())


@app.route("/flags/<flag>", methods=["POST"])
def set_flag_route(flag):
    data = request.json or {}
    value = bool(data.get("value", False))
    from jarvis_feature_flags import set_flag

    set_flag(flag, value)
    return jsonify({"flag": flag, "value": value})


@app.route("/tracer/recent")
def tracer_recent():
    from jarvis_distributed_tracer import recent_traces

    return jsonify(recent_traces(20))


@app.route("/sla")
def sla():
    from jarvis_sla_tracker import full_report

    return jsonify(full_report())



@app.route("/intent", methods=["POST"])
def intent():
    data = request.json or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    from jarvis_intent_classifier import classify
    return jsonify(classify(text))


@app.route("/workflow/<name>", methods=["POST"])
def run_workflow(name):
    from jarvis_workflow_engine import run_workflow as _run, WORKFLOWS
    if name not in WORKFLOWS:
        return jsonify({"error": f"unknown workflow: {name}", "available": list(WORKFLOWS.keys())}), 404
    return jsonify(_run(name))

@app.route("/workflow")
def list_workflows():
    from jarvis_workflow_engine import WORKFLOWS
    return jsonify({k: {"steps": len(v["steps"]), "parallel": v["parallel"]} for k, v in WORKFLOWS.items()})

if __name__ == "__main__":
    print("[API Gateway] Starting on :8767")
    app.run(host="0.0.0.0", port=8767, debug=False, threaded=True)
