#!/usr/bin/env python3
"""Injection massive 420 tâches OpenClaw — 14 blocs, tous domaines JARVIS."""
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'

conn = sqlite3.connect(str(DB))
conn.execute('''CREATE TABLE IF NOT EXISTS openclaw_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    agent TEXT DEFAULT 'main',
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    scheduled_time TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    output TEXT
)''')

# Supprimer anciennes tâches massives pending pour éviter doublons
conn.execute("DELETE FROM openclaw_tasks WHERE description LIKE '[OC2-%' AND status='pending'")
conn.commit()

NOW = datetime.now().strftime('%Y-%m-%d %H:%M')

TASKS = [

# ══════════════════════════════════════════════════════════════════
# BLOC CORE — Engine routing & failover (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-CORE-01] Teste le routing OpenClaw sur 10 types de requêtes différentes (code, analyse, système, trading, linkedin, sécurité, monitoring, infra, docs, recherche) et retourne quel agent a été sélectionné pour chaque type", "master", 9),
("[OC2-CORE-02] Mesure la latence de réponse pour chaque provider LLM disponible (M2/qwen3.5-9b, M2/deepseek-r1, OL1/gemma3:4b, OL1/deepseek-r1:7b) avec une requête test identique de 50 tokens", "cluster-mgr", 9),
("[OC2-CORE-03] Vérifie que le fallback automatique fonctionne : simule un timeout sur M2 et confirme que OL1 prend le relais dans les 10 secondes", "master", 9),
("[OC2-CORE-04] Crée un rapport de santé complet du cluster LLM : uptime, latence moyenne, taux d'erreur, tokens générés aujourd'hui pour chaque nœud", "cluster-mgr", 8),
("[OC2-CORE-05] Teste la gestion des erreurs : envoie une requête malformée à chaque provider et vérifie que l'erreur est correctement catchée et loggée", "master", 8),
("[OC2-CORE-06] Vérifie que le rate limiter fonctionne : envoie 10 requêtes simultanées et confirme que max 3 arrivent en parallèle à M2", "ops-sre", 8),
("[OC2-CORE-07] Analyse les logs OpenClaw des dernières 24h et identifie les 3 patterns d'erreur les plus fréquents avec solutions proposées", "omega-analysis-agent", 8),
("[OC2-CORE-08] Teste que le circuit breaker s'active après 5 erreurs consécutives sur M2 et que le système bascule vers OL1", "master", 8),
("[OC2-CORE-09] Génère un fichier de configuration optimisée pour le routing basé sur les benchmarks actuels du cluster", "omega-dev-agent", 7),
("[OC2-CORE-10] Vérifie que toutes les variables d'environnement critiques sont présentes : TELEGRAM_TOKEN, OPENROUTER_API_KEY, LM_STUDIO clés", "omega-security-agent", 9),
("[OC2-CORE-11] Crée un script de démarrage rapide qui vérifie et démarre tous les composants OpenClaw dans le bon ordre", "ops-sre", 8),
("[OC2-CORE-12] Teste la reconnexion automatique après une coupure réseau simulée vers M2", "cluster-mgr", 7),
("[OC2-CORE-13] Vérifie que le port 18789 OpenClaw est accessible et répond correctement aux health checks", "monitoring", 9),
("[OC2-CORE-14] Crée un dashboard de monitoring temps réel pour l'état du cluster LLM formaté en ASCII pour terminal", "monitoring", 7),
("[OC2-CORE-15] Analyse et optimise la configuration de routing dans openclaw.json pour minimiser la latence", "omega-dev-agent", 7),
("[OC2-CORE-16] Teste le comportement du système quand tous les providers locaux sont indisponibles : doit basculer OpenRouter sans crash", "master", 8),
("[OC2-CORE-17] Vérifie l'intégrité de tous les fichiers de configuration OpenClaw (JSON valide, champs requis présents)", "ops-sre", 7),
("[OC2-CORE-18] Crée un test de charge : 50 messages en rafale vers différents agents et mesure le throughput", "monitoring", 7),
("[OC2-CORE-19] Optimise le timeout de chaque provider : M2=30s, OL1=20s, OpenRouter=15s et documente les changements", "omega-dev-agent", 7),
("[OC2-CORE-20] Vérifie que les sessions agents sont correctement nettoyées après 30 minutes d'inactivité", "sys-ops", 7),
("[OC2-CORE-21] Crée une procédure de rollback si une mise à jour OpenClaw échoue", "ops-sre", 6),
("[OC2-CORE-22] Teste le mode consensus : envoie une requête de décision à 3 agents différents et vérifie l'agrégation", "master", 7),
("[OC2-CORE-23] Vérifie que les logs sont en rotation (max 100MB par fichier, 7 jours de rétention)", "sys-ops", 6),
("[OC2-CORE-24] Crée un script de diagnostic complet qui peut être lancé en cas de problème", "ops-sre", 7),
("[OC2-CORE-25] Teste que le système récupère correctement après un crash du process principal OpenClaw", "ops-sre", 8),
("[OC2-CORE-26] Implémente un mécanisme de heartbeat : chaque agent envoie un ping toutes les 5 minutes", "monitoring", 7),
("[OC2-CORE-27] Vérifie que le cache Redis est opérationnel et que les hits/miss sont loggés", "ai-engine", 7),
("[OC2-CORE-28] Crée un rapport d'utilisation hebdomadaire automatique : agents les plus utilisés, tâches exécutées, succès/échecs", "monitoring", 7),
("[OC2-CORE-29] Teste que les webhooks Telegram fonctionnent correctement pour les notifications", "comms", 8),
("[OC2-CORE-30] Valide la configuration complète du système avec un checklist de 20 points critiques", "master", 9),

# ══════════════════════════════════════════════════════════════════
# BLOC LLM — Modèles locaux validation & optimisation (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-LLM-01] Génère 5 exemples de code Python de qualité production avec qwen3.5-35b et évalue : syntaxe, bonnes pratiques, gestion erreurs", "omega-dev-agent", 9),
("[OC2-LLM-02] Teste les capacités de raisonnement de deepseek-r1 : résous un problème d'optimisation d'algorithme de tri et compare avec qwen3.5", "omega-analysis-agent", 8),
("[OC2-LLM-03] Benchmark comparatif : 10 tâches JARVIS réelles soumises à qwen3.5-9b vs qwen3.5-35b — mesure qualité et latence", "ai-engine", 8),
("[OC2-LLM-04] Valide que gemma3:4b sur OL1 peut gérer des tâches de monitoring système simples en moins de 3 secondes", "monitoring", 8),
("[OC2-LLM-05] Crée des prompts système optimisés pour chaque rôle d'agent (développeur, analyste, système, sécurité)", "omega-docs-agent", 7),
("[OC2-LLM-06] Teste la capacité multilingue : qwen3.5-9b génère du code commenté en français pour 5 fonctions Python utilitaires", "omega-dev-agent", 7),
("[OC2-LLM-07] Évalue la cohérence des réponses : pose la même question 3 fois à deepseek-r1 et mesure la similarité des réponses", "omega-analysis-agent", 7),
("[OC2-LLM-08] Optimise les context windows : teste quelle longueur de contexte donne le meilleur rapport qualité/vitesse pour chaque modèle", "ai-engine", 7),
("[OC2-LLM-09] Crée une bibliothèque de prompts testés et validés pour les 10 tâches les plus fréquentes OpenClaw", "omega-docs-agent", 8),
("[OC2-LLM-10] Teste le streaming de réponse pour les agents interactifs et mesure l'amélioration de l'expérience utilisateur", "ai-engine", 6),
("[OC2-LLM-11] Valide que kimi-k2.5:cloud sur OL1 peut effectuer des recherches et analyses complexes", "omega-analysis-agent", 7),
("[OC2-LLM-12] Crée un test de régression LLM : 20 requêtes de référence avec réponses attendues pour détecter les dégradations", "cowork-testing", 8),
("[OC2-LLM-13] Teste la capacité de code review : soumet un script Python avec 5 bugs intentionnels à qwen3.5-35b", "cowork-refactor", 8),
("[OC2-LLM-14] Évalue deepseek-r1 sur des tâches de debugging : trace d'erreur Python → identification cause → solution", "omega-dev-agent", 8),
("[OC2-LLM-15] Crée un système de scoring automatique des réponses LLM (0-10) basé sur pertinence, complétude, qualité code", "ai-engine", 7),
("[OC2-LLM-16] Teste la capacité de génération de documentation : qwen3.5-35b documente automatiquement 3 scripts JARVIS existants", "omega-docs-agent", 7),
("[OC2-LLM-17] Optimise les paramètres de génération (temperature, top_p, top_k) pour chaque type de tâche", "ai-engine", 6),
("[OC2-LLM-18] Valide que tous les modèles OL1 répondent correctement aux requêtes format OpenAI compatible", "cluster-mgr", 8),
("[OC2-LLM-19] Crée un test de charge LLM : 20 requêtes parallèles et mesure le taux de succès", "monitoring", 7),
("[OC2-LLM-20] Analyse les patterns d'erreur LLM les plus fréquents et crée des handlers spécifiques", "omega-dev-agent", 7),
("[OC2-LLM-21] Teste la génération de tests unitaires automatiques avec qwen3.5-35b sur 3 fonctions Python", "cowork-testing", 8),
("[OC2-LLM-22] Évalue la capacité d'analyse de logs : soumets 50 lignes d'erreurs à deepseek-r1 et évalue la qualité du diagnostic", "omega-analysis-agent", 8),
("[OC2-LLM-23] Crée une politique de sélection de modèle automatique basée sur la complexité estimée de la tâche", "ai-engine", 8),
("[OC2-LLM-24] Teste que le cache LLM Redis fonctionne : vérifie les hit rates et la TTL des entrées", "ai-engine", 7),
("[OC2-LLM-25] Valide que les modèles locaux ne leakent pas d'informations sensibles entre sessions", "omega-security-agent", 8),
("[OC2-LLM-26] Benchmark de qualité de traduction FR/EN pour les agents qui traitent du contenu multilingue", "ai-engine", 6),
("[OC2-LLM-27] Crée un rapport mensuel de performance LLM avec tendances et recommandations d'optimisation", "monitoring", 7),
("[OC2-LLM-28] Teste la robustesse aux prompts adversariaux : vérifie que les agents rejettent les instructions malveillantes", "omega-security-agent", 8),
("[OC2-LLM-29] Optimise le prompt système de chaque agent spécialisé pour maximiser la pertinence des réponses", "omega-dev-agent", 7),
("[OC2-LLM-30] Crée un système de A/B testing pour comparer deux versions de prompts et sélectionner le meilleur", "cowork-testing", 7),

# ══════════════════════════════════════════════════════════════════
# BLOC TOOLS — Outils bash/sqlite/web/fichiers (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-TOOLS-01] Crée et teste un outil file-writer.py : permet aux agents d'écrire des fichiers dans ~/.openclaw/output/ avec validation du chemin", "omega-dev-agent", 9),
("[OC2-TOOLS-02] Crée et teste un outil git-tool.py : wraps git status/diff/log/add/commit avec sécurité (no force push, no main direct)", "cowork-git", 9),
("[OC2-TOOLS-03] Crée et teste un outil python-runner.py : exécute du code Python en sandbox avec timeout 30s, no network, output capturé", "cowork-codegen", 9),
("[OC2-TOOLS-04] Améliore bash-exec.py : ajoute 15 commandes utiles à la whitelist (docker ps, systemctl status, journalctl, etc.)", "omega-dev-agent", 8),
("[OC2-TOOLS-05] Teste sqlite-tool.py sur 10 opérations différentes et vérifie que les opérations destructives sont bloquées", "cowork-testing", 8),
("[OC2-TOOLS-06] Améliore web-tools.py : ajoute support des APIs JSON (pas seulement HTML), timeout configurable, cache local", "omega-dev-agent", 8),
("[OC2-TOOLS-07] Crée un outil log-reader.py : lit les N dernières lignes de n'importe quel log avec filtre regex et colorisation", "monitoring", 7),
("[OC2-TOOLS-08] Crée un outil cron-manager.py : liste/ajoute/supprime des crons pour les agents, avec validation", "automation", 8),
("[OC2-TOOLS-09] Crée un outil docker-tool.py : ps/logs/restart/exec pour containers autorisés uniquement", "omega-system-agent", 8),
("[OC2-TOOLS-10] Crée un outil metrics-collector.py : collecte CPU/RAM/GPU/disk et retourne JSON structuré", "monitoring", 8),
("[OC2-TOOLS-11] Crée un outil api-caller.py : wrapper sécurisé pour appels API externes avec retry et rate limiting", "omega-dev-agent", 7),
("[OC2-TOOLS-12] Améliore telegram-tool.py : support des messages formatés Markdown, envoi fichiers, photos", "comms", 7),
("[OC2-TOOLS-13] Crée un outil redis-tool.py : get/set/delete avec namespace par agent, TTL configurable", "ai-engine", 7),
("[OC2-TOOLS-14] Crée un outil email-tool.py : lecture IMAP et envoi SMTP avec template HTML", "mail-agent", 7),
("[OC2-TOOLS-15] Crée un outil process-manager.py : liste/kill processes par nom avec confirmation sécurité", "sys-ops", 7),
("[OC2-TOOLS-16] Améliore web-tools.py : ajoute DuckDuckGo search, LinkedIn scraping basique, Codeur.com API", "browser-ops", 8),
("[OC2-TOOLS-17] Crée un outil json-validator.py : valide et répare les fichiers JSON corrompus dans ~/.openclaw/", "cowork-testing", 7),
("[OC2-TOOLS-18] Crée un outil template-engine.py : génère du contenu à partir de templates Jinja2 pour les agents", "omega-dev-agent", 6),
("[OC2-TOOLS-19] Crée un outil backup-tool.py : snapshot atomique des DBs SQLite vers ~/.openclaw/backups/", "sys-ops", 8),
("[OC2-TOOLS-20] Teste tous les outils existants de manière automatisée et génère un rapport de couverture", "cowork-testing", 8),
("[OC2-TOOLS-21] Crée un outil schedule-tool.py : interface vers openclaw_tasks pour créer/modifier des tâches programmatiquement", "automation", 8),
("[OC2-TOOLS-22] Crée un outil diff-tool.py : compare deux versions de code Python et génère un résumé des changements", "cowork-refactor", 6),
("[OC2-TOOLS-23] Crée un outil notification-hub.py : centralise toutes les notifications (Telegram, log, console) avec niveaux severity", "comms", 7),
("[OC2-TOOLS-24] Améliore lm-query-safe.py : ajoute queue de priorité, statistiques d'utilisation, métriques par provider", "ai-engine", 8),
("[OC2-TOOLS-25] Crée un outil secret-manager.py : lecture sécurisée des credentials depuis .env avec masquage dans les logs", "omega-security-agent", 8),
("[OC2-TOOLS-26] Crée un outil health-checker.py : vérifie la santé de tous les services JARVIS et retourne statut structuré", "monitoring", 8),
("[OC2-TOOLS-27] Crée un outil content-formatter.py : formate les outputs agents en HTML/Markdown/JSON selon le canal de destination", "comms", 6),
("[OC2-TOOLS-28] Crée un outil task-tracker.py : tableau de bord des tâches avec progression, ETA, historique", "monitoring", 7),
("[OC2-TOOLS-29] Teste la chaîne complète tools : bash-exec → sqlite-tool → telegram-tool sur un workflow réel", "cowork-testing", 8),
("[OC2-TOOLS-30] Documente tous les outils avec exemples d'utilisation et les intègre dans le QUICKSTART.md", "omega-docs-agent", 7),

# ══════════════════════════════════════════════════════════════════
# BLOC AGENT — Configuration agents spécialisés (40 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-AGENT-01] Configure codeur-agent pour scanner automatiquement Codeur.com toutes les 2h, filtrer les projets Python/IA score>30, stocker en DB", "codeur-agent", 9),
("[OC2-AGENT-02] Configure linkedin-agent pour surveiller les notifications LinkedIn via Chrome CDP et générer des réponses aux commentaires", "linkedin-agent", 9),
("[OC2-AGENT-03] Configure trading-engine pour récupérer le prix BTC/ETH depuis CoinGecko API, calculer variation 24h et envoyer alerte si >5%", "trading-engine", 8),
("[OC2-AGENT-04] Configure linux-admin pour effectuer un audit système complet (CPU/RAM/disk/services/ports ouverts) et envoyer rapport Telegram", "linux-admin", 8),
("[OC2-AGENT-05] Configure omega-security-agent pour scanner les ports ouverts, vérifier les permissions fichiers sensibles et détecter les anomalies", "omega-security-agent", 9),
("[OC2-AGENT-06] Configure omega-system-agent pour auditer les containers Docker (running/stopped/OOM/crash-loop) et corriger automatiquement", "omega-system-agent", 8),
("[OC2-AGENT-07] Configure monitoring pour collecter et aggréger toutes les métriques JARVIS dans un tableau de bord unifié", "monitoring", 8),
("[OC2-AGENT-08] Configure cluster-mgr pour surveiller la santé de M2/OL1 et rerouter automatiquement en cas de panne", "cluster-mgr", 9),
("[OC2-AGENT-09] Configure omega-analysis-agent avec un corpus de contexte JARVIS pour qu'il comprenne l'architecture du système", "omega-analysis-agent", 7),
("[OC2-AGENT-10] Configure omega-dev-agent pour générer du code conforme aux standards JARVIS (type hints, docstrings, error handling)", "omega-dev-agent", 8),
("[OC2-AGENT-11] Configure comms pour router intelligemment les notifications (urgent→Telegram immédiat, info→batch toutes les 2h)", "comms", 8),
("[OC2-AGENT-12] Configure voice-engine pour la synthèse TTS des alertes critiques via piper-fr", "voice-engine", 7),
("[OC2-AGENT-13] Configure browser-ops pour ouvrir/contrôler Chrome CDP et extraire le contenu de pages web authentifiées", "browser-ops", 7),
("[OC2-AGENT-14] Configure memory-manager pour indexer et rendre recherchable tout le contenu des sessions passées", "memory-manager", 7),
("[OC2-AGENT-15] Configure mail-agent avec les credentials IMAP/SMTP et teste l'envoi/réception de mails", "mail-agent", 7),
("[OC2-AGENT-16] Crée le prompt système complet pour codeur-agent : expertise freelance, style candidature, critères sélection", "codeur-agent", 8),
("[OC2-AGENT-17] Crée le prompt système complet pour linkedin-agent : ton professionnel Franc Delmas, thèmes IA/freelance", "linkedin-agent", 8),
("[OC2-AGENT-18] Crée le prompt système complet pour trading-engine : analyse technique, gestion risque, format alerte", "trading-engine", 8),
("[OC2-AGENT-19] Crée le prompt système complet pour omega-security-agent : protocoles sécurité, format rapport CVE", "omega-security-agent", 8),
("[OC2-AGENT-20] Crée le prompt système complet pour linux-admin : expertise sysadmin, format rapport système", "linux-admin", 7),
("[OC2-AGENT-21] Configure ai-engine pour router les requêtes LLM vers le modèle optimal selon la complexité estimée de la tâche", "ai-engine", 8),
("[OC2-AGENT-22] Configure ops-sre pour détecter et auto-réparer les problèmes récurrents (services down, disk full, zombies)", "ops-sre", 8),
("[OC2-AGENT-23] Configure sys-ops pour gérer le cycle de vie complet des processus système (start/stop/restart/monitor)", "sys-ops", 7),
("[OC2-AGENT-24] Configure automation pour orchestrer des workflows multi-étapes avec dépendances entre tâches", "automation", 8),
("[OC2-AGENT-25] Configure cowork-codegen pour générer du code Python de qualité production avec tests unitaires inclus", "cowork-codegen", 8),
("[OC2-AGENT-26] Configure cowork-refactor pour analyser le code existant et proposer des améliorations concrètes", "cowork-refactor", 7),
("[OC2-AGENT-27] Configure cowork-testing pour générer des tests unitaires et d'intégration automatiquement", "cowork-testing", 8),
("[OC2-AGENT-28] Configure cowork-git pour gérer les commits, branches et PRs de manière autonome", "cowork-git", 7),
("[OC2-AGENT-29] Configure cowork-docs pour générer et maintenir la documentation technique de JARVIS", "cowork-docs", 7),
("[OC2-AGENT-30] Configure cowork-monitor pour surveiller les métriques de qualité code (coverage, complexité, duplication)", "cowork-monitor", 7),
("[OC2-AGENT-31] Crée un agent 'opportunity-hunter' dédié à la veille Codeur/DevPost/LinkedIn pour trouver des opportunités business", "opportunity-hunter", 8),
("[OC2-AGENT-32] Crée un agent 'content-creator' spécialisé dans la génération de posts LinkedIn avec rotation de sujets", "linkedin-agent", 8),
("[OC2-AGENT-33] Crée un agent 'system-healer' qui détecte et répare automatiquement les problèmes système JARVIS", "ops-sre", 8),
("[OC2-AGENT-34] Crée un agent 'knowledge-base' qui indexe toutes les connaissances JARVIS pour répondre aux questions", "memory-manager", 7),
("[OC2-AGENT-35] Crée un agent 'deployment-manager' pour gérer les mises à jour de JARVIS sans interruption de service", "cowork-deploy", 7),
("[OC2-AGENT-36] Teste chaque agent avec 3 requêtes représentatives de son domaine et valide la qualité des réponses", "master", 9),
("[OC2-AGENT-37] Crée des métriques de qualité par agent : taux de tâches réussies, satisfaction score, temps de réponse moyen", "monitoring", 7),
("[OC2-AGENT-38] Implémente l'auto-escalade : si un agent échoue 3 fois, escalader automatiquement à omega-analysis-agent", "master", 8),
("[OC2-AGENT-39] Crée un registre d'agents avec leurs capacités, limitations et cas d'usage optimaux", "omega-docs-agent", 7),
("[OC2-AGENT-40] Valide que tous les 68 agents ont un defaultModelId local configuré et répondent correctement", "master", 9),

# ══════════════════════════════════════════════════════════════════
# BLOC PIPE — Pipelines et workflows (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-PIPE-01] Crée et teste le pipeline morning-routine complet : 7h00 briefing cluster + scan Codeur + LinkedIn notifs → 1 résumé Telegram", "automation", 9),
("[OC2-PIPE-02] Crée et teste le pipeline content-publish : génération post → validation qualité → sauvegarde → notification Telegram", "automation", 8),
("[OC2-PIPE-03] Crée et teste le pipeline apply-codeur : scan projets → score ≥30 → génération candidature → envoi validation Telegram", "codeur-agent", 9),
("[OC2-PIPE-04] Crée et teste le pipeline self-heal : détection erreur → analyse logs → proposition fix → application si confiance ≥80%", "ops-sre", 8),
("[OC2-PIPE-05] Crée et teste le pipeline cluster-optimize : benchmark → identification goulots → reconfiguration weights → validation", "cluster-mgr", 8),
("[OC2-PIPE-06] Crée et teste le pipeline weekly-report : agrégation stats 7j → rapport structuré → envoi Telegram dimanche 20h", "monitoring", 8),
("[OC2-PIPE-07] Crée et teste le pipeline code-review : analyse code → détection bugs → suggestions → score qualité 0-10", "cowork-refactor", 7),
("[OC2-PIPE-08] Crée et teste le pipeline backup-verify : backup DBs → vérification intégrité → notification résultat", "sys-ops", 8),
("[OC2-PIPE-09] Crée et teste le pipeline opportunity-scanner : Codeur + DevPost + LinkedIn → agrégat top 10 opportunités", "opportunity-hunter", 8),
("[OC2-PIPE-10] Crée et teste le pipeline security-audit : scan ports → analyse logs → vérification permissions → rapport", "omega-security-agent", 8),
("[OC2-PIPE-11] Crée un pipeline trading-alert : BTC/ETH prix → analyse technique → signal buy/sell → alerte Telegram si signal fort", "trading-engine", 8),
("[OC2-PIPE-12] Crée un pipeline linkedin-engage : nouvelles notifications → analyse commentaires → génération réponses → validation", "linkedin-agent", 8),
("[OC2-PIPE-13] Crée un pipeline devpost-monitor : scrape hackathons → filtre IA/Python/prize>1000$ → alerte nouveaux → sauvegarde", "opportunity-hunter", 7),
("[OC2-PIPE-14] Crée un pipeline auto-docs : changes Git détectées → génération docs → commit docs → notification", "cowork-docs", 7),
("[OC2-PIPE-15] Crée un pipeline incident-response : alerte critique → diagnostic → actions correctives → rapport post-mortem", "ops-sre", 8),
("[OC2-PIPE-16] Crée un pipeline cluster-health : ping tous nœuds → vérification modèles chargés → rapport → alerte si dégradé", "cluster-mgr", 9),
("[OC2-PIPE-17] Crée un pipeline daily-briefing : 8h00 statut cluster + projets Codeur actifs + posts LinkedIn du jour + météo IA", "automation", 8),
("[OC2-PIPE-18] Crée un pipeline task-completion : tâche terminée → vérification output → mise à jour DB → notification", "automation", 8),
("[OC2-PIPE-19] Crée un pipeline model-benchmark : test quotidien performance modèles → stockage métriques → alerte si dégradation", "ai-engine", 7),
("[OC2-PIPE-20] Crée un pipeline zombie-cleaner : détection zombies → identification parent → signal SIGCHLD → rapport nettoyage", "sys-ops", 7),
("[OC2-PIPE-21] Implémente la gestion des dépendances entre tâches : tâche B ne démarre que si tâche A est completed", "automation", 7),
("[OC2-PIPE-22] Crée un pipeline disk-monitor : vérification espace disque toutes les heures → alerte si <10% free → cleanup auto", "sys-ops", 7),
("[OC2-PIPE-23] Crée un pipeline gpu-thermal : surveillance températures GPU toutes les 5min → alerte si >80°C → throttling auto", "cluster-mgr", 8),
("[OC2-PIPE-24] Crée un pipeline freelance-analytics : agrégat données Codeur hebdo → calcul métriques → rapport performance", "codeur-agent", 7),
("[OC2-PIPE-25] Crée un pipeline linkedin-analytics : métriques posts (vues, likes, commentaires) → rapport hebdo → optimisation stratégie", "linkedin-agent", 7),
("[OC2-PIPE-26] Crée un pipeline auto-update : vérification nouvelles versions OpenClaw/JARVIS → test en staging → déploiement", "cowork-deploy", 7),
("[OC2-PIPE-27] Crée un pipeline context-refresh : résumé des sessions passées → mise à jour contexte agents → optimisation mémoire", "memory-manager", 7),
("[OC2-PIPE-28] Crée un pipeline error-learning : analyse erreurs récurrentes → mise à jour handlers → test corrections → commit", "omega-dev-agent", 8),
("[OC2-PIPE-29] Teste tous les pipelines créés avec des données réelles et valide les outputs", "cowork-testing", 9),
("[OC2-PIPE-30] Documente tous les pipelines avec schémas de flux et conditions d'activation", "omega-docs-agent", 7),

# ══════════════════════════════════════════════════════════════════
# BLOC MEM — Mémoire et contexte (25 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-MEM-01] Implémente un système de mémoire persistante par agent : chaque agent sauvegarde ses apprentissages dans ~/.openclaw/memory/agent-name/", "memory-manager", 9),
("[OC2-MEM-02] Crée un index de recherche sémantique sur les sessions passées avec nomic-embed-text", "memory-manager", 8),
("[OC2-MEM-03] Implémente le résumé automatique du contexte quand il dépasse 4000 tokens", "master", 8),
("[OC2-MEM-04] Crée un système de shared context entre agents d'un même pipeline", "master", 8),
("[OC2-MEM-05] Implémente la mémoire à long terme : faits importants persistés au-delà des sessions", "memory-manager", 8),
("[OC2-MEM-06] Crée un archivage automatique des sessions > 30 jours vers ~/.openclaw/archive/", "sys-ops", 7),
("[OC2-MEM-07] Implémente le cache LLM Redis avec TTL adaptatif selon la fraîcheur du contenu requis", "ai-engine", 8),
("[OC2-MEM-08] Crée un système de snapshots de l'état complet JARVIS (config + DB + sessions) pour rollback", "sys-ops", 8),
("[OC2-MEM-09] Teste que la mémoire des agents survit aux redémarrages du système", "cowork-testing", 8),
("[OC2-MEM-10] Crée un outil de recherche dans la mémoire : retrouve rapidement toute information passée", "memory-manager", 7),
("[OC2-MEM-11] Implémente la déduplication : évite de stocker le même fait deux fois en mémoire", "memory-manager", 7),
("[OC2-MEM-12] Crée un système de priorité mémoire : informations récentes et fréquentes ont priorité de rétention", "memory-manager", 7),
("[OC2-MEM-13] Implémente la mémoire de travail : contexte temporaire pour les tâches multi-étapes en cours", "master", 7),
("[OC2-MEM-14] Crée des métriques de mémoire : taille totale, entrées par agent, taux de hit cache", "monitoring", 7),
("[OC2-MEM-15] Implémente le garbage collection mémoire : supprime automatiquement les entrées obsolètes", "sys-ops", 6),
("[OC2-MEM-16] Crée un backup automatique journalier de toute la mémoire OpenClaw vers ~/IA/Core/jarvis/data/backups/", "sys-ops", 8),
("[OC2-MEM-17] Teste les performances du système de mémoire avec 10000 entrées simulées", "cowork-testing", 7),
("[OC2-MEM-18] Implémente la synchronisation de mémoire entre plusieurs sessions parallèles", "memory-manager", 7),
("[OC2-MEM-19] Crée un outil d'inspection de mémoire : liste les N entrées les plus récentes/importantes par agent", "memory-manager", 6),
("[OC2-MEM-20] Optimise la mémoire Redis : compression des valeurs > 1KB, TTL intelligent, éviction LRU", "ai-engine", 7),
("[OC2-MEM-21] Implémente un mécanisme de mémoire épisodique : se souvient d'événements importants avec timestamp", "memory-manager", 7),
("[OC2-MEM-22] Crée un système de mémoire partagée entre les agents d'un même domaine (ex: tous les agents cowork)", "memory-manager", 7),
("[OC2-MEM-23] Valide l'isolation mémoire : vérifier qu'un agent ne peut pas lire la mémoire d'un autre agent sans autorisation", "omega-security-agent", 8),
("[OC2-MEM-24] Implémente un historique des décisions : log des choix importants faits par les agents avec contexte", "memory-manager", 7),
("[OC2-MEM-25] Crée un rapport hebdomadaire d'utilisation mémoire avec recommandations d'optimisation", "monitoring", 6),

# ══════════════════════════════════════════════════════════════════
# BLOC MON — Monitoring et alertes (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-MON-01] Crée un tableau de bord système complet : CPU/RAM/GPU/disk/network en temps réel dans le terminal", "monitoring", 9),
("[OC2-MON-02] Implémente des alertes Telegram multicouches : INFO (batch 2h), WARN (batch 30min), CRITICAL (immédiat)", "comms", 8),
("[OC2-MON-03] Crée un log structuré JSON pour tous les agents avec : timestamp, agent_id, model_used, latency_ms, tokens, success", "monitoring", 9),
("[OC2-MON-04] Implémente la détection d'anomalies : alerte si latence >3x baseline ou taux d'erreur >20%", "monitoring", 8),
("[OC2-MON-05] Crée des métriques Prometheus-compatible pour OpenClaw (requêtes/sec, latence p50/p95/p99, erreurs)", "monitoring", 8),
("[OC2-MON-06] Implémente le monitoring des GPUs : température, VRAM utilisée, utilisation par modèle", "cluster-mgr", 8),
("[OC2-MON-07] Crée un système d'alertes graduées : WARNING → CRITICAL → EMERGENCY avec escalade automatique", "comms", 8),
("[OC2-MON-08] Implémente le monitoring des containers Docker : CPU/RAM/restarts/logs d'erreur", "omega-system-agent", 8),
("[OC2-MON-09] Crée des health checks automatiques toutes les 5min pour tous les services JARVIS critiques", "monitoring", 9),
("[OC2-MON-10] Implémente le monitoring des performances LLM : tokens/sec, temps TTFT, qualité réponse", "ai-engine", 7),
("[OC2-MON-11] Crée un historique des incidents avec timeline, cause racine, résolution, durée", "monitoring", 7),
("[OC2-MON-12] Implémente le monitoring des tâches OpenClaw : progression, taux de succès, temps moyen d'exécution", "monitoring", 8),
("[OC2-MON-13] Crée des alertes pour les ressources critiques : disk >85%, RAM >90%, GPU >85°C", "monitoring", 9),
("[OC2-MON-14] Implémente le monitoring des processus zombies avec alerte si >10 zombies détectés", "sys-ops", 7),
("[OC2-MON-15] Crée un rapport de disponibilité (uptime) pour chaque service JARVIS sur 30 jours glissants", "monitoring", 7),
("[OC2-MON-16] Implémente le monitoring de la qualité réseau vers M2 (latence, packet loss, bandwidth)", "cluster-mgr", 7),
("[OC2-MON-17] Crée un système de corrélation d'alertes : regrouper les alertes liées au même incident", "monitoring", 7),
("[OC2-MON-18] Implémente le monitoring de l'utilisation des APIs externes (OpenRouter, Telegram, Codeur)", "monitoring", 7),
("[OC2-MON-19] Crée un dashboard dédié pour le trading : prix crypto, signaux, P&L simulé", "trading-engine", 7),
("[OC2-MON-20] Implémente le monitoring de la mémoire Redis : utilisation, evictions, fragmentation", "ai-engine", 7),
("[OC2-MON-21] Crée des SLOs (Service Level Objectives) pour OpenClaw : disponibilité 99%, latence <5s p95", "monitoring", 7),
("[OC2-MON-22] Implémente le monitoring du coût API : tokens OpenRouter consommés, estimation coût journalier", "monitoring", 7),
("[OC2-MON-23] Crée un système de on-call automatique : Telegram alert avec boutons d'action directs", "comms", 8),
("[OC2-MON-24] Implémente le tracking des performances business : candidatures Codeur envoyées, taux réponse, LinkedIn engagement", "monitoring", 7),
("[OC2-MON-25] Crée un rapport de tendances hebdomadaires : améliorations notées, régressions détectées, recommandations", "monitoring", 7),
("[OC2-MON-26] Implémente le monitoring de la fraîcheur des données : alerter si les données de veille ont >24h", "monitoring", 6),
("[OC2-MON-27] Crée un système de logging distribué : tous les agents envoient leurs logs vers un collecteur central", "monitoring", 7),
("[OC2-MON-28] Implémente le monitoring des cronjobs : détecte les crons qui n'ont pas tourné à l'heure prévue", "monitoring", 7),
("[OC2-MON-29] Crée des tableaux de bord Telegram : /status, /metrics, /logs, /tasks commands via bot", "comms", 8),
("[OC2-MON-30] Valide que le système de monitoring lui-même est monitoré (quis custodiet ipsos custodes)", "monitoring", 7),

# ══════════════════════════════════════════════════════════════════
# BLOC SEC — Sécurité et hardening (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-SEC-01] Audit complet de sécurité OpenClaw : vérification de toutes les surfaces d'attaque possibles", "omega-security-agent", 9),
("[OC2-SEC-02] Implémente la validation stricte de tous les inputs agents : injection, path traversal, XSS, command injection", "omega-security-agent", 9),
("[OC2-SEC-03] Crée un système de rotation automatique des tokens LM Studio (tous les 30 jours)", "omega-security-agent", 8),
("[OC2-SEC-04] Vérifie que les secrets ne sont jamais loggés en clair dans aucun fichier de log", "omega-security-agent", 9),
("[OC2-SEC-05] Implémente le rate limiting par IP sur l'API OpenClaw :18789 (max 60 req/min)", "ops-sre", 8),
("[OC2-SEC-06] Audit des containers Docker : no-privileged, non-root user, read-only filesystem là où possible", "omega-security-agent", 8),
("[OC2-SEC-07] Crée une politique de moindre privilège pour chaque agent : accès minimum nécessaire seulement", "omega-security-agent", 8),
("[OC2-SEC-08] Implémente la détection d'intrusion : alerter si tentative d'accès non autorisé à l'API", "omega-security-agent", 8),
("[OC2-SEC-09] Teste la résistance aux prompt injections : 20 tests d'injection sur différents agents", "omega-security-agent", 9),
("[OC2-SEC-10] Crée un audit trail complet : chaque action significative est loggée avec qui/quoi/quand/pourquoi", "omega-security-agent", 8),
("[OC2-SEC-11] Vérifie que bash-exec.py bloque correctement toutes les commandes dangereuses (rm -rf, chmod 777, curl|bash, etc.)", "omega-security-agent", 9),
("[OC2-SEC-12] Implémente le chiffrement des données sensibles en mémoire et dans Redis", "omega-security-agent", 7),
("[OC2-SEC-13] Crée une sandbox renforcée pour l'exécution de code généré par les agents", "omega-security-agent", 8),
("[OC2-SEC-14] Audit des permissions fichiers : ~/.openclaw/ doit être chmod 700, les scripts 750", "omega-security-agent", 7),
("[OC2-SEC-15] Implémente la vérification d'intégrité des scripts JARVIS critiques (hash SHA256)", "omega-security-agent", 7),
("[OC2-SEC-16] Crée un système de détection des comportements anormaux des agents (sorties inattendues)", "omega-security-agent", 8),
("[OC2-SEC-17] Teste que les credentials API ne sont jamais exposés dans les réponses des agents", "omega-security-agent", 8),
("[OC2-SEC-18] Implémente HTTPS/TLS pour l'API OpenClaw (certificat auto-signé pour usage local)", "ops-sre", 7),
("[OC2-SEC-19] Crée une politique de backup sécurisé avec chiffrement des archives", "omega-security-agent", 7),
("[OC2-SEC-20] Audit de la surface d'attaque réseau : quels ports sont ouverts et pourquoi", "omega-security-agent", 8),
("[OC2-SEC-21] Implémente la validation des URLs avant fetch (pas de SSRF, pas d'IPs privées sauf cluster)", "omega-security-agent", 8),
("[OC2-SEC-22] Crée des tests de pénétration automatisés pour l'API OpenClaw", "omega-security-agent", 7),
("[OC2-SEC-23] Vérifie que les modèles LLM locaux ne peuvent pas exfiltrer de données vers l'extérieur", "omega-security-agent", 8),
("[OC2-SEC-24] Implémente la limitation de la taille des inputs pour éviter les attaques DoS", "omega-security-agent", 7),
("[OC2-SEC-25] Crée un rapport mensuel de sécurité automatique avec métriques et recommandations", "omega-security-agent", 7),
("[OC2-SEC-26] Vérifie qu'aucun fichier .env ou .key n'est accessible via l'API OpenClaw", "omega-security-agent", 9),
("[OC2-SEC-27] Implémente l'authentification sur l'API OpenClaw :18789 (token Bearer)", "ops-sre", 7),
("[OC2-SEC-28] Crée un système de révocation d'accès : désactiver rapidement un agent compromis", "omega-security-agent", 7),
("[OC2-SEC-29] Audit des dépendances Python : vérifier CVEs connues dans les packages utilisés", "omega-security-agent", 8),
("[OC2-SEC-30] Valide que le système de sécurité complet fonctionne end-to-end avec un test scenario réaliste", "omega-security-agent", 9),

# ══════════════════════════════════════════════════════════════════
# BLOC PERF — Performance et optimisation (25 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-PERF-01] Profile cowork_engine.py et identifie les 5 fonctions les plus lentes, propose des optimisations", "omega-dev-agent", 9),
("[OC2-PERF-02] Ajoute des INDEX SQLite sur les colonnes fréquemment requêtées : status, scheduled_time, agent, priority", "cowork-codegen", 8),
("[OC2-PERF-03] Implémente le connection pooling pour SQLite avec max 5 connexions simultanées", "omega-dev-agent", 8),
("[OC2-PERF-04] Benchmark throughput OpenClaw : mesure le nombre max de requêtes/sec avant dégradation", "monitoring", 8),
("[OC2-PERF-05] Optimise les prompts des 10 agents les plus utilisés pour réduire les tokens de 20%", "omega-docs-agent", 7),
("[OC2-PERF-06] Implémente le lazy loading : ne charger les modules agents que quand ils sont nécessaires", "master", 7),
("[OC2-PERF-07] Compresse et archive les logs > 7 jours, libère l'espace disque", "sys-ops", 7),
("[OC2-PERF-08] Optimise les requêtes SQLite les plus fréquentes avec EXPLAIN QUERY PLAN", "cowork-codegen", 7),
("[OC2-PERF-09] Implémente l'async pour tous les appels LLM (remplace requests par aiohttp)", "omega-dev-agent", 8),
("[OC2-PERF-10] Crée un profiler de tâches : mesure le temps réel d'exécution de chaque type de tâche", "monitoring", 7),
("[OC2-PERF-11] Optimise le startup time d'OpenClaw : vise < 3 secondes pour être opérationnel", "omega-dev-agent", 7),
("[OC2-PERF-12] Implémente le prefetching : préparer les modèles LLM avant que les tâches arrivent", "ai-engine", 7),
("[OC2-PERF-13] Optimise l'utilisation VRAM : décharger les modèles inactifs après 10 minutes", "cluster-mgr", 8),
("[OC2-PERF-14] Crée un système de priorité de tâches dynamique basé sur l'urgence et l'impact", "automation", 7),
("[OC2-PERF-15] Optimise le dispatcher de tâches : traitement en batch de 10 tâches au lieu de 1 par 1", "automation", 8),
("[OC2-PERF-16] Implémente la mise en cache des résultats de scraping Codeur/DevPost pendant 2h", "codeur-agent", 7),
("[OC2-PERF-17] Optimise les requêtes Redis : pipeline les commandes multiples en une seule transaction", "ai-engine", 7),
("[OC2-PERF-18] Crée un benchmark continu : mesure les performances tous les jours et stocke les tendances", "monitoring", 7),
("[OC2-PERF-19] Optimise le réseau : activer HTTP/2 pour les appels aux APIs LLM locales", "omega-dev-agent", 6),
("[OC2-PERF-20] Implémente le deduplication des tâches : évite d'exécuter la même tâche deux fois en parallèle", "automation", 7),
("[OC2-PERF-21] Optimise l'utilisation mémoire Python : identifie les memory leaks avec tracemalloc", "omega-dev-agent", 7),
("[OC2-PERF-22] Crée des pools de threads dédiés par type de tâche (I/O-bound vs CPU-bound)", "omega-dev-agent", 7),
("[OC2-PERF-23] Implémente le warm-up des modèles LLM au démarrage pour éviter le cold start", "ai-engine", 7),
("[OC2-PERF-24] Optimise le format de stockage des sessions : compression + format binaire pour grandes sessions", "memory-manager", 6),
("[OC2-PERF-25] Valide les gains de performance avec un benchmark before/after sur 20 métriques clés", "monitoring", 8),

# ══════════════════════════════════════════════════════════════════
# BLOC BUSI — Business Codeur/LinkedIn/DevPost (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-BUSI-01] Analyse les 50 derniers projets Codeur.com en DB et identifie les patterns des projets les mieux payés", "omega-analysis-agent", 9),
("[OC2-BUSI-02] Génère 5 templates de candidature Codeur optimisés pour les domaines IA/Python/Automatisation/LLM/DevOps", "codeur-agent", 8),
("[OC2-BUSI-03] Crée un système de scoring automatique des projets Codeur : calcule un score 0-100 basé sur budget, complexité, compétences requises", "codeur-agent", 9),
("[OC2-BUSI-04] Génère le planning LinkedIn pour les 14 prochains jours : sujets, angles, hashtags, heures optimales de publication", "linkedin-agent", 8),
("[OC2-BUSI-05] Analyse les 5 derniers posts LinkedIn générés et propose des améliorations pour augmenter l'engagement", "omega-analysis-agent", 8),
("[OC2-BUSI-06] Crée 10 posts LinkedIn complets sur les thèmes : GPU cluster, LLM locaux, freelance IA, automatisation, trading algo", "linkedin-agent", 8),
("[OC2-BUSI-07] Recherche et liste les 20 hackathons DevPost actifs avec prize > 1000$, triés par pertinence IA/Python", "opportunity-hunter", 8),
("[OC2-BUSI-08] Crée des templates de propositions complètes pour les 5 types de missions freelance les plus fréquents", "codeur-agent", 7),
("[OC2-BUSI-09] Analyse le marché freelance IA sur Codeur.com : tendances, TJM moyen, compétences les plus demandées", "omega-analysis-agent", 8),
("[OC2-BUSI-10] Crée un système de suivi des candidatures : tracking statut, relances automatiques, analyse taux de réponse", "codeur-agent", 8),
("[OC2-BUSI-11] Génère un rapport de stratégie LinkedIn pour les 30 prochains jours basé sur les meilleures pratiques 2026", "linkedin-agent", 7),
("[OC2-BUSI-12] Crée un système de veille concurrentielle : surveille les profils LinkedIn concurrents et identifie les tendances", "omega-analysis-agent", 7),
("[OC2-BUSI-13] Génère 5 études de cas détaillées sur des projets IA réussis à présenter comme références clients", "linkedin-agent", 7),
("[OC2-BUSI-14] Crée un calendrier éditorial automatique pour LinkedIn basé sur les actualités IA de la semaine", "linkedin-agent", 7),
("[OC2-BUSI-15] Analyse l'efficacité des hashtags LinkedIn : lesquels génèrent le plus de visibilité pour le profil Franc Delmas", "omega-analysis-agent", 7),
("[OC2-BUSI-16] Crée un bot de veille DevPost : notifie Telegram dès qu'un hackathon IA avec prize >5000$ est publié", "opportunity-hunter", 8),
("[OC2-BUSI-17] Génère un pitch deck complet pour les prestations de services IA (format Telegram/slides)", "linkedin-agent", 7),
("[OC2-BUSI-18] Crée un système de tarification dynamique des missions freelance basé sur la complexité et le délai", "codeur-agent", 7),
("[OC2-BUSI-19] Analyse les commentaires LinkedIn reçus et génère des réponses personnalisées et engageantes", "linkedin-agent", 8),
("[OC2-BUSI-20] Crée un rapport mensuel business : revenus potentiels Codeur, engagement LinkedIn, hackathons participés", "monitoring", 7),
("[OC2-BUSI-21] Génère une liste de 50 prospects potentiels sur LinkedIn (startups tech, scale-ups ayant besoin d'IA)", "linkedin-agent", 7),
("[OC2-BUSI-22] Crée un système de nurturing automatique : séquence de 5 messages pour convertir un prospect LinkedIn en client", "linkedin-agent", 6),
("[OC2-BUSI-23] Analyse les tendances IA sur Twitter/X et identifie les sujets viraux pour alimenter les posts LinkedIn", "omega-analysis-agent", 7),
("[OC2-BUSI-24] Crée un portfolio automatique : génère un document PDF avec les meilleurs projets et résultats", "linkedin-agent", 7),
("[OC2-BUSI-25] Implémente un A/B test sur les candidatures Codeur : compare 2 styles et mesure le taux de réponse", "codeur-agent", 7),
("[OC2-BUSI-26] Crée une base de connaissances des technologies IA pour enrichir les candidatures et posts", "memory-manager", 7),
("[OC2-BUSI-27] Génère des cas d'usage concrets de JARVIS/OpenClaw pour les transformer en contenus marketing", "linkedin-agent", 7),
("[OC2-BUSI-28] Crée un système d'alerte pour les appels d'offres publics liés à l'IA sur les plateformes gouvernementales", "opportunity-hunter", 7),
("[OC2-BUSI-29] Analyse et optimise le profil LinkedIn Franc Delmas pour maximiser la visibilité dans les recherches", "omega-analysis-agent", 7),
("[OC2-BUSI-30] Crée un tableau de bord business complet : KPIs freelance, objectifs, progression, projections", "monitoring", 8),

# ══════════════════════════════════════════════════════════════════
# BLOC CODE — Génération code et refactoring (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-CODE-01] Génère des tests unitaires complets pour task-executor.py avec pytest et mocks appropriés", "cowork-testing", 9),
("[OC2-CODE-02] Génère des tests unitaires pour oc-task-dispatcher.py couvrant les cas d'erreur et timeout", "cowork-testing", 8),
("[OC2-CODE-03] Génère des tests d'intégration pour le pipeline complet : injection tâche → dispatch → exécution → vérification", "cowork-testing", 8),
("[OC2-CODE-04] Refactorise content-machine.py pour utiliser le nouveau fallback cascade M2→OL1→OpenRouter", "cowork-refactor", 8),
("[OC2-CODE-05] Refactorise daily-briefing.py pour agréger les données de tous les agents en un seul message structuré", "cowork-refactor", 7),
("[OC2-CODE-06] Génère un wrapper Python complet pour l'API OpenClaw avec documentation et exemples", "cowork-codegen", 8),
("[OC2-CODE-07] Crée un CLI Python pour gérer les tâches OpenClaw depuis le terminal (list/add/done/retry)", "cowork-codegen", 8),
("[OC2-CODE-08] Génère le code complet d'un agent de monitoring autonome avec auto-correction", "cowork-codegen", 8),
("[OC2-CODE-09] Refactorise lm-query-safe.py pour utiliser asyncio et supporter les requêtes parallèles", "cowork-refactor", 7),
("[OC2-CODE-10] Génère le code d'un système de task queue distribué compatible avec Redis", "cowork-codegen", 7),
("[OC2-CODE-11] Crée un decorator Python @retry(max=3, backoff=2) réutilisable pour tous les appels réseau", "cowork-codegen", 8),
("[OC2-CODE-12] Génère le code d'un logger structuré JSON compatible avec les outils de monitoring", "cowork-codegen", 7),
("[OC2-CODE-13] Refactorise codeur-auto-apply.py pour supporter le batch de 10 candidatures et la déduplication", "cowork-refactor", 8),
("[OC2-CODE-14] Génère le code d'un rate limiter générique avec fenêtre glissante et backoff exponentiel", "cowork-codegen", 8),
("[OC2-CODE-15] Crée un générateur de code Python de configuration YAML/JSON avec validation de schéma", "cowork-codegen", 7),
("[OC2-CODE-16] Refactorise devpost-scraper.py avec pagination, retry et stockage incrémental en DB", "cowork-refactor", 7),
("[OC2-CODE-17] Génère le code d'un circuit breaker pattern réutilisable pour tous les providers LLM", "cowork-codegen", 8),
("[OC2-CODE-18] Crée un système de versionning des prompts pour tracer les changements et leur impact", "cowork-codegen", 7),
("[OC2-CODE-19] Génère un SDK Python complet pour intégrer OpenClaw dans n'importe quel script Python", "cowork-codegen", 8),
("[OC2-CODE-20] Refactorise linkedin-interactions.py pour supporter la lecture des commentaires et likes via CDP", "cowork-refactor", 7),
("[OC2-CODE-21] Génère le code d'un task scheduler cron-compatible avec support des expressions cron", "cowork-codegen", 7),
("[OC2-CODE-22] Crée des fixtures de test réutilisables pour tous les tests OpenClaw (DB mockée, agents mockés)", "cowork-testing", 7),
("[OC2-CODE-23] Génère le code d'un health check endpoint HTTP pour chaque service JARVIS", "cowork-codegen", 7),
("[OC2-CODE-24] Refactorise self-improve.py pour créer automatiquement des PRs avec les corrections identifiées", "cowork-refactor", 7),
("[OC2-CODE-25] Génère le code d'un système de feature flags pour activer/désactiver des fonctionnalités JARVIS", "cowork-codegen", 7),
("[OC2-CODE-26] Crée des type stubs Python pour toutes les interfaces OpenClaw (améliore l'autocomplétion IDE)", "cowork-docs", 6),
("[OC2-CODE-27] Génère le code d'un système de plugin pour étendre OpenClaw sans modifier le core", "cowork-codegen", 7),
("[OC2-CODE-28] Refactorise task-executor.py pour supporter les tâches dépendantes (A doit finir avant B)", "cowork-refactor", 8),
("[OC2-CODE-29] Génère la suite de tests complète E2E pour le workflow morning-routine", "cowork-testing", 7),
("[OC2-CODE-30] Crée un générateur automatique de documentation API à partir des docstrings Python", "cowork-docs", 7),

# ══════════════════════════════════════════════════════════════════
# BLOC INFRA — Docker/systemd/cron/déploiement (30 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-INFRA-01] Crée un docker-compose.yml optimisé pour tous les services JARVIS avec health checks et restart policies", "omega-system-agent", 9),
("[OC2-INFRA-02] Crée des systemd user services pour tous les scripts JARVIS en daemon : task-executor, proactive-monitor, oc-dispatcher", "ops-sre", 8),
("[OC2-INFRA-03] Crée un crontab complet et optimisé pour tous les jobs planifiés JARVIS (journalier, hebdo, horaire)", "automation", 8),
("[OC2-INFRA-04] Crée un Dockerfile optimisé pour OpenClaw avec multi-stage build et minimal image size", "omega-system-agent", 7),
("[OC2-INFRA-05] Implémente le démarrage automatique de tous les services au boot via systemd enable", "ops-sre", 9),
("[OC2-INFRA-06] Crée un script de déploiement zero-downtime pour les mises à jour JARVIS", "cowork-deploy", 8),
("[OC2-INFRA-07] Configure les logs systemd avec journald : rotation automatique, max 500MB total", "ops-sre", 7),
("[OC2-INFRA-08] Crée un script de backup complet : DBs + config + sessions → archive chiffrée → stockage externe", "sys-ops", 8),
("[OC2-INFRA-09] Implémente le monitoring systemd : alerter si un service passe en failed state", "monitoring", 8),
("[OC2-INFRA-10] Crée un réseau Docker dédié pour JARVIS avec isolation et communication inter-containers", "omega-system-agent", 7),
("[OC2-INFRA-11] Configure les variables d'environnement Docker Compose depuis le fichier .env", "omega-system-agent", 7),
("[OC2-INFRA-12] Crée des volumes Docker nommés pour la persistance des données JARVIS", "omega-system-agent", 7),
("[OC2-INFRA-13] Implémente les ressource limits Docker : CPU et RAM max par container pour éviter les OOM", "omega-system-agent", 8),
("[OC2-INFRA-14] Crée un script de recovery complet en cas de panne totale : restore depuis backup + restart services", "ops-sre", 8),
("[OC2-INFRA-15] Configure les health checks Docker : readiness et liveness probes pour chaque service", "omega-system-agent", 8),
("[OC2-INFRA-16] Crée des timers systemd pour remplacer les crons : meilleure gestion des erreurs et logs", "ops-sre", 7),
("[OC2-INFRA-17] Implémente le cleanup automatique Docker : suppression images/containers/volumes inutilisés", "sys-ops", 7),
("[OC2-INFRA-18] Crée un manifest d'infrastructure complet documentant tous les services et leurs dépendances", "omega-docs-agent", 7),
("[OC2-INFRA-19] Configure le restart automatique des services après crash avec backoff exponentiel", "ops-sre", 8),
("[OC2-INFRA-20] Implémente le graceful shutdown pour tous les services (SIGTERM handler)", "omega-dev-agent", 8),
("[OC2-INFRA-21] Crée un script d'installation complète de JARVIS sur une nouvelle machine (< 30 minutes)", "cowork-deploy", 8),
("[OC2-INFRA-22] Configure le sysctl kernel pour optimiser les performances réseau et I/O pour JARVIS", "linux-admin", 7),
("[OC2-INFRA-23] Crée des alertes systemd : unit failed, restart loops, disk full → Telegram", "monitoring", 7),
("[OC2-INFRA-24] Implémente le versioning des configs avec git : chaque changement de config est commité", "cowork-git", 7),
("[OC2-INFRA-25] Crée un environnement de staging pour tester les changements avant production", "cowork-deploy", 7),
("[OC2-INFRA-26] Configure le load balancing entre M2 et OL1 avec nginx comme reverse proxy", "ops-sre", 7),
("[OC2-INFRA-27] Crée un script de smoke test post-déploiement : vérifie que tout fonctionne en 2 minutes", "cowork-testing", 8),
("[OC2-INFRA-28] Implémente la gestion des secrets avec un vault simple (pas de secrets en plain text dans configs)", "omega-security-agent", 8),
("[OC2-INFRA-29] Crée un runbook complet pour les opérations courantes JARVIS (restart, backup, debug, scaling)", "omega-docs-agent", 7),
("[OC2-INFRA-30] Valide l'infrastructure complète avec un test de résilience : kill aléatoire de services et mesure recovery time", "ops-sre", 8),

# ══════════════════════════════════════════════════════════════════
# BLOC DATA — SQL/Redis/cache/données (25 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-DATA-01] Optimise le schéma de openclaw_tasks : ajoute retry_count, last_error, output_size, duration_ms", "cowork-codegen", 9),
("[OC2-DATA-02] Crée des vues SQL utiles : tasks_pending_now, tasks_failing, agent_performance, daily_stats", "cowork-codegen", 8),
("[OC2-DATA-03] Implémente une migration SQL automatique : versioning du schéma avec Alembic-style", "cowork-codegen", 8),
("[OC2-DATA-04] Crée un système d'archivage : tasks completed > 30j → table archive_tasks (évite DB bloat)", "cowork-codegen", 7),
("[OC2-DATA-05] Optimise Redis : configurer maxmemory-policy allkeys-lru, persist RDB quotidien", "ai-engine", 7),
("[OC2-DATA-06] Crée un système de métriques temps réel dans Redis : compteurs par agent, latence rolling average", "ai-engine", 8),
("[OC2-DATA-07] Implémente le sharding des sessions : sessions > 1MB stockées en filesystem, référence dans SQLite", "memory-manager", 7),
("[OC2-DATA-08] Crée des procédures de nettoyage automatique : sessions orphelines, tasks stuck > 1h en running", "sys-ops", 8),
("[OC2-DATA-09] Implémente la réplication SQLite vers S3/backup distant pour disaster recovery", "sys-ops", 7),
("[OC2-DATA-10] Crée un dashboard données : taille DBs, tables les plus grandes, croissance hebdomadaire", "monitoring", 7),
("[OC2-DATA-11] Optimise les requêtes les plus fréquentes avec EXPLAIN et ajout d'INDEX appropriés", "cowork-codegen", 8),
("[OC2-DATA-12] Crée un ETL pour importer les données historiques Codeur.com dans un format analytique", "services-data", 7),
("[OC2-DATA-13] Implémente le versioning des données : chaque modification importante est tracée avec diff", "cowork-codegen", 7),
("[OC2-DATA-14] Crée un système de quotas par agent : max tokens/jour, max tâches/heure", "ai-engine", 7),
("[OC2-DATA-15] Optimise le stockage des embeddings : utilise nomic-embed pour indexer et chercher dans les contenus", "ai-engine", 7),
("[OC2-DATA-16] Implémente la validation des données entrantes dans SQLite avec des contraintes CHECK", "cowork-codegen", 7),
("[OC2-DATA-17] Crée un système de données de référence : tables de lookup pour les agents, modèles, providers", "cowork-codegen", 6),
("[OC2-DATA-18] Implémente le monitoring de la santé SQLite : integrity check quotidien, vacuum hebdo", "monitoring", 7),
("[OC2-DATA-19] Crée des rapports d'analyse de données : tendances tasks, performance agents, utilisation modèles", "omega-analysis-agent", 7),
("[OC2-DATA-20] Optimise le cache Redis pour les résultats LLM : clé = hash(model+prompt), TTL adaptatif", "ai-engine", 8),
("[OC2-DATA-21] Crée un système de données temps réel : pub/sub Redis pour les événements JARVIS", "ai-engine", 7),
("[OC2-DATA-22] Implémente la compression des données : JSON minification + gzip pour les grandes sessions", "memory-manager", 6),
("[OC2-DATA-23] Crée une politique de rétention des données : 7j hot (Redis), 30j warm (SQLite), 1an cold (archive)", "sys-ops", 7),
("[OC2-DATA-24] Implémente un système de rollup des métriques : données brutes → agrégats horaires → agrégats journaliers", "monitoring", 7),
("[OC2-DATA-25] Valide l'intégrité complète de toutes les bases de données et répare les corruptions détectées", "ops-sre", 9),

# ══════════════════════════════════════════════════════════════════
# BLOC SELF — Auto-amélioration continue (25 tâches)
# ══════════════════════════════════════════════════════════════════
("[OC2-SELF-01] Implémente un boucle d'auto-amélioration : analyse erreurs → identifie pattern → génère fix → teste → commit si OK", "omega-dev-agent", 9),
("[OC2-SELF-02] Crée un système de scoring de qualité des agents : mesure automatiquement l'utilité de chaque réponse", "monitoring", 8),
("[OC2-SELF-03] Implémente l'apprentissage des préférences utilisateur : adapte le style de réponse selon les feedbacks", "master", 8),
("[OC2-SELF-04] Crée un système de suggestions proactives : analyse l'état du système et propose des améliorations", "omega-analysis-agent", 8),
("[OC2-SELF-05] Implémente la découverte automatique de nouvelles tâches utiles basée sur les patterns d'usage", "automation", 7),
("[OC2-SELF-06] Crée un système de détection des agents sous-performants et propose leur remplacement/amélioration", "monitoring", 7),
("[OC2-SELF-07] Implémente le ré-entraînement des prompts : A/B test automatique de nouvelles versions de prompts", "ai-engine", 7),
("[OC2-SELF-08] Crée un audit hebdomadaire automatique : rapport complet sur l'état d'OpenClaw et recommendations", "omega-analysis-agent", 8),
("[OC2-SELF-09] Implémente la détection des tâches redondantes : identifie et fusionne les tâches similaires", "automation", 7),
("[OC2-SELF-10] Crée un système de veille technologique automatique : surveille les nouveaux outils IA et évalue leur pertinence", "omega-analysis-agent", 7),
("[OC2-SELF-11] Implémente l'optimisation automatique des crons : ajuste les horaires selon la charge système", "automation", 7),
("[OC2-SELF-12] Crée un système de génération automatique de nouvelles tâches basé sur les objectifs business", "automation", 7),
("[OC2-SELF-13] Implémente le suivi des KPIs OpenClaw avec alertes si régression détectée", "monitoring", 8),
("[OC2-SELF-14] Crée un générateur automatique de rapports d'amélioration basé sur les données de monitoring", "omega-analysis-agent", 7),
("[OC2-SELF-15] Implémente la mise à jour automatique des prompts systèmes des agents basée sur les performances", "ai-engine", 7),
("[OC2-SELF-16] Crée un système de tests de régression automatiques : compare les performances avant/après changement", "cowork-testing", 8),
("[OC2-SELF-17] Implémente un tableau de bord de santé global OpenClaw avec score 0-100 calculé automatiquement", "monitoring", 8),
("[OC2-SELF-18] Crée un système de documentation automatique : chaque changement génère une entrée dans le changelog", "cowork-docs", 7),
("[OC2-SELF-19] Implémente la détection des dépendances cassées : vérifie régulièrement que tous les imports fonctionnent", "ops-sre", 8),
("[OC2-SELF-20] Crée un système de benchmarks continus : mesure 10 métriques clés chaque jour et stocke l'historique", "monitoring", 8),
("[OC2-SELF-21] Implémente l'auto-scaling des ressources : ajuste dynamiquement le nombre de workers selon la charge", "automation", 7),
("[OC2-SELF-22] Crée un bot d'amélioration Telegram : reçoit les feedbacks utilisateur et les transforme en tâches", "comms", 7),
("[OC2-SELF-23] Implémente la gestion du cycle de vie des tâches : création → scheduling → exécution → vérification → archivage", "automation", 8),
("[OC2-SELF-24] Crée un rapport trimestriel d'évolution d'OpenClaw : fonctionnalités ajoutées, performance améliorée, bugs résolus", "omega-docs-agent", 7),
("[OC2-SELF-25] Valide que le système d'auto-amélioration fonctionne end-to-end : injecte une erreur intentionnelle et vérifie qu'elle est détectée et corrigée automatiquement", "omega-dev-agent", 9),
]

# Injection
inserted = 0
for desc, agent, priority in TASKS:
    conn.execute(
        'INSERT INTO openclaw_tasks (description, agent, status, priority, scheduled_time) VALUES (?,?,?,?,?)',
        (desc, agent, 'pending', priority, NOW)
    )
    inserted += 1

conn.commit()

# Stats par bloc
print(f'✅ {inserted} tâches injectées')
blocs = {}
import re
for desc, agent, _ in TASKS:
    m = re.match(r'\[OC2-([A-Z]+)-', desc)
    if m:
        b = m.group(1)
        blocs[b] = blocs.get(b, 0) + 1
print('\nDistribution par bloc:')
for b, n in sorted(blocs.items()):
    print(f'  {b:8s}: {n:3d} tâches')

# Total DB
total = conn.execute("SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC%'").fetchone()[0]
pending = conn.execute("SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC%' AND status='pending'").fetchone()[0]
print(f'\nTotal OpenClaw tâches DB: {total} | Pending: {pending}')
conn.close()
