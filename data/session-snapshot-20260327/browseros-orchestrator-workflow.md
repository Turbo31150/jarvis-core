# JARVIS Orchestrateur Autonome — Workflow BrowserOS

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  JARVIS ORCHESTRATOR (systemd + BrowserOS skills)            │
│                                                               │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐     │
│  │  WATCHER    │  │  WATCHER     │  │  WATCHER        │     │
│  │  Codeur.com │  │  LinkedIn    │  │  Contenu        │     │
│  │  (5 min)    │  │  (15 min)    │  │  (8h/12h/18h)   │     │
│  │             │  │              │  │                  │     │
│  │  Scan       │  │  Notifs      │  │  Posts planifiés │     │
│  │  projets    │  │  Comments    │  │  Articles        │     │
│  │  Messages   │  │  Messages    │  │  GitHub updates  │     │
│  │  Offres     │  │  Feed        │  │  Images/PDF      │     │
│  └──────┬──────┘  └──────┬───────┘  └────────┬────────┘     │
│         │                │                    │               │
│         ▼                ▼                    ▼               │
│  ┌───────────────────────────────────────────────────┐       │
│  │              ACTION QUEUE (SQLite)                 │       │
│  │                                                    │       │
│  │  id | type | priority | status | payload | created │       │
│  │  ─────────────────────────────────────────────── │       │
│  │  1  | offer      | high   | pending  | {...}     │       │
│  │  2  | li_comment | low    | auto     | {...}     │       │
│  │  3  | pdf_gen    | medium | done     | {...}     │       │
│  │  4  | li_post    | high   | approval | {...}     │       │
│  └──────────────────────┬────────────────────────────┘       │
│                         │                                     │
│         ┌───────────────┼───────────────────┐                 │
│         ▼               ▼                   ▼                 │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐     │
│  │ AUTO EXEC  │  │ APPROVAL     │  │ GENERATOR        │     │
│  │            │  │ ROUTER       │  │                   │     │
│  │ • Like     │  │              │  │ • PDF proposition │     │
│  │ • Simple   │  │ → Telegram   │  │ • Image LinkedIn  │     │
│  │   comment  │  │ → Email      │  │ • Image GitHub    │     │
│  │ • Scan     │  │ → Dashboard  │  │ • Article draft   │     │
│  │ • Track    │  │ → Terminal   │  │ • README update   │     │
│  │ • Veille   │  │              │  │                   │     │
│  └────────────┘  │ Attend "go"  │  │ Chrome headless   │     │
│                  │ ou "skip"    │  │ → HTML → PNG/PDF  │     │
│                  └──────────────┘  └──────────────────────┘  │
│                                                               │
│  ┌───────────────────────────────────────────────────┐       │
│  │              LLM ENGINE                            │       │
│  │                                                    │       │
│  │  Rapide (auto)     : gemma-3-4b (M1, 0.4s)       │       │
│  │  Qualité (approval): Claude Haiku API              │       │
│  │  Raisonnement      : deepseek-r1 (M3)             │       │
│  │  Consensus         : M1 + M3 + vote               │       │
│  └───────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

## Flux détaillés

### Flux 1 — Codeur.com Watcher (toutes les 5 min)

```
SCAN
  │
  ├─ WebFetch pages 1-3 de /projects
  ├─ Filtre par keywords (config.yaml)
  ├─ Compare avec projects_seen (SQLite)
  │
  ├─ NOUVEAU MATCH TROUVÉ ?
  │   │
  │   ├─ OUI → Génère proposition (LLM gemma-3-4b)
  │   │        Génère PDF (Chrome headless)
  │   │        Génère image bannière
  │   │
  │   │        Budget < 500€ ?
  │   │        ├─ OUI → Notification Telegram "Nouveau projet [titre] [budget]"
  │   │        │        Boutons: [Envoyer 🚀] [Modifier ✏️] [Ignorer ❌]
  │   │        │
  │   │        └─ NON → Notification Telegram URGENT
  │   │                 "🔴 GROS PROJET: [titre] [budget]"
  │   │                 Attend validation avant envoi
  │   │
  │   └─ NON → Log "Aucun nouveau match"
  │
  ├─ CHECK MESSAGES (toutes les 10 min)
  │   ├─ Scrape messagerie Codeur
  │   ├─ Nouveau message client ?
  │   │   ├─ OUI → Résumé LLM + brouillon réponse
  │   │   │        Notification Telegram URGENT
  │   │   │        "💬 Message de [client]: [résumé]"
  │   │   │        [Répondre] [Modifier] [Voir]
  │   │   └─ NON → skip
  │   │
  └───┘
```

### Flux 2 — LinkedIn Watcher (toutes les 15 min)

