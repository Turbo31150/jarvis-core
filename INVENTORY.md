# JARVIS System Inventory — 2026-04-14
> Mis à jour : 2026-04-14 | Prochain audit : hebdomadaire (lundi)

---

## Docker Conteneurs

| Nom | Status | Image | Ports |
|-----|--------|-------|-------|
| jarvis-n8n | Up | n8nio/n8n:latest | 0.0.0.0:5678->5678/tcp |
| jarvis-pipeline | Up | jarvis-linux-jarvis-pipeline:latest | 0.0.0.0:9742->9742/tcp |

### Images disponibles

| Image | Tag | Taille |
|-------|-----|--------|
| jarvis-python | latest | 568 MB |
| n8nio/n8n | latest | 2.06 GB |
| ollama/ollama | latest | 9.89 GB |
| node | 20-bookworm-slim | 293 MB |
| node | 22-bookworm-slim | 329 MB |
| node | 20-alpine | 194 MB |
| jarvis-linux-jarvis-pipeline | latest | 10.7 GB |
| openclaw-sandbox | bookworm-slim | 116 MB |
| postgres | 15-alpine | 392 MB |
| redis | alpine | 134 MB |
| hashicorp/terraform-mcp-server | 0.4.0 | 19.8 MB |

---

## Répertoires Principaux

| Chemin | Rôle |
|--------|------|
| `/home/turbo/IA/Core/jarvis/` | Racine JARVIS — 493 modules Python |
| `/home/turbo/IA/Core/jarvis/core/` | 467 modules jarvis_*.py (moteur principal) |
| `/home/turbo/IA/Core/jarvis/orchestrator/` | Orchestrateur + DB SQLite (300K) |
| `/home/turbo/IA/Core/jarvis/agents/` | Agents opérateurs (browseros, github, mail, sql, telegram, voice…) |
| `/home/turbo/IA/Core/jarvis/skills/` | Skills Claude (codeur, linkedinpilot, rgpd-audit, security…) |
| `/home/turbo/IA/Core/jarvis/scripts/` | Scripts opérationnels (30+) |
| `/home/turbo/IA/Core/jarvis/data/` | Données runtime (JSON, HTML, clusters, codeur…) |
| `/home/turbo/IA/Core/jarvis/exports/` | Exports benchmark/GPU/metrics |
| `/home/turbo/IA/Core/jarvis/mcp-servers/` | Configs MCP projet |
| `/home/turbo/IA/Core/jarvis/logs/` | Logs système |
| `/home/turbo/IA/Core/jarvis/cowork/` | CoWork AI collaboration |
| `/home/turbo/jarvis-linux/scripts/` | CLIs systemd + jarvis-monitor/gpu/zombie/cluster |
| `/home/turbo/.claude/` | Config Claude Code (settings, plugins, agents, skills) |
| `/home/turbo/.claude/plugins/local/jarvis-turbo/` | Plugin principal JARVIS |
| `/home/turbo/.claude/plugins/local/jarvis-os/` | Plugin OS JARVIS |

---

## Core Modules (/core/) — 467 fichiers jarvis_*.py

