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

# SEC-004: token interne minimal (définir JARVIS_API_TOKEN dans l'env pour activer)
_API_TOKEN = os.environ.get("JARVIS_API_TOKEN", "")
_PUBLIC_ENDPOINTS = {"health", "static"}


@app.before_request
def _check_token():
    if _API_TOKEN and request.endpoint not in _PUBLIC_ENDPOINTS:
        if request.headers.get("X-Jarvis-Token") != _API_TOKEN:
            from flask import abort

            abort(401)


# SEC-005: security headers sur toutes les réponses
@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        return jsonify(
            {"error": f"unknown workflow: {name}", "available": list(WORKFLOWS.keys())}
        ), 404
    return jsonify(_run(name))


@app.route("/workflow")
def list_workflows():
    from jarvis_workflow_engine import WORKFLOWS

    return jsonify(
        {
            k: {"steps": len(v["steps"]), "parallel": v["parallel"]}
            for k, v in WORKFLOWS.items()
        }
    )


@app.route("/advisor")
def advisor():
    from jarvis_optimization_advisor import analyze

    return jsonify(analyze())


@app.route("/logs")
def logs():
    raw = r_client.get("jarvis:log_analysis")
    if not raw:
        from jarvis_log_analyzer import analyze_all

        return jsonify(analyze_all())
    return jsonify(json.loads(raw))


@app.route("/self-test")
def self_test():
    from jarvis_self_test import run

    return jsonify(run())


@app.route("/health/full")
def health_full():
    from jarvis_health_aggregator import aggregate

    return jsonify(aggregate())


@app.route("/config")
def config_all():
    from jarvis_config_manager import get_all

    return jsonify(get_all())


@app.route("/config/<path:key>", methods=["GET", "POST"])
def config_key(key):
    if request.method == "POST":
        data = request.json or {}
        from jarvis_config_manager import set_config

        ok = set_config(key.replace("/", "."), data.get("value"))
        return jsonify({"ok": ok})
    from jarvis_config_manager import get

    return jsonify({"key": key, "value": get(key.replace("/", "."))})


@app.route("/bench/run")
def bench_run():
    backend = request.args.get("backend")
    bench = request.args.get("bench", "math_simple")
    from jarvis_model_benchmark import run_benchmark

    return jsonify(run_benchmark(backend, bench))


@app.route("/scheduler/tick")
def scheduler_tick():
    from jarvis_adaptive_scheduler import tick

    return jsonify(tick())


@app.route("/scheduler/can-run")
def scheduler_can_run():
    task = request.args.get("task", "default")
    from jarvis_adaptive_scheduler import can_run

    ok, reason = can_run(task)
    return jsonify({"task": task, "can_run": ok, "reason": reason})


@app.route("/cost/today")
def cost_today():
    from jarvis_cost_tracker import daily_report

    return jsonify(daily_report())


@app.route("/cost/week")
def cost_week():
    from jarvis_cost_tracker import weekly_summary

    return jsonify(weekly_summary())


@app.route("/events/stats")
def events_stats():
    from jarvis_event_replay import stats

    return jsonify(stats())


@app.route("/mesh")
def mesh():
    from jarvis_service_mesh import topology

    return jsonify(topology())


@app.route("/mesh/scan")
def mesh_scan():
    from jarvis_service_mesh import scan_all

    return jsonify(scan_all())


@app.route("/mesh/best-llm")
def mesh_best_llm():
    task = request.args.get("task", "default")
    from jarvis_service_mesh import get_best_llm

    return jsonify(get_best_llm(task))


@app.route("/runbook")
def runbook_list():
    from jarvis_runbook import list_runbooks

    return jsonify(list_runbooks())


@app.route("/runbook/<name>", methods=["POST"])
def runbook_run(name):
    dry = request.args.get("dry", "false").lower() == "true"
    from jarvis_runbook import run_runbook

    return jsonify(run_runbook(name, dry_run=dry))


@app.route("/notify", methods=["POST"])
def notify():
    data = request.json or {}
    msg = data.get("message", "")
    if not msg:
        return jsonify({"error": "message required"}), 400
    from jarvis_notification_router import send

    return jsonify(send(msg, data.get("severity", "info"), data.get("source", "api")))


@app.route("/notif/stats")
def notif_stats():
    from jarvis_notification_router import stats

    return jsonify(stats())


@app.route("/infer", methods=["POST"])
def infer_route():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    from jarvis_inference_gateway import infer

    return jsonify(
        infer(prompt, data.get("task_type", "default"), data.get("use_cache", True))
    )


@app.route("/models/select")
def models_select():
    task = request.args.get("task", "default")
    from jarvis_model_selector import select

    return jsonify(select(task))


@app.route("/models/probe")
def models_probe():
    from jarvis_model_selector import probe_all

    return jsonify(probe_all())


@app.route("/governor")
def governor_route():
    from jarvis_resource_governor import govern

    return jsonify(govern())


@app.route("/anomalies")
def anomalies():
    from jarvis_anomaly_scorer import scan_anomalies

    return jsonify(scan_anomalies())


@app.route("/nodes")
def nodes():
    result = {}
    for n in ["M1", "M2", "M3", "M32", "OL1"]:
        result[n] = {
            "status": r_client.get(f"jarvis:node:{n}:status") or "unknown",
            "info": r_client.hgetall(f"jarvis:node:{n}:info"),
        }
    return jsonify(result)


if __name__ == "__main__":
    print("[API Gateway] Starting on :8767")
    app.run(host="0.0.0.0", port=8767, debug=False, threaded=True)
