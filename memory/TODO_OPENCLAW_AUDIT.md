# JARVIS — TODO OpenClaw Audit & Hardening
> Généré : 2026-04-15 | Protocole : AUDIT → ACTION → CORRECTION → DOMINO
> Statut : [✅ DONE] [🔴 TODO] [⏳ EN COURS] [⚠️ BLOQUÉ]

---

## SCORES AUDIT INITIAL

| Point faible | Sévérité | Score actuel | Cible |
|---|---|---|---|
| P1 — Gateway DOWN | 🔴 CRITIQUE | 0/10 | 9/10 |
| P2 — Memory disabled | 🔴 HAUTE | 0/10 | 8/10 |
| P3 — 51 agents = 1 modèle | 🟠 HAUTE | 2/10 | 8/10 |
| P4 — Bootstrap files >8k (tronqués) | 🟠 HAUTE | 3/10 | 9/10 |
| P5 — Cron jobs payload invalide | 🟠 MOYENNE | 0/10 | 8/10 |
| P6 — 40 skills missing | 🟡 FAIBLE | 4/10 | 6/10 |
| P7 — Routing rules = 0 | 🟡 FAIBLE | 0/10 | 7/10 |

---

## BLOC P1 — GATEWAY : démarrer et stabiliser [🔴 TODO]
**But :** openclaw agent sans --local = timeout. Gateway jamais démarré (systemd user bus absent).
**Dépend de :** rien (P1 critique, premier)
**Domino :** → P5 (cron nécessite gateway)

```bash
# AUDIT
openclaw gateway status
openclaw health
ps aux | grep "openclaw.*gateway" | grep -v grep

# ACTION — lancer gateway en background dans screen
screen -dmS openclaw-gw bash -c 'openclaw gateway start 2>&1 | tee /tmp/openclaw-gw.log'
sleep 3

# VÉRIF
openclaw health
curl -s --max-time 5 http://127.0.0.1:18789/health | python3 -m json.tool 2>/dev/null || echo "KO"

# Si OK — rendre persistant via supervisor
cat > ~/.openclaw/start-gateway.sh << 'EOF'
#!/bin/bash
exec openclaw gateway start 2>&1 | tee -a /tmp/openclaw-gw.log
EOF
chmod +x ~/.openclaw/start-gateway.sh

# Ajouter au crontab reboot
(crontab -l 2>/dev/null | grep -v openclaw-gw; echo "@reboot screen -dmS openclaw-gw ~/.openclaw/start-gateway.sh") | crontab -
```
**Résultat attendu :** `openclaw health` répond OK. `openclaw agent --agent main --message "ping"` répond sans timeout.
**Score cible :** 9/10

---

## BLOC P2 — MEMORY : activer la mémoire persistante [🔴 TODO]
**But :** Agents sans mémoire = contexte perdu à chaque session. plugin memory-core désactivé.
**Dépend de :** P1 (gateway)
**Domino :** → P7 (routing enrichi par mémoire)

```bash
# AUDIT
openclaw memory --help
openclaw plugins list 2>/dev/null | grep memory
cat ~/.openclaw/openclaw.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('memory', 'absent'))"

# ACTION — activer memory-core
openclaw config set memory.enabled true
openclaw config set memory.searchEnabled true

# Définir workspace mémoire
MEMORY_DIR="$HOME/IA/Core/jarvis/memory"
openclaw config set memory.path "$MEMORY_DIR"

# Réindexer
openclaw memory reindex 2>/dev/null || echo "reindex non disponible — vérifier version"

# VÉRIF
openclaw memory search "cluster" 2>/dev/null
openclaw doctor 2>/dev/null | grep -i memory
```
**Résultat attendu :** `openclaw memory search "cluster"` retourne des résultats. Doctor ne signale plus "memory disabled".
**Score cible :** 8/10

---

