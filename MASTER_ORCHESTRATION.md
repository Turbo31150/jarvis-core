# MASTER ORCHESTRATION — JARVIS-TURBO
> Version : 2026-04-14 | Modèle actif : claude-sonnet (économie tokens)
> Principe : 1 demande = 1 action concrète. Zéro question. Zéro token brûlé à réfléchir.

---

## LOI FONDAMENTALE

```
DEMANDE → FILTRE MOT-CLÉ → AGENT/SKILL/TOOL → ACTION → CHECKPOINT → SUIVANT
```

Jamais : demande → question → attente → token perdu.

---

## ARCHITECTURE DE DÉCHARGE (Token Offload Stack)

```
[Claude Sonnet] ← orchestrateur uniquement
      ↓
[lm-ask.sh]     ← résumé, extract, classify, doc        (0 token)
[lm-ask.sh --big]  ← code routinier, refactor            (0 token)
[lm-ask.sh --reason] ← debug, reasoning                  (0 token)
[OpenClaw agents]  ← tâches spécialisées                 (0 token)
[Ollama OL1]    ← kimi-k2.5:cloud, gemma3, deepseek-r1   (0 token)
[M1 distillés]  ← claude-opus-distilled (qualité Opus)   (0 token)
      ↓
[Claude Sonnet] ← décision finale uniquement
```

**Règle** : si lm-ask.sh peut le faire → déléguer. Toujours.

---

## DÉCLENCHEURS MOT-CLÉ → ACTION AUTO

| Mot-clé détecté | Action immédiate | Outil |
|---|---|---|
| status / health / système | `jarvis status` | Bash |
| gpu / vram / thermal | `jarvis-gpu status` | Bash |
| zombie / defunct | `jarvis-zombie kill` | Bash |
| sécurité / audit / port | `jarvis-security scan` | Bash |
| cluster / m1 / m2 / ol1 | `jarvis-cluster health` | Bash |
| résume / summarize / extrait | `lm-ask.sh "..."` | Bash |
| code / refactor / écris | `lm-ask.sh --big "..."` | Bash |
| debug / raisonne / pourquoi | `lm-ask.sh --reason "..."` | Bash |
| commit / push | Skill `commit` | Skill |
| linkedin / post / contenu | Skill `linkedinpilot` | Skill |
| codeur / mission | Skill `codeur` | Skill |
| hackathon / devpost | Agent `omega-analysis-agent` | Agent |
| trading / signal | Agent `omega-trading-agent` | Agent |
| feature / implémente | Agent `omega-dev-agent` | Agent |
| doc / documentation | Agent `omega-docs-agent` | Agent |
| infra / déploie | Agent `omega-system-agent` | Agent |

---

## JARVIS PARALLEL TASK ENGINE (JPTE) — TODOLIST AUTO-ALIMENTÉE

Chaque demande est décomposée en N tâches parallèles (max 10) gérées via SQLite `data/todolist.db`.

### Protocole JPTE — AUDIT → ACTION → CORRECTION

1. **DÉCOMPOSEUR** : Découpe demande → T1..T10 (JSON).
2. **SCOREUR** : Calcule complexité (1-5), priorité (P1-P3) et score initial.
3. **DISPATCHER** : Lance agents/skills/CLI en parallèle via `asyncio`.
4. **EXÉCUTEUR** : Boucle AUDIT (existant) → ACTION (agent) → CORRECTION (validation).
5. **AUTO-FEED** : Si score < 0.5 ou échec → génère sous-tâche de correction immédiate.
6. **COMPILATEUR** : Merge tous les outputs en résultat cohérent + mise à jour mémoire.

### Commandes JPTE

```bash
# Lancer une pipeline JPTE complète
cd scripts/jpte && ./jpte_run.py "votre demande ici"

# Consulter l'état des tâches actives
sqlite3 data/todolist.db "SELECT title, status, score_final FROM tasks WHERE status != 'done'"
```

---

## BLOC DE TÂCHE — STRUCTURE STANDARD

Chaque tâche suit ce pipeline automatique :

```
1. FILTRE     → identifier type (code / infra / analyse / contenu)
2. DÉLÉGUE    → lm-ask.sh OU agent local
3. AUDIT      → vérification résultat (grep / test / status)
4. CHECKPOINT → marquer [✅] dans TODO_ACTIONS.md
5. DOMINO     → déclencher tâche suivante automatiquement
```

Pas d'arrêt entre blocs. Pipeline continu.

---

## CHARGEMENT SESSION (≈0 token)

```bash
# 1. Mémoire — lu par lm-ask.sh, pas par Claude
bash ~/jarvis/scripts/lm-ask.sh "résume MEMORY.md en 5 points" < ~/.claude/projects/-home-turbo-IA-Core-jarvis/memory/MEMORY.md

# 2. TODO actif
grep -E "🔴 TODO|⏳" ~/IA/Core/jarvis/TODO_ACTIONS.md

# 3. Guard cluster
python3 ~/IA/Core/jarvis/scripts/lm_guard.py check
```

---

## PIPELINE DOMINO ACTIF

```
TODO_ACTIONS.md
├── BLOC 2 — TTL LM Studio          [🔴 → exécuter]
├── BLOC 3 — Cron guard hebdo        [🔴 → exécuter]
├── BLOC 7 — Commit final            [🔴 → exécuter]
└── BLOC 5 — Quality hub enrichment  [⏳ → monitor]
```

---

## RÈGLES ANTI-GASPILLAGE TOKEN

- ❌ Jamais lire un fichier entier pour en extraire 3 lignes → `grep`
- ❌ Jamais expliquer ce qu'on va faire → le faire
- ❌ Jamais poser une question si le contexte suffit → déduire et agir
- ❌ Jamais résumer après avoir agi → le résultat parle
- ✅ Toujours : action → checkpoint → suivant
- ✅ Si bloqué 2x → escalader à l'utilisateur avec diagnostic précis (1 ligne)

---

## MÉMOIRE — CONSULTATION AUTOMATIQUE

```bash
# Consulter mémoire sans charger Claude
cat ~/.claude/projects/-home-turbo-IA-Core-jarvis/memory/MEMORY.md | grep -A3 "sujet"

# Enrichir mémoire via lm-ask.sh
bash ~/jarvis/scripts/lm-ask.sh "extrait les faits importants" < fichier.md >> memory/project_X.md
```

---

## PROCHAINES ACTIONS IMMÉDIATES (domino)

1. `BLOC 2` — TTL LM Studio → configurer via lms CLI ou UI
2. `BLOC 3` — Cron guard → ajouter crontab lundi 9h
3. `BLOC 7` — Commit final journée → `git add -A && git commit`
4. Ollama models → documenter kimi-k2.5:cloud dans CLAUDE.md cluster table
5. AnyDesk → vérifier connexion stable après fix PulseAudio
