---
name: Cluster Architecture
description: M1/M2/M3/OL1 GPU cluster topology, endpoints, and roles
type: project
---

**Cluster nodes (benchmark 2026-03-27):**
- M3 (remote): LMStudio at http://192.168.1.113:1234 — **champion fiabilite 100%**, deepseek-r1-0528-qwen3-8b, raisonnement
- OL1 (local): Ollama at http://127.0.0.1:11434 — 85% pass, ultra-rapide (0.3-5s), qwen2.5:1.5b + deepseek-r1:7b
- M1 (local): LMStudio at http://127.0.0.1:1234 — gemma-3-4b (0.4s rapide) + qwen3.5-9b (reasoning lent 46s) + deepseek-r1
- M2 (remote): LMStudio at http://192.168.1.26:1234 — deepseek-coder, instable (HTTP 500 frequents, 35% pass)

**MCP servers active:** jarvis, jarvis-m1, jarvis-ol1, browseros (http://127.0.0.1:9200/mcp), chrome-devtools, comet, github, playwright, Google Calendar, Canva, Notion

**Cascade failover (benchmark 2026-03-27):** M3→OL1→M1→M2→GEMINI→CLAUDE

**Safety rules:**
- Thermal guard: block if GPU > 85C
- Never CPU fallback to asyncio
- BrowserOS at port 9200 with 56 automation tools (offline — needs manual start)

**Why:** Knowing the cluster topology prevents routing errors and enables proper failover decisions.
**How to apply:** Always check node health before dispatching GPU tasks; route appropriately based on node roles.