| Catégorie | Count | Modules clés |
|-----------|-------|--------------|
| model | 29 | model_aliaser, model_bench, model_benchmarker, model_cache… |
| task | 11 | task_decomposer, task_dispatcher, task_executor, task_scheduler… |
| cluster | 11 | cluster_coordinator, cluster_event_bus, cluster_gossip, cluster_map… |
| context | 10 | context_assembler, context_budget, context_cache, context_compressor… |
| event | 10 | event_aggregator, event_bus, event_correlator, event_deduplicator… |
| llm | 10 | llm_benchmark_live, llm_cache, llm_client, llm_cost_optimizer… |
| token | 10 | token_budget, token_budget_tracker, token_compressor, token_counter… |
| request | 10 | request_coalescer, request_dedup, request_hedger, request_logger… |
| prompt | 16 | prompt_builder, prompt_cache, prompt_chain, prompt_compressor, prompt_guard… |
| conversation | 6 | conversation_compressor, conversation_graph, conversation_memory… |
| agent | 6 | agent_memory, agent_mesh, agent_planner, agent_registry, agent_runner… |
| gpu | 6 | gpu_allocator, gpu_balancer, gpu_health, gpu_memory_manager, gpu_profiler… |
| inference | 7 | inference_cache, inference_gateway, inference_limiter, inference_monitor… |
| output | 6 | output_cache, output_formatter, output_parser, output_ranker, output_streamer… |
| response | 7 | response_builder, response_cache, response_filter, response_formatter… |
| config | 7 | config_diff, config_loader, config_manager, config_schema, config_store… |
| health | 7 | health_aggregator, health_dashboard, health_predictor, health_probe… |
| api | 7 | api_client_sdk, api_gateway, api_gateway_v2, api_key_manager, api_mock… |
| data | 7 | data_exporter, data_lineage, data_masker, data_pipeline, data_quality… |
| node | 7 | node_affinity, node_balancer, node_exporter, node_monitor, node_rebalancer… |
| consensus | 5 | consensus_engine, consensus_log, consensus_router, consensus_v2, consensus_voter |
| embedding | 5 | embedding_cache, embedding_indexer, embedding_pipeline, embedding_router |
| memory | 5 | memory_consolidator, memory_graph, memory_index, memory_manager, memory_store |
| pipeline | 5 | pipeline_builder, pipeline_debugger, pipeline_executor, pipeline_monitor |
| ab | 4 | ab_router, ab_test_engine, ab_test_runner, ab_tester |
| adaptive | 4 | adaptive_polling, adaptive_prompt, adaptive_scheduler, adaptive_timeout |
| alert | 5 | alert_aggregator, alert_deduplicator, alert_escalator, alert_manager, alert_router |
| cache | 3 | cache_invalidator, cache_prewarm, cache_warmer |
| canary | 3 | canary_analyzer, canary_deployer, canary_tester |
| circuit | 3 | circuit_breaker, circuit_breaker_v2, circuit_monitor |
| cost | 4 | cost_estimator, cost_optimizer, cost_reporter, cost_tracker |
| dependency | 3 | dependency_graph, dependency_injector, dependency_resolver |
| distributed | 3 | distributed_counter, distributed_lock, distributed_tracer |
| feature | 4 | feature_flag, feature_flag_manager, feature_flags, feature_store |
| feedback | 3 | feedback_collector, feedback_engine, feedback_loop |
| load | 4 | load_balancer, load_balancer_v2, load_shedder, load_tester |
| log | 4 | log_aggregator, log_analyzer, log_shipper, log_streamer |
| query | 4 | query_cache, query_optimizer, query_planner, query_router |
| rate | 4 | rate_limiter, rate_limiter_v2, rate_optimizer, rate_window |
| retry | 3 | retry_engine, retry_manager, retry_policy |
| secret | 3 | secret_manager, secret_rotator, secret_vault |
| semantic | 3 | semantic_cache, semantic_dedup, semantic_router |
| session | 4 | session_context, session_manager, session_replay, session_store |
| stream | 4 | stream_aggregator, stream_buffer, stream_processor, stream_response |
| workflow | 3 | workflow_engine, workflow_monitor, workflow_orchestrator |
| quality | 2 | quality_hub, quality_scorer |
| autres | ~60 | (1-2 fichiers chacun : saga, sla, slo, telemetry, thermal, webhook, websocket…) |

### Non-jarvis_ dans /core/

| Fichier | Rôle |
|---------|------|
| benchmark_daily.py | Benchmark quotidien |
| browseros_workflows.py | Workflows BrowserOS |
| capabilities.py | Capacités système |
| config.py | Config centrale |
| docker/ | Configs Docker |
| events.py | Bus événements |
| github_audit.py | Audit GitHub |
| health_dashboard.py | Dashboard santé |
| INDEX_QUALITY_MODULES.md | Index modules qualité |
| __init__.py | Init package |

