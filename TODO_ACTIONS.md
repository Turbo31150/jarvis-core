# JARVIS — TODO Actions Chronologiques
> Checkpoints prêts à exécuter. Chaque bloc = une action complète autonome.
> Mise à jour : 2026-04-14 | Statut : voir badges [✅ DONE] [🔴 TODO] [⏳ EN COURS] [⚠️ BLOQUÉ]

---

## MÉTHODE D'UTILISATION
1. Lire le bloc entier avant d'exécuter
2. Copier-coller le bloc bash directement dans le terminal
3. Vérifier le résultat attendu en bas de chaque bloc
4. Marquer [✅ DONE] après succès

---

## BLOC 0 — GUARD PRÉ-SESSION (à faire CHAQUE SESSION)
**But :** Vérifier état cluster avant tout travail. 30 secondes.

```bash
# Guard VRAM + modèles
python3 ~/IA/Core/jarvis/scripts/lm_guard.py check

# Cluster nodes up ?
for node in "192.168.1.85:1234" "192.168.1.26:1234" "192.168.1.113:1234" "127.0.0.1:11434"; do
  curl -s --max-time 3 "http://$node/v1/models" > /dev/null 2>&1 && echo "✅ $node" || echo "❌ $node"
done

# Modèles chargés
~/.lmstudio/bin/lms ps 2>/dev/null
```
**Résultat attendu :** Guard OK ou warnings compris. Tous nœuds UP.

---

## BLOC 1 — INTÉGRER lm_guard dans lm-ask.sh [✅ DONE]
**But :** Enforcement automatique avant chaque appel LLM. Sans ça, les règles restent du texte.

```bash
# 1. Backup
cp ~/jarvis/scripts/lm-ask.sh ~/jarvis/scripts/lm-ask.sh.bak

# 2. Ajouter le guard après la ligne "[[ -z "$PROMPT" ]]"
# Trouver la ligne exacte
grep -n 'PROMPT.*exit\|exit 1' ~/jarvis/scripts/lm-ask.sh | head -5
```

Puis éditer pour insérer après la validation du prompt :
```bash
# Guard VRAM (warn seulement, pas bloquant pour speed)
GUARD_OUT=$(python3 ~/IA/Core/jarvis/scripts/lm_guard.py check 2>/dev/null)
GUARD_EXIT=$?
if [[ $GUARD_EXIT -eq 2 ]]; then
  echo "🔴 [GUARD CRITICAL] $GUARD_OUT" >&2
  # Auto-enforce si critique
  python3 ~/IA/Core/jarvis/scripts/lm_guard.py enforce >/dev/null 2>&1
fi
```
**Résultat attendu :** `lm-ask.sh` auto-vérifie VRAM avant chaque appel.

---

## BLOC 2 — CONFIGURER TTL PAR DÉFAUT LM STUDIO [⚠️ MANUEL — UI LM Studio → Settings → Server → TTL 30min]
**But :** Modèles idle se déchargent automatiquement après 30min (évite les 22GB gaspillés).

```bash
# Vérifier config LM Studio actuelle
cat ~/.lmstudio/config.json 2>/dev/null | python3 -m json.tool | grep -i ttl

# Config recommandée via lms CLI
~/.lmstudio/bin/lms server config --help 2>/dev/null | grep -i ttl

# Alternative : configurer dans l'UI LM Studio
# Settings → Server → Default TTL → 30 minutes
# Applicable à tous les modèles sauf si override explicite
```
**Résultat attendu :** `lms ps` montre TTL sur tous les modèles IDLE.

---

## BLOC 3 — CRON GUARD HEBDOMADAIRE [✅ DONE]
**But :** Audit automatique lundi matin + mise à jour INVENTORY.md.

```bash
# Ajouter cron (lundi 08h00)
(crontab -l 2>/dev/null; echo "0 8 * * 1 python3 ~/IA/Core/jarvis/scripts/lm_guard.py enforce >> /var/log/jarvis-lm-guard.log 2>&1") | crontab -

# Vérifier
crontab -l | grep lm_guard

# Aussi : inventory update hebdo
(crontab -l 2>/dev/null; echo "5 8 * * 1 cd ~/IA/Core/jarvis && git log --oneline -5 >> /var/log/jarvis-weekly-audit.log 2>&1") | crontab -
```
**Résultat attendu :** `crontab -l` montre les 2 entrées lundi 08h.

---

## BLOC 4 — BRANCHER quality_hub SUR LE PROXY LLM [✅ DONE]
**But :** Chaque requête passant par `jarvis-proxy:18800` est automatiquement analysée (injection, modération, hallucination).

```bash
# Vérifier proxy actuel
curl -s http://localhost:18800/v1/models | python3 -m json.tool | head -10

# Inspecter le code proxy
find ~/IA/Core/jarvis -name "*proxy*" -name "*.py" | head -5
cat ~/IA/Core/jarvis/core/jarvis_lm_link_proxy.py | head -50
```

Puis modifier le proxy pour appeler QualityHub sur chaque request :
```python
# À insérer dans le handler de requête du proxy
import sys
sys.path.insert(0, '/home/turbo/IA/Core/jarvis/core')
from jarvis_quality_hub import build_jarvis_quality_hub
_hub = build_jarvis_quality_hub()

# Dans le handler :
guard_result = _hub.analyze_input(prompt_text)
if guard_result.get("action") == "BLOCK":
    return {"error": "blocked", "reason": guard_result}
```
**Résultat attendu :** Logs quality_hub dans `/tmp/jarvis_quality_hub_stats.json` à chaque requête.

