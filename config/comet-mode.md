# COMET ORCHESTRATEUR IA — Navigateur Distribué v10.0

## MODE ACTIF
Quand ce fichier est chargé, Claude Code opère en mode COMET :
- Chaque requête est classifiée (simple/complexe/critique)
- Les tâches sont distribuées sur M1/M2/M3 en parallèle
- Les onglets BrowserOS servent de canaux (analyse/validation/actions)
- Les résultats sont agrégés en consensus

## CLUSTER
| Machine | IP | Rôle | Modèle par défaut |
|---------|-----|------|-------------------|
| M1 | 127.0.0.1:1234 | MASTER analyse profonde | qwen3.5-9b (fast) / deepseek-r1 (deep) |
| M2 | 192.168.1.26:1234 | DETECTOR rapide | qwen3-8b |
| M3 | 192.168.1.113:1234 | ORCHESTRATEUR validation | deepseek-r1 / mistral-7b |

## COMMANDES
- `distribute("simple", prompt)` → M2 seul
- `distribute("complex", prompt)` → M2+M3 parallèle
- `distribute("critical", prompt)` → consensus 3 nœuds
- `query_fast(prompt)` → réponse <5s
- `consensus(prompt)` → vote 2/3

## BROWSER TABS
- Onglet 1: Analyse (données brutes)
- Onglet 2: Validation (IA cross-check)
- Onglet 3: Actions (résultats + exécution)
