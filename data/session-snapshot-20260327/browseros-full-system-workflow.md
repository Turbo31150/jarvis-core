# JARVIS Full System — Multi-Agent Orchestration Workflow

## Architecture Complète

```
╔═══════════════════════════════════════════════════════════════════════╗
║                    JARVIS MULTI-AGENT SYSTEM v10.0                    ║
╠═══════════════════════════════════════════════════════════════════════╣
║                                                                       ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │                    COUCHE 1 — ENTRÉES                           │  ║
║  │                                                                  │  ║
║  │  📧 Emails    📱 Telegram    🌐 Web Pages    📄 Documents      │  ║
║  │  💬 Codeur    🔷 LinkedIn    📊 MEXC         🎙 Voice          │  ║
║  │  🔔 Webhooks  📡 RSS        🤖 MCP Calls    ⌨️ Terminal       │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 2 — ROUTEUR INTELLIGENT                     │  ║
║  │                                                                  │  ║
║  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │  ║
║  │  │CLASSIFIER│ │PRIORITY  │ │DISPATCHER│ │LOAD      │          │  ║
║  │  │Type/Topic│ │Score 1-10│ │→ Agent   │ │BALANCER  │          │  ║
║  │  │→ keyword │ │→ urgence │ │→ Queue   │ │→ M1/M2/M3│          │  ║
║  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 3 — AGENTS SPÉCIALISÉS                      │  ║
║  │                                                                  │  ║
║  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │  ║
║  │  │ FREELANCE    │ │ LINKEDIN     │ │ CONTENU      │            │  ║
║  │  │ • scan       │ │ • notifs     │ │ • posts      │            │  ║
║  │  │ • proposer   │ │ • commenter  │ │ • articles   │            │  ║
║  │  │ • négocier   │ │ • publier    │ │ • images     │            │  ║
║  │  │ • PDF/images │ │ • réseau     │ │ • PDF        │            │  ║
║  │  └──────────────┘ └──────────────┘ └──────────────┘            │  ║
║  │                                                                  │  ║
║  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │  ║
║  │  │ TRADING      │ │ BROWSER      │ │ CLUSTER      │            │  ║
║  │  │ • scan MEXC  │ │ • navigate   │ │ • GPU health │            │  ║
║  │  │ • signaux    │ │ • click/fill │ │ • modèles    │            │  ║
║  │  │ • consensus  │ │ • screenshot │ │ • failover   │            │  ║
║  │  │ • alertes    │ │ • tab groups │ │ • monitor    │            │  ║
║  │  └──────────────┘ └──────────────┘ └──────────────┘            │  ║
║  │                                                                  │  ║
║  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │  ║
║  │  │ VOICE        │ │ VEILLE       │ │ DEVOPS       │            │  ║
║  │  │ • Whisper STT│ │ • RSS agreg  │ │ • Docker     │            │  ║
║  │  │ • TTS Denise │ │ • synthèse   │ │ • systemd    │            │  ║
║  │  │ • commandes  │ │ • alertes    │ │ • deploy     │            │  ║
║  │  │ • 2658 cmds  │ │ • scoring    │ │ • CI/CD      │            │  ║
║  │  └──────────────┘ └──────────────┘ └──────────────┘            │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 4 — MOTEURS IA                              │  ║
║  │                                                                  │  ║
║  │  LOCAL (24/7 sans internet)                                      │  ║
║  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │  ║
║  │  │ M1 MASTER   │ │ M3 ORCH     │ │ OL1 FAST    │               │  ║
║  │  │ gemma-3-4b  │ │ deepseek-r1 │ │ qwen2.5:1.5b│               │  ║
║  │  │ qwen3.5-9b  │ │ gpt-oss-20b │ │ kimi-k2.5   │               │  ║
║  │  │ deepseek-r1 │ │ mistral-7b  │ │ deepseek:7b │               │  ║
║  │  │ 6 GPU 46GB  │ │ remote .113 │ │ local Ollama│               │  ║
║  │  └─────────────┘ └─────────────┘ └─────────────┘               │  ║
║  │                                                                  │  ║
║  │  CLOUD (étend les capacités)                                     │  ║
║  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │  ║
║  │  │ CLAUDE      │ │ GEMINI      │ │ ChatGPT     │               │  ║
║  │  │ Opus/Sonnet │ │ 2.5 Pro     │ │ GPT-4o      │               │  ║
║  │  │ Haiku       │ │ Flash       │ │ Vision      │               │  ║
║  │  │ Agent SDK   │ │ AI Studio   │ │ API         │               │  ║
║  │  └─────────────┘ └─────────────┘ └─────────────┘               │  ║
║  │                                                                  │  ║
║  │  IA WEB (via BrowserOS)                                          │  ║
║  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │  ║
║  │  │ Perplexity  │ │ Claude.ai   │ │ Gemini Web  │               │  ║
║  │  │ recherche   │ │ raisonnement│ │ multimodal  │               │  ║
║  │  └─────────────┘ └─────────────┘ └─────────────┘               │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 5 — DISTRIBUTION & CONSENSUS                │  ║
║  │                                                                  │  ║
║  │  STRATÉGIE :                                                     │  ║
║  │  Simple (OL1 qwen 0.8s) → Complexe (M1 qwen3.5) →             │  ║
║  │  Critique (M1+M3+cloud consensus vote pondéré)                  │  ║
║  │                                                                  │  ║
║  │  ┌──────────┐    ┌──────────┐    ┌──────────┐                  │  ║
║  │  │ RAPIDE   │    │ QUALITÉ  │    │ CONSENSUS│                  │  ║
║  │  │ OL1      │───▶│ M1/M3    │───▶│ VOTE     │                  │  ║
║  │  │ <1s      │    │ 5-30s    │    │ 3 modèles│                  │  ║
║  │  │ tri,tags │    │ rédaction│    │ pondéré  │                  │  ║
║  │  └──────────┘    └──────────┘    └──────────┘                  │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 6 — SORTIES & NOTIFICATIONS                 │  ║
║  │                                                                  │  ║
║  │  AUTO (sans validation)          APPROVAL (attend validation)   │  ║
║  │  ✅ Likes LinkedIn               📱 Telegram → offre Codeur    │  ║
║  │  ✅ Scan projets                 📱 Telegram → réponse client  │  ║
║  │  ✅ Génération PDF/images        📧 Email → digest quotidien   │  ║
║  │  ✅ Stats/monitoring             🖥 Dashboard → file attente   │  ║
║  │  ✅ GitHub commits               💻 Terminal → info/logs       │  ║
║  │  ✅ Classification emails                                       │  ║
║  └──────────────────────────┬──────────────────────────────────────┘  ║
║                              │                                        ║
║                              ▼                                        ║
║  ┌─────────────────────────────────────────────────────────────────┐  ║
║  │              COUCHE 7 — STOCKAGE & MÉMOIRE                      │  ║
║  │                                                                  │  ║
║  │  SQLite jarvis_orchestrator.db                                   │  ║
║  │  ├── projects_seen (Codeur scans)                               │  ║
║  │  ├── action_queue (file d'attente)                              │  ║
║  │  ├── content_calendar (publications)                            │  ║
║  │  ├── linkedin_stats (analytics)                                 │  ║
║  │  ├── offer_templates (propositions)                             │  ║
║  │  └── client_messages (conversations)                            │  ║
║  │                                                                  │  ║
║  │  BrowserOS Memory (persistant)                                   │  ║
║  │  Claude Code Memory (sessions)                                   │  ║
║  │  n8n Workflows (65 actifs)                                       │  ║
║  └─────────────────────────────────────────────────────────────────┘  ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
```

