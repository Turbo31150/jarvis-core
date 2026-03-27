---
name: Codeur.com Active Missions
description: All active offers, proposals, client negotiations on Codeur.com as of 2026-03-27
type: project
---

## Offres confirmées postées (2026-03-27)

1. **Assistant IA Guillaume Chupin** (#480357) — Super offre déposée
   - Client: Guillaume Chupin, élu local, mandat municipal
   - Besoin: tri demandes citoyennes OCR + IA + Qomon CRM + veille + alertes sujets sensibles + Outlook mairie
   - Budget client: 500-1000€ | Notre proposition: 1 400€ TTC
   - **EN NÉGOCIATION** — le client a répondu avec cahier des charges complet
   - PDF envoyé: Proposition-Assistant-IA-Guillaume-Chupin.pdf
   - **Why:** Qomon API REST confirmée (developers.qomon.app), Outlook IMAP/SMTP compatible anciennes versions

2. **Symfony Claude Code** (#480338) — 500€/10j POSTÉE
   - Budget client: 10 000€+ | Moyenne devis: 4 700€
   - Cherche dev Symfony qui utilise Claude Code quotidiennement

3. **Fiches WooCommerce IA** (#480360) — Offre déposée
   - Budget: <500€ | 1000 articles seconde main photo→fiche

4. **Programmation IA** (#480333) — Offre déposée
   - Budget: 500-1000€

## Offres préparées non postées

5. **Interface Claude CoLean** (#480296) — 690€/5j — texte prêt
6. **Zapier + IA immobilier** (#480325) — 890€/6j — texte prêt
7. **Claude IA SEO** (#480292) — 450€/5j — texte prêt
8. **IA Énergies Renouvelables** (#480392) — 8 500€/30j — texte prêt
   - Budget: 10 000€+ | Moyenne devis: 42 900€ | ML + énergies renouvelables
9. **PaddleOCR déploiement** (#479498) — 690€/5j — texte prêt

## Fichiers

- Propositions: `~/IA/Core/jarvis/data/propositions/`
- PDF Guillaume: `Proposition-Assistant-IA-Guillaume-Chupin.pdf` (116 Ko)
- Visuels PNG: 3 banners + 1 carte visite
- Fichier complet: `OFFRES-A-POSTER.txt`

## Problème technique

BrowserOS perd ses sessions au redémarrage. Fix appliqué dans:
- `~/.browseros/profile_permanent/Default/Preferences` (exit_type=Normal, restore=ON)
- `~/jarvis-linux/core/scripts/monitoring/browseros_watchdog.py` (fix before start/stop)
- Chrome GUI n'a pas de CDP → impossible de poster automatiquement via script

**How to apply:** Pour poster les 5 offres restantes, il faut les coller manuellement dans Chrome GUI (seul navigateur avec session Codeur.com active).