---

## BLOC 5 — ENRICHIR LES MODULES QUALITY (post-utilisation) [⏳ EN COURS — auto]
**But :** Les modules s'enrichissent à chaque appel via online_learner. Vérifier l'évolution.

```bash
# Stats usage actuel
python3 -c "
import json
try:
    d = json.load(open('/tmp/jarvis_quality_hub_stats.json'))
    total = sum(v.get('call_count',0) for v in d.values())
    print(f'Total calls: {total}')
    for mod, v in sorted(d.items(), key=lambda x: -x[1].get('call_count',0)):
        print(f'  {mod}: {v.get(\"call_count\",0)} calls, avg_reward={v.get(\"avg_reward\",0):.2f}')
except: print('Pas encore de stats')
"

# Lancer un cycle de test pour enrichir les datasets
cd ~/IA/Core/jarvis/core && python3 jarvis_quality_hub.py
```
**Résultat attendu :** Call count monte, avg_reward se stabilise vers 0.8-1.0.

---

## BLOC 6 — RÉSULTATS BENCHMARK CLUSTER [✅ DONE]
**But :** Récupérer les résultats du benchmark latence M1/M2/M32 lancé plus tôt.

```bash
# Chercher le fichier de résultats
ls -lt /tmp/jarvis_benchmark_cluster_*.json 2>/dev/null | head -3

# Afficher les recommandations
python3 -c "
import json, glob
files = sorted(glob.glob('/tmp/jarvis_benchmark_cluster_*.json'))
if files:
    d = json.load(open(files[-1]))
    print(json.dumps(d.get('recommendations', d), indent=2))
else:
    print('Benchmark pas encore terminé')
"
```
**Résultat attendu :** JSON avec classement modèles par latence/qualité + pipelines recommandés.

---

## BLOC 7 — COMMIT FINAL JOURNÉE [✅ DONE]
**But :** Sauvegarder tout le travail du jour en un commit propre.

```bash
cd ~/IA/Core/jarvis

# Voir ce qui est modifié/non commité
git status --short

# Stage les fichiers pertinents (pas les exports GPU ni daily-intelligence)
git add core/jarvis_*.py core/INDEX_QUALITY_MODULES.md INVENTORY.md scripts/lm_guard.py 2>/dev/null
git add orchestrator/jarvis_orchestrator.db 2>/dev/null

# Commit
git commit -m "Session 2026-04-14: quality modules, enforcement, inventory

Modules créés (10 + hub):
- prompt_injection_detector, hallucination_detector, content_moderator
- evaluation_harness, model_committee, lora_manager
- online_learner, golden_dataset, regression_tester, data_quality
- quality_hub (orchestrateur 10/10)

Enforcement:
- lm_guard.py: VRAM/modèles/cascade enforcement runtime
- 35b IDLE déchargé (22GB libérés)

Index:
- INVENTORY.md: inventaire complet système
- INDEX_QUALITY_MODULES.md: carte mentale pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
**Résultat attendu :** `git log --oneline -3` montre le commit avec tous les fichiers.

---

## BLOC 8 — HOOK lm_guard DANS UserPromptSubmit [✅ DONE]
**But :** Guard auto à chaque message Claude Code (pas seulement lm-ask).

Dans `~/.claude/settings.json`, ajouter :
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "llm|model|génèr|crée|analyse",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/IA/Core/jarvis/scripts/lm_guard.py check 2>/dev/null || true"
        }]
      }
    ]
  }
}
```
**Résultat attendu :** Guard check silencieux avant les prompts LLM lourds.

---

## RÉCAPITULATIF STATUTS

| Bloc | Action | Statut | Priorité |
|------|--------|--------|----------|
| 0 | Guard pré-session | ✅ OPÉRATIONNEL | Chaque session |
| 1 | Hook lm-ask.sh | ✅ DONE | HIGH |
| 2 | TTL LM Studio par défaut | 🔴 TODO | HIGH |
| 3 | Cron guard hebdo | ✅ DONE | MEDIUM |
| 4 | quality_hub → proxy | ✅ DONE | HIGH |
| 5 | Enrichissement modules | ⏳ AUTO | LOW |
| 6 | Benchmark résultats | ✅ DONE | ~~MEDIUM~~ |
| 7 | Commit final journée | ✅ DONE | HIGH |
| 8 | Hook UserPromptSubmit | ✅ DONE | MEDIUM |

---

## RÈGLES D'OR (rappel enforcement)

```
AVANT tout travail LLM :
  python3 ~/IA/Core/jarvis/scripts/lm_guard.py check

AVANT de charger un gros modèle :
  python3 ~/IA/Core/jarvis/scripts/lm_guard.py enforce

CASCADE OBLIGATOIRE :
  fast   → qwen3.5-9b (M1/M2/M32)
  reason → deepseek-r1 (M1/M2/M32)
  big    → qwen3.5-35b-a3b (SEULEMENT si explicitement demandé)
  code   → qwen2.5-coder-14b (M1)
  local  → OL1 gemma3:4b (fallback offline)

JAMAIS :
  - Charger 35b sans besoin explicite
  - Laisser >2 modèles IDLE sans TTL
  - Appeler Claude (moi) pour du code routinier → déléguer M1/M2
```