## Flux de Distribution des Tâches

```
TÂCHE ENTRANTE
      │
      ▼
  ┌────────────────┐
  │ CLASSIFIER     │
  │ (OL1 qwen 0.8s)│
  │                │
  │ Type:          │
  │ • freelance    │
  │ • linkedin     │
  │ • trading      │
  │ • cluster      │
  │ • contenu      │
  │ • voice        │
  │ • devops       │
  └───────┬────────┘
          │
          ▼
  ┌────────────────┐
  │ PRIORITY       │
  │                │
  │ 🔴 Urgent (10)│── Message client, alerte GPU >85°C
  │ 🟡 High (7)   │── Nouveau projet >1K€, commentaire technique
  │ 🟢 Normal (4) │── Scan routine, stats, likes
  │ ⚪ Low (1)    │── Archive, log, cleanup
  └───────┬────────┘
          │
          ▼
  ┌────────────────────────────────────────────────┐
  │ DISPATCHER — Routing vers le bon agent         │
  │                                                │
  │  freelance  → Agent Freelance (Codeur)         │
  │  linkedin   → Agent LinkedIn (BrowserOS CDP)   │
  │  trading    → Agent Trading (MEXC + consensus) │
  │  cluster    → Agent Cluster (GPU/Docker/n8n)   │
  │  contenu    → Agent Contenu (PDF/images/posts) │
  │  voice      → Agent Voice (Whisper/TTS)        │
  │  devops     → Agent DevOps (Docker/systemd)    │
  └────────────────────┬───────────────────────────┘
                       │
                       ▼
  ┌────────────────────────────────────────────────┐
  │ LOAD BALANCER — Choix du modèle IA             │
  │                                                │
  │  Simple (score <4)     → OL1 qwen2.5 (0.8s)   │
  │  Standard (score 4-7)  → M1 gemma-3-4b (0.4s) │
  │  Complexe (score 7-9)  → M1 qwen3.5-9b (2s)   │
  │  Critique (score 10)   → Consensus 3 modèles   │
  │                          M1 + M3 + Claude API  │
  │                          Vote pondéré           │
  │                                                │
  │  Fallback cascade :                             │
  │  M1 → M3 → OL1 → Gemini → Claude              │
  └────────────────────────────────────────────────┘
```