## BLOC P3 — MODELS : routing intelligent par agent [🔴 TODO]
**But :** 51 agents tous sur qwen3.5-9b. Pas de spécialisation modèle par type de tâche.
**Dépend de :** rien (indépendant)
**Domino :** → P7 (routing rules s'appuient sur bons modèles)

```bash
# AUDIT
openclaw agents list 2>/dev/null | grep -E "^-|Model:"

# MATRICE CIBLE
# reasoning/debug  → deepseek-r1-0528 (M2)
# code/implem      → qwen3.5-27b-claude-opus-distilled (M1)
# docs/contenu     → qwen3.5-35b-a3b (M2) alias big
# ops/cluster      → gemma3:4b (OL1) — rapide, léger
# trading          → qwen3.5-9b (M1) — défaut

# ACTION — reconfigurer agents critiques
for agent in jarvis-auditor jarvis-code-reviewer feature-dev-code-reviewer pr-review-toolkit-code-reviewer; do
  openclaw agents configure "$agent" --model lmstudio-m2/deepseek/deepseek-r1-0528-qwen3-8b 2>/dev/null \
    && echo "✅ $agent → deepseek-r1" || echo "❌ $agent"
done

for agent in omega-dev-agent jarvis-auto-improver feature-dev-code-architect; do
  openclaw agents configure "$agent" --model lmstudio-m1/qwen/qwen3.5-27b-claude-opus-distilled 2>/dev/null \
    && echo "✅ $agent → opus-distilled" || echo "❌ $agent"
done

for agent in omega-docs-agent omega-analysis-agent; do
  openclaw agents configure "$agent" --model lmstudio-m2/qwen/qwen3.5-35b-a3b 2>/dev/null \
    && echo "✅ $agent → 35b" || echo "❌ $agent"
done

for agent in jarvis-cluster-health jarvis-zombie-cleaner jarvis-monitor-live system-health-monitor; do
  openclaw agents configure "$agent" --model ollama/gemma3:4b 2>/dev/null \
    && echo "✅ $agent → gemma3-ol1" || echo "❌ $agent"
done

# VÉRIF
openclaw agents list 2>/dev/null | grep -E "^-|Model:" | grep -v "qwen3.5-9b" | head -20
```
**Résultat attendu :** Agents reasoning sur deepseek-r1, dev sur opus-distilled, docs sur 35b, ops sur gemma3.
**Score cible :** 8/10

---

## BLOC P4 — BOOTSTRAP : pruner les fichiers tronqués [🔴 TODO]
**But :** AGENTS.md 16k, IDENTITY.md 10k, TOOLS.md 8k → tronqués à 8000 chars → agents perdent leurs instructions.
**Dépend de :** rien
**Domino :** → améliore P3 (agents lisent instructions complètes)

```bash
# AUDIT — taille actuelle
wc -c ~/.openclaw/workspace/AGENTS.md ~/.openclaw/workspace/IDENTITY.md ~/.openclaw/workspace/TOOLS.md

# BACKUP
cp ~/.openclaw/workspace/AGENTS.md ~/.openclaw/workspace/AGENTS.md.bak
cp ~/.openclaw/workspace/IDENTITY.md ~/.openclaw/workspace/IDENTITY.md.bak
cp ~/.openclaw/workspace/TOOLS.md ~/.openclaw/workspace/TOOLS.md.bak

# ACTION — résumer via lm-ask.sh (0 token Claude)
bash ~/jarvis/scripts/lm-ask.sh --big "Résume ce fichier en gardant UNIQUEMENT les sections essentielles pour un agent IA. Max 4000 caractères. Supprime tout doublon, exemple redondant, texte décoratif." \
  < ~/.openclaw/workspace/AGENTS.md > /tmp/agents_pruned.md

bash ~/jarvis/scripts/lm-ask.sh --big "Résume ce fichier identité en gardant UNIQUEMENT: rôle, règles critiques, liste outils. Max 3500 caractères." \
  < ~/.openclaw/workspace/IDENTITY.md > /tmp/identity_pruned.md

bash ~/jarvis/scripts/lm-ask.sh "Résume ce fichier outils en gardant UNIQUEMENT: nom outil, quand l'utiliser (1 ligne). Max 3000 caractères." \
  < ~/.openclaw/workspace/TOOLS.md > /tmp/tools_pruned.md

# VÉRIF taille avant apply
wc -c /tmp/agents_pruned.md /tmp/identity_pruned.md /tmp/tools_pruned.md

# APPLY si < 6000 chars chacun
[ $(wc -c < /tmp/agents_pruned.md) -lt 6000 ] && cp /tmp/agents_pruned.md ~/.openclaw/workspace/AGENTS.md && echo "✅ AGENTS.md pruné"
[ $(wc -c < /tmp/identity_pruned.md) -lt 5000 ] && cp /tmp/identity_pruned.md ~/.openclaw/workspace/IDENTITY.md && echo "✅ IDENTITY.md pruné"
[ $(wc -c < /tmp/tools_pruned.md) -lt 5000 ] && cp /tmp/tools_pruned.md ~/.openclaw/workspace/TOOLS.md && echo "✅ TOOLS.md pruné"
```
**Résultat attendu :** Chaque fichier < 5000 chars. Agents ne tronquent plus leur contexte.
**Score cible :** 9/10

---

## BLOC P5 — CRON : normaliser les 10 jobs payload [🔴 TODO]
**But :** 10 cron jobs avec `cron: "?"` — scheduler ne peut pas les déclencher.
**Dépend de :** P1 (gateway doit tourner)
**Domino :** → jobs s'exécutent, alimente adaptive_trigger

```bash
# AUDIT
cat ~/.openclaw/cron/jobs.json | python3 -m json.tool | head -60

# ACTION — fix via doctor
openclaw doctor --fix

# VÉRIF
openclaw cron list 2>/dev/null | head -20
cat ~/.openclaw/cron/jobs.json | python3 -c "
import json,sys
jobs = json.load(sys.stdin)
jlist = jobs if isinstance(jobs, list) else jobs.get('jobs', [])
for j in jlist:
  cron = j.get('cron') or j.get('schedule') or j.get('payload',{}).get('cron','?')
  print(f'{j.get(\"name\",\"?\"):35} | {cron}')
"
```
**Résultat attendu :** `openclaw cron list` affiche 10 jobs avec expressions cron valides (ex: `0 8 * * *`).
**Score cible :** 8/10

---

## BLOC P7 — ROUTING RULES : configurer keyword → agent [🔴 TODO]
**But :** Tous agents à 0 routing rules. OpenClaw ne route pas intelligemment.
**Dépend de :** P1 + P3
**Domino :** → adaptive_trigger peut s'appuyer sur routing natif OpenClaw

```bash
# AUDIT
openclaw agents list 2>/dev/null | grep "Routing:"

# ACTION — configurer routing sur main + openclaw-master
# Format: openclaw agents routing add <agent> --keyword <kw> --target <agent>
openclaw agents routing add main --keyword "codeur" --target jarvis-task-balancer 2>/dev/null || \
  echo "routing CLI non disponible — vérifier: openclaw agents --help"

# Fallback: éditer config routing directement
ROUTING_FILE=~/.openclaw/agents/main/agent/routing.json
cat > "$ROUTING_FILE" << 'EOF'
{
  "rules": [
    {"keyword": "codeur",    "target": "jarvis-task-balancer",   "priority": 10},
    {"keyword": "mission",   "target": "jarvis-task-balancer",   "priority": 10},
    {"keyword": "hackathon", "target": "omega-analysis-agent",   "priority": 9},
    {"keyword": "cluster",   "target": "jarvis-cluster-health",  "priority": 9},
    {"keyword": "gpu",       "target": "jarvis-gpu-manager",     "priority": 9},
    {"keyword": "code",      "target": "omega-dev-agent",        "priority": 8},
    {"keyword": "security",  "target": "omega-security-agent",   "priority": 8},
    {"keyword": "doc",       "target": "omega-docs-agent",       "priority": 7},
    {"keyword": "trade",     "target": "omega-trading-agent",    "priority": 7}
  ]
}
EOF
echo "✅ routing.json créé"

# Copier sur openclaw-master
cp "$ROUTING_FILE" ~/.openclaw/agents/openclaw-master/agent/routing.json

# VÉRIF
cat ~/.openclaw/agents/main/agent/routing.json | python3 -m json.tool
```
**Résultat attendu :** routing.json présent sur main + openclaw-master. Routing rules > 0.
**Score cible :** 7/10

---

## ORDRE D'EXÉCUTION (avec dépendances)

```
P1 (gateway)    ──────────────────────────────────► P5 (cron)
                                                         │
P4 (bootstrap)  ──► P3 (models) ──► P7 (routing) ◄──────┘
                                         │
P2 (memory)     ──────────────────────────────────► P7 (routing enrichi)
```

**Séquence recommandée :**
1. P4 (bootstrap prune) — indépendant, rapide, améliore tout le reste
2. P1 (gateway) — bloquant pour P5
3. P3 (models routing) — indépendant
4. P2 (memory) — après P1
5. P5 (cron fix) — après P1
6. P7 (routing rules) — après P1+P3

---

## CHECKPOINT FINAL

```bash
# Score global post-fix
openclaw health
openclaw agents list 2>/dev/null | grep -c "Model:" && echo "agents configurés"
openclaw memory search "test" 2>/dev/null | head -3
openclaw cron list 2>/dev/null | wc -l
cat ~/.openclaw/agents/main/agent/routing.json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d[\"rules\"])} routing rules')"
wc -c ~/.openclaw/workspace/AGENTS.md ~/.openclaw/workspace/IDENTITY.md
```
**Résultat attendu :** gateway OK, memory actif, cron valides, routing rules > 8, bootstrap < 5k chars.
