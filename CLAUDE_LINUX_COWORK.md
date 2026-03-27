# CLAUDE_LINUX_COWORK.md — JARVIS Linux
# Lu automatiquement au début de chaque session Claude Desktop

## IDENTITÉ
- Système : JARVIS Linux (Ubuntu)
- Utilisateur : Turbo (Franc Delmas)
- Langue : Français exclusivement
- TTS : fr-FR-DeniseNeural (Edge TTS)
- STT : Whisper CUDA turbo

## RÈGLES ABSOLUES
1. JAMAIS `localhost` → TOUJOURS `127.0.0.1`
2. Déléguer au cluster AVANT de répondre seul
3. Réponses ultra-compactes, structurées
4. Attribution systématique : [M1/qwen3] [OL1] [M2/deepseek] etc.
5. /nothink obligatoire sur M1, /no_think sur OL1
6. Ollama cloud : "think": false dans le body JSON

## CLUSTER LINUX
| Noeud | Adresse | Modèle | Poids |
|-------|---------|--------|-------|
| M1    | 127.0.0.1:1234  | qwen3-8b       | 1.9 |
| OL1   | 127.0.0.1:11434 | qwen3:1.7b     | 1.4 |
| M2    | 192.168.1.26:1234  | deepseek-r1 | 1.5 |
| M3    | 192.168.1.113:1234 | deepseek-r1 | 1.1 |
| GEMINI | API direct | gemini-flash   | 1.5 |
| CLAUDE | claude.ai  | sonnet-4       | 1.2 |

Fallback chain : M1 → OL1 → M2 → M3 → GEMINI → CLAUDE
Quorum consensus : score >= 0.65 → FORT

## HEALTH CHECK (à lancer en début de session)
```bash
bash ~/jarvis/scripts/health_check.sh
```
Vérifie : M1, OL1, M2, M3, Ollama, services vocaux, MCP handlers

## MCP HANDLERS LINUX (600+)
- Filesystem : lecture/écriture ~/jarvis/
- LM Studio : lm_query, lm_consensus
- Ollama : ollama_query
- Vocal : voice_command, tts_speak
- Telegram : send_alert, send_report
- Cluster : dispatch_task, monitor_nodes

## SERVICES BOOT
```
jarvis-core.service      → moteur principal
jarvis-voice.service     → pipeline STT/TTS
jarvis-mcp.service       → serveur MCP (port 8765)
jarvis-monitor.service   → monitoring cluster
jarvis-telegram.service  → bot alertes
```

## COWORK MODE
Rôle : monitoring + dispatch + audit + amélioration continue
- Scan sniper : toutes les 30s (MEXC Futures)
- Monitor cluster : toutes les 5min
- Alertes Telegram : seuils critiques
- NE JAMAIS modifier le code directement

## WORKFLOW COWORK
1. Health check cluster
2. Vérifier queue tâches
3. Dispatcher aux agents selon type
4. Compiler résultats + consensus
5. Logger en SQLite + rapport Telegram si critique

## FORMAT RÉPONSE COWORK
```
[STATUT] M1:OK | OL1:OK | M2:OK | M3:TIMEOUT | GEMINI:OK
[QUEUE] N tâches pending
[ACTION] description courte
[DISPATCH] tâche → agent → résultat
```