## OpenClaw Integration

```
OPENCLAW (40 agents, gateway:18789)
      │
      ├── 7 providers : M1, M1B, M2, M3, OL1, Gemini, Claude
      ├── 96 agent patterns en DB
      ├── Dispatch engine 9 étapes
      ├── Quality gate 6 portes
      ├── Self-improvement 5 types
      └── Reflection engine 5 axes
```

## Telegram Bot (@turboSSebot)

```
UTILISATEUR
      │
      ▼
  Telegram @turboSSebot
      │
      ├── /status → État du cluster + stats
      ├── /scan   → Force scan Codeur.com
      ├── /offers → Liste offres en attente
      ├── /go [id]→ Valide et envoie une offre
      ├── /skip   → Ignore action en attente
      ├── /stats  → LinkedIn + Codeur analytics
      ├── /gpu    → État GPUs température
      └── /help   → Liste commandes
      │
      ▼
  RÉPONSES INLINE
      │
      ├── [Envoyer 🚀] → Exécute l'action
      ├── [Modifier ✏️] → Ouvre l'éditeur
      └── [Ignorer ❌]  → Archive l'action
```

## Récupération & Tri des Données

```
SOURCES                    TRI                      STOCKAGE
───────                    ───                      ────────
Codeur.com ──┐
LinkedIn ────┤             ┌──────────┐
GitHub ──────┤             │ PIPELINE │             SQLite DB
RSS feeds ───┤────────────▶│ ETL      │────────────▶ projects_seen
Emails ──────┤             │          │             action_queue
MEXC API ────┤             │ 1.Ingest │             content_calendar
Telegram ────┤             │ 2.Clean  │             linkedin_stats
Webhooks ────┘             │ 3.Class  │             offer_templates
                           │ 4.Score  │
                           │ 5.Route  │             BrowserOS
                           │ 6.Store  │────────────▶ Memory
                           └──────────┘             Skills
                                                    Scheduled Tasks
```
