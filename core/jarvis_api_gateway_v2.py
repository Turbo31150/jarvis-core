#!/usr/bin/env python3
"""JARVIS API Gateway v2 — Extended REST API with new module endpoints on :8768"""

from flask import Flask, jsonify, request
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
app = Flask("jarvis-api-v2")


@app.route("/health")
def health():
    return jsonify(
        {"status": "ok", "version": 2, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    )


@app.route("/rag", methods=["POST"])
def rag():
    data = request.json or {}
    q = data.get("query", "")
    if not q:
        return jsonify({"error": "query required"}), 400
    from jarvis_rag_engine import ask

    return jsonify(
        ask(q, top_k=data.get("top_k", 4), task_type=data.get("task_type", "default"))
    )


@app.route("/intent", methods=["POST"])
def intent():
    data = request.json or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    from jarvis_intent_router import classify

    return jsonify(classify(text))


@app.route("/intent/route", methods=["POST"])
def intent_route():
    data = request.json or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "text required"}), 400
    from jarvis_intent_router import route_and_handle

    return jsonify(route_and_handle(text))


@app.route("/rerank", methods=["POST"])
def rerank():
    data = request.json or {}
    items = data.get("items", [])
    query = data.get("query", "")
    if not items:
        return jsonify({"error": "items required"}), 400
    from jarvis_reranker import rerank as do_rerank

    return jsonify(do_rerank(items, query))


@app.route("/optimize", methods=["POST"])
def optimize():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    from jarvis_token_optimizer import optimize as do_opt

    return jsonify(
        do_opt(prompt, data.get("max_tokens", 800), data.get("aggressive", False))
    )


@app.route("/multi-query", methods=["POST"])
def multi_query():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    mode = data.get("mode", "fan_out")
    from jarvis_multi_query import fan_out, consensus, race

    if mode == "consensus":
        return jsonify(consensus(prompt, data.get("backends")))
    elif mode == "race":
        return jsonify(race(prompt, data.get("backends")))
    return jsonify(fan_out(prompt, data.get("backends")))


@app.route("/snapshot", methods=["POST"])
def snapshot():
    data = request.json or {}
    from jarvis_snapshot import capture

    return jsonify(capture(data.get("label", "api"), data.get("tags", [])))


@app.route("/snapshot/list")
def snapshot_list():
    from jarvis_snapshot import list_snapshots

    return jsonify(list_snapshots(int(request.args.get("limit", 10))))


@app.route("/snapshot/diff")
def snapshot_diff():
    a = request.args.get("a")
    b = request.args.get("b")
    if not a or not b:
        return jsonify({"error": "a and b required"}), 400
    from jarvis_snapshot import diff

    return jsonify(diff(a, b))


@app.route("/kb/search")
def kb_search():
    q = request.args.get("q", "")
    tag = request.args.get("tag")
    from jarvis_knowledge_base import search

    return jsonify(
        search(q, tags=[tag] if tag else None, limit=int(request.args.get("limit", 5)))
    )


@app.route("/kb/store", methods=["POST"])
def kb_store():
    data = request.json or {}
    from jarvis_knowledge_base import store

    doc = store(
        data["id"],
        data["title"],
        data["content"],
        data.get("tags", []),
        data.get("source", "api"),
    )
    return jsonify({"ok": True, "id": doc["id"]})


@app.route("/embed/search")
def embed_search():
    q = request.args.get("q", "")
    from jarvis_embedding_store import search

    return jsonify(search(query_text=q, top_k=int(request.args.get("k", 5))))


@app.route("/cluster/state")
def cluster_state():
    from jarvis_cluster_state import get_state, get_nodes

    return jsonify({"state": get_state(), "nodes": get_nodes()})


@app.route("/cluster/transition", methods=["POST"])
def cluster_transition():
    data = request.json or {}
    from jarvis_cluster_state import transition

    return jsonify(transition(data.get("state", ""), data.get("reason", "")))


@app.route("/health-score")
def health_score():
    from jarvis_health_scorer import stats

    return jsonify(stats())


@app.route("/drift")
def drift():
    from jarvis_drift_detector import scan_all

    return jsonify(scan_all())


@app.route("/self-optimize", methods=["POST"])
def self_optimize():
    from jarvis_self_optimizer import run_cycle

    return jsonify(run_cycle())


@app.route("/signals")
def signals():
    from jarvis_signal_bus import read_latest

    n = int(request.args.get("n", 10))
    return jsonify(read_latest(n))


@app.route("/audit")
def audit():
    from jarvis_audit_log import recent

    return jsonify(
        recent(
            int(request.args.get("limit", 20)), request.args.get("min_severity", "info")
        )
    )


@app.route("/model-cache")
def model_cache():
    from jarvis_model_cache import stats

    return jsonify(stats())


@app.route("/node-balance")
def node_balance():
    from jarvis_node_balancer import cluster_status

    return jsonify(cluster_status())


if __name__ == "__main__":
    print("[API Gateway v2] Starting on :8768")
    app.run(host="0.0.0.0", port=8768, debug=False, threaded=True)