---

## Services Systemd Actifs (13 running)

| Service | Rôle | Port |
|---------|------|------|
| health-patrol.service | Monitoring continu | — |
| jarvis-api.service | API Gateway | :8767 |
| jarvis-dashboard.service | Web Dashboard | :8765 |
| jarvis-gpu-monitor.service | GPU Monitor & Alerting | — |
| jarvis-hw-monitor.service | Hardware Event Monitor | — |
| jarvis-llm-monitor.service | LLM Monitor (latence + tokens/s, /5min) | — |
| jarvis-log-reactor.service | Log Reactor — auto-trigger dominos | — |
| jarvis-proxy.service | Parallel LLM Proxy | :18800 |
| jarvis-pulse.service | OS Autonomous Pulse | — |
| jarvis-score-updater.service | Score /100 (toutes les 30s) | — |
| jarvis-watchdog.service | Hardware Watchdog (MCE + GPU thermal) | — |
| jarvis-webhook.service | Webhook Server | :8766 |
| jarvis-whisper-api.service | Whisper STT (OpenAI-compatible) | — |

> Services failed : aucun détecté

---

## Bases de Données

| Chemin | Taille | Usage |
|--------|--------|-------|
| `/home/turbo/IA/Core/jarvis/orchestrator/jarvis_orchestrator.db` | 300 K | SQLite principal orchestrateur |
| `/home/turbo/IA/Core/jarvis/jarvis_master_index.db` | — | Index maître |
| `/home/turbo/IA/Core/jarvis/data/etoile.db` | — | DB étoile |
| `/home/turbo/.browseros/profile_permanent/.browseros/browseros.db` | — | BrowserOS sessions |

---

## MCP Servers (depuis .mcp.json projet)

| Serveur | Type |
|---------|------|
| browseros | HTTP — Browser automation |
| playwright | MCP — Browser testing |
| chrome-devtools | stdio — Chrome DevTools :9222 |
| filesystem | MCP — Accès fichiers |
| memory | MCP — Mémoire persistante |
| sequential-thinking | MCP — Raisonnement chainé |
| sqlite | MCP — Accès SQLite |
| context7 | MCP — Docs librairies |
| jarvis-agents | Custom — Agents cluster |
| jarvis-cluster | Custom — Santé cluster |
| jarvis-memory | Custom — Mémoire JARVIS |
| jarvis-tools | Custom — Outils JARVIS |
| jarvis-trading | Custom — Pipeline trading |
| jarvis-voice | Custom — Voice/STT |

> MCP définis dans : `/home/turbo/IA/Core/jarvis/.mcp.json` + `/home/turbo/IA/Core/jarvis/mcp-servers/`
> Note : settings.json global ne contient pas de mcpServers (défini au niveau projet)

---

## Claude Plugins

| Plugin | Type | Chemin |
|--------|------|--------|
| jarvis-turbo | local | `~/.claude/plugins/local/jarvis-turbo/` |
| jarvis-os | local | `~/.claude/plugins/local/jarvis-os/` |
| claude-plugins-official | marketplace | `~/.claude/plugins/marketplaces/claude-plugins-official/` |

### jarvis-turbo Plugin Contents

**Agents (16):** jarvis-auditor, jarvis-auto-improver, jarvis-auto-scaler, jarvis-backpressure, jarvis-browser, jarvis-cluster-health, jarvis-code-reviewer, jarvis-devops, jarvis-doc-sync, jarvis-flow-dispatcher, jarvis-github-manager, jarvis-gpu-manager, jarvis-multi-platform-router, jarvis-quality-gate, jarvis-task-balancer, jarvis-voice-controller

**Commands (10):** ask-ai, browse, cluster-check, consensus, content, demarrage, diagnostic, github, gpu-status, heal-cluster, models, os, trade, trading-scan (+ variants)

**Skills (13):** autotest-analysis, browseros-skill-triggers, browser-workflow, continuous-improvement, demarrage, failover-recovery, github-manager, jarvis-omega-megaprompt, mao-workflow, performance-tuning, security-audit, system-optimizer, weighted-orchestration