```
CHECK NOTIFICATIONS
  │
  ├─ Via BrowserOS CDP → linkedin.com/notifications
  ├─ Parse notifications non lues
  │
  ├─ TYPE: Like/Reaction sur ton post
  │   └─ AUTO: Log + compteur (pas d'action)
  │
  ├─ TYPE: Commentaire sur ton post
  │   ├─ Commentaire simple (merci, bravo, emoji) ?
  │   │   └─ AUTO: Like le commentaire
  │   │
  │   └─ Commentaire technique/question ?
  │       └─ APPROVAL: Brouillon réponse (LLM)
  │          → Telegram "[user] a commenté: [extrait]"
  │          → Proposition de réponse
  │          → [Répondre] [Modifier] [Ignorer]
  │
  ├─ TYPE: Message privé
  │   └─ APPROVAL: Résumé + brouillon
  │      → Telegram URGENT
  │
  ├─ TYPE: Mention / Tag
  │   └─ APPROVAL: Brouillon réponse contextuelle
  │
  └─ TYPE: Profil consulté / Recherche
      └─ AUTO: Log dans stats quotidiennes
```

### Flux 3 — Contenu (8h / 12h / 18h + cron)

```
CONTENT CALENDAR CHECK
  │
  ├─ Post LinkedIn planifié pour maintenant ?
  │   ├─ OUI → Génère image/bannière (HTML→PNG)
  │   │        Prépare le post complet
  │   │        APPROVAL: Telegram avec preview
  │   │        [Publier 🚀] [Reporter ⏰] [Modifier ✏️]
  │   └─ NON → skip
  │
  ├─ Article Dev.to/Medium planifié ?
  │   ├─ OUI → Génère brouillon (LLM qualité)
  │   │        APPROVAL: lien preview
  │   └─ NON → skip
  │
  ├─ GitHub activity planifiée ?
  │   ├─ Mise à jour README ? → AUTO
  │   ├─ Nouveau gist ? → AUTO
  │   ├─ Release notes ? → APPROVAL
  │   └─ Commit activity ? → AUTO
  │
  └─ DAILY DIGEST (7h chaque matin)
      → Email résumé :
        - Projets Codeur matchés hier
        - Offres envoyées + statuts
        - LinkedIn stats (vues, impressions, commentaires)
        - Actions en attente de validation
        - Contenu prévu aujourd'hui
```

## Base de données SQLite

```sql
-- Projets scannés
CREATE TABLE projects_seen (
    id INTEGER PRIMARY KEY,
    codeur_id TEXT UNIQUE,
    title TEXT,
    budget TEXT,
    url TEXT,
    matched_keywords TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    offer_sent BOOLEAN DEFAULT 0,
    offer_amount REAL,
    offer_status TEXT DEFAULT 'none'
);

-- File d'attente d'actions
CREATE TABLE action_queue (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'pending',
    payload JSON,
    created DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed DATETIME,
    result TEXT,
    channel TEXT
);

-- Calendrier de contenu
CREATE TABLE content_calendar (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    content_type TEXT,
    title TEXT,
    body TEXT,
    image_path TEXT,
    pdf_path TEXT,
    scheduled_at DATETIME,
    published_at DATETIME,
    status TEXT DEFAULT 'draft'
);

-- Stats LinkedIn
CREATE TABLE linkedin_stats (
    id INTEGER PRIMARY KEY,
    date DATE,
    profile_views INTEGER,
    post_impressions INTEGER,
    search_appearances INTEGER,
    new_connections INTEGER,
    comments_received INTEGER,
    comments_replied INTEGER
);

-- Templates de propositions
CREATE TABLE offer_templates (
    id INTEGER PRIMARY KEY,
    category TEXT,
    keywords TEXT,
    template_text TEXT,
    min_budget REAL,
    max_budget REAL,
    default_delay INTEGER,
    success_rate REAL DEFAULT 0
);

-- Messages clients
CREATE TABLE client_messages (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    client_name TEXT,
    message_text TEXT,
    received_at DATETIME,
    response_draft TEXT,
    response_sent BOOLEAN DEFAULT 0,
    response_sent_at DATETIME
);
```

## Notifications Telegram

```
Format messages:

🟢 AUTO EXÉCUTÉ
  "✅ Like posté sur commentaire de [user]"

🟡 EN ATTENTE
  "📋 Nouveau projet Codeur: [titre]
   Budget: [montant] | [X] offres
   Match: [keywords]

   Proposition générée (690€ / 5j)
   PDF: [lien]

   [Envoyer 🚀] [Modifier ✏️] [Ignorer ❌]"

🔴 URGENT
  "💬 MESSAGE CLIENT — [nom]
   Projet: [titre]

   [résumé du message]

   Réponse proposée:
   [brouillon]

   [Répondre ✅] [Modifier ✏️] [Appeler 📞]"

📊 DIGEST QUOTIDIEN (7h)
  "📊 JARVIS Daily — 27 mars 2026

   Codeur: 3 nouveaux matchs, 1 offre envoyée
   LinkedIn: 45 impressions, 2 commentaires
   Contenu: 1 post prévu à 12h
   Actions en attente: 2

   [Voir dashboard]"
```
