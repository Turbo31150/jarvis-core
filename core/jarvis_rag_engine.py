#!/usr/bin/env python3
"""JARVIS RAG Engine — Retrieval-Augmented Generation: search KB + embed + rerank + LLM"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:rag:stats"


def retrieve(query: str, top_k: int = 5) -> list:
    """Retrieve relevant docs from KB and embedding store."""
    results = []
    try:
        from jarvis_knowledge_base import search as kb_search

        kb_results = kb_search(query, limit=top_k)
        for doc in kb_results:
            results.append(
                {
                    "text": doc["content"],
                    "title": doc["title"],
                    "source": "kb",
                    "score": doc.get("_score", 0),
                    "ts": doc.get("created_at", time.time()),
                }
            )
    except Exception:
        pass

    try:
        from jarvis_embedding_store import search as embed_search

        em_results = embed_search(query_text=query, top_k=top_k)
        for doc in em_results:
            results.append(
                {
                    "text": doc["text"],
                    "title": doc.get("id", ""),
                    "source": "embeddings",
                    "score": doc["score"],
                    "ts": time.time(),
                }
            )
    except Exception:
        pass

    return results


def build_context(docs: list, max_tokens: int = 1500) -> str:
    """Build a context string from retrieved docs, respecting token budget."""
    budget = max_tokens
    parts = []
    for doc in docs:
        title = doc.get("title", "")
        text = doc.get("text", "")
        snippet = f"[{title}]\n{text}" if title else text
        tokens = len(snippet.split()) * 4 // 3
        if tokens > budget:
            # Truncate to budget
            words = snippet.split()
            snippet = " ".join(words[: int(budget * 3 / 4)]) + "…"
            parts.append(snippet)
            break
        parts.append(snippet)
        budget -= tokens
        if budget <= 0:
            break
    return "\n\n---\n\n".join(parts)


def generate(query: str, context: str, task_type: str = "default") -> str:
    """Generate answer from LLM using retrieved context."""
    prompt = f"""Answer the question using ONLY the context below. Be concise.

Context:
{context}

Question: {query}

Answer:"""
    try:
        from jarvis_token_optimizer import optimize

        opt = optimize(prompt)
        prompt = opt["optimized"]
    except Exception:
        pass

    try:
        from jarvis_llm_router import ask

        return ask(prompt, task_type)
    except Exception as e:
        return f"[rag_error] {e}"


def ask(
    query: str, top_k: int = 4, task_type: str = "default", rerank: bool = True
) -> dict:
    """Full RAG pipeline: retrieve → rerank → generate."""
    t0 = time.perf_counter()
    r.hincrby(STATS_KEY, "queries", 1)

    # 1. Retrieve
    docs = retrieve(query, top_k=top_k * 2)
    if not docs:
        r.hincrby(STATS_KEY, "no_context", 1)
        # Fallback: direct LLM
        from jarvis_llm_router import ask as llm_ask

        answer = llm_ask(query, task_type)
        return {
            "answer": answer,
            "sources": [],
            "context_used": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
        }

    # 2. Rerank
    if rerank and len(docs) > 1:
        try:
            from jarvis_reranker import rerank as do_rerank

            docs = do_rerank(docs, query)[:top_k]
        except Exception:
            docs = docs[:top_k]
    else:
        docs = docs[:top_k]

    # 3. Build context
    context = build_context(docs, max_tokens=1200)

    # 4. Generate
    answer = generate(query, context, task_type)

    lat = round((time.perf_counter() - t0) * 1000)
    r.hincrby(STATS_KEY, "answered", 1)
    r.setex(
        "jarvis:rag:last",
        300,
        json.dumps({"query": query[:100], "answer": answer[:200], "ts": time.time()}),
    )

    return {
        "answer": answer,
        "sources": [
            {"title": d.get("title", ""), "source": d.get("source", "")} for d in docs
        ],
        "context_used": True,
        "docs_retrieved": len(docs),
        "latency_ms": lat,
    }


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    last = r.get("jarvis:rag:last")
    return {
        **{k: int(v) for k, v in s.items()},
        "last_query": json.loads(last) if last else None,
    }


if __name__ == "__main__":
    queries = [
        "How do I manage GPU temperatures in the cluster?",
        "What is the LLM routing strategy?",
    ]
    for q in queries:
        result = ask(q, top_k=3)
        print(f"Q: {q}")
        print(f"  Sources: {[s['title'] for s in result['sources']]}")
        print(f"  Answer: {result['answer'][:120]}")
        print(f"  Latency: {result['latency_ms']}ms\n")
    print(f"Stats: {stats()}")