### ~/.claude/ Agents & Skills

| Type | Count |
|------|-------|
| Agents (~/.claude/agents/) | 58 |
| Skills (~/.claude/skills/) | 9 |
| Commands (~/.claude/commands/) | 0 (via plugins) |

---

## Scripts Clés

### /home/turbo/IA/Core/jarvis/scripts/ (30+)

| Script | Rôle |
|--------|------|
| browser_agent.py / cdp.py | CDP / BrowserOS automation |
| codeur-auto-apply.py / codeur-scan.py | Codeur.com automation |
| cluster_benchmark_distributed.py | Benchmark cluster distribué |
| comet_cluster.py | Cluster Comet AI |
| content-machine.py | Content automation |
| daily-briefing.py / daily-report.py | Rapports quotidiens |
| dashboard-server.py | Serveur dashboard |
| gpu_oc_jarvis.sh | GPU overclocking JARVIS |
| health_check.sh | Health check rapide |
| linkedin_auto_poster.py | LinkedIn automation |
| multi_agent_pipeline.py | Pipeline multi-agent |

### /home/turbo/jarvis-linux/scripts/ (CLIs systèmes)

alert-correlation-engine.py, benchmark_1000.py, benchmark-parallel.py, consensus_engine.py, content_automator.py, cron_manager.py, health_dashboard.py, health_patrol.py, intelligent_dispatcher.py, linkedin_deep_engagement.py, memory_consolidation.py, n8n_webhook_handler.py, nightly_reporter.py, openclaw-health-dashboard.py

---

## Exports & Data

### exports/
cuda12_bench_20260414.json, events_20260414.json, gpu_autotuning_final.json, gpu_tuning_phase1/3.json, metrics_*.csv, scores_*.csv, benchmark_visual.html

### data/
blueprint/, bookmarks/, browser/, cluster/, codeur-analysis-latest.json, codeur-propositions-reelles.md, codeur-reponses-types.md, codeur-seen-projects.json, daily-intelligence-*.txt, dashboard.html, devpost-hackathons.json, etoile.db, github-images/, content/

---

## Fichiers Configuration Clés

| Fichier | Chemin |
|---------|--------|
| CLAUDE.md global | `/home/turbo/.claude/CLAUDE.md` |
| CLAUDE.md projet | `/home/turbo/IA/Core/jarvis/CLAUDE.md` |
| settings.json | `/home/turbo/.claude/settings.json` |
| settings.local.json | `/home/turbo/.claude/settings.local.json` |
| .mcp.json projet | `/home/turbo/IA/Core/jarvis/.mcp.json` |
| Orchestrateur DB | `/home/turbo/IA/Core/jarvis/orchestrator/jarvis_orchestrator.db` |
| Master Index DB | `/home/turbo/IA/Core/jarvis/jarvis_master_index.db` |
| MEMORY.md | `/home/turbo/.claude/projects/-home-turbo-IA-Core-jarvis/memory/MEMORY.md` |
| INVENTORY.md (ce fichier) | `/home/turbo/IA/Core/jarvis/INVENTORY.md` |

---

## Statistiques Globales

| Métrique | Valeur |
|----------|--------|
| Modules Python core (jarvis_*.py) | **467** |
| Fichiers Python total /core/ | **493** |
| Conteneurs Docker actifs | **2** (jarvis-n8n, jarvis-pipeline) |
| Images Docker disponibles | **11** |
| Services systemd running | **13** |
| MCP Servers configurés | **14** |
| Agents ~/.claude/agents/ | **58** |
| Skills ~/.claude/skills/ | **9** |
| Plugins actifs | **3** (jarvis-turbo, jarvis-os, claude-plugins-official) |
| DB SQLite principales | **3+** |
| Scripts opérationnels | **30+** |
| Services ports exposés | 5678, 8765, 8766, 8767, 9742, 18800 |
