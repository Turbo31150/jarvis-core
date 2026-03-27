# Proposition — Assistant IA pour élu local
## Guillaume Chupin — Gestion de demandes, veille, communication

---

Bonjour Guillaume,

J'ai bien étudié l'ensemble de votre cahier des charges. Votre besoin est clair et structuré — je vous propose ci-dessous une réponse point par point, avec les outils concrets, le planning et le budget.

---

## 1. Traitement des demandes entrantes

**Ce que vous recevez :** courriers scannés, emails Outlook, messages divers.

**Ce que le système fait automatiquement :**

| Étape | Traitement | Outil |
|-------|-----------|-------|
| Réception | Email Outlook → détection automatique | n8n (connecteur IMAP Outlook) |
| Extraction | Courrier scanné → texte lisible | OCR (PaddleOCR ou Tesseract) |
| Résumé | Texte → résumé en 3 lignes | IA (Claude Haiku — rapide et économique) |
| Catégorisation | Voirie, propreté, sécurité, social, urbanisme... | IA (classification automatique) |
| Urgence | Niveau 1 (immédiat) à 4 (information) | IA (analyse du contenu + mots-clés) |
| Ton et sensibilité | Détection de colère, menace, sujet politique sensible | IA (analyse de sentiment) |
| Réponse proposée | Réponse institutionnelle adaptée, prête à valider | IA (génération avec ton formel mairie) |

**Vous recevez :** une notification (email ou Telegram) avec le résumé, la catégorie, l'urgence et une proposition de réponse. Vous validez en un clic ou vous modifiez.

---

## 2. Intégration Qomon

J'ai vérifié : Qomon dispose d'une API publique (contacts + actions) et d'une intégration Zapier/webhooks native. L'intégration est donc directe.

| Action | Fonctionnement |
|--------|---------------|
| Création de contact | Chaque nouveau demandeur est créé automatiquement dans Qomon |
| Mise à jour | Les interactions sont enregistrées sur la fiche contact |
| Segmentation | Tags automatiques selon la catégorie de demande |
| Suivi | Historique complet des échanges par contact |

**Compatibilité Outlook :** n8n se connecte à Outlook via IMAP/SMTP (même versions anciennes). Pas besoin de Microsoft 365 moderne.

---

## 3. Veille informationnelle

| Source | Intégration |
|--------|------------|
| Flux RSS (presse locale, institutionnel) | Agrégation automatique via n8n |
| Alertes Google | Réception par email → traitement automatique |
| Articles transmis par email | Détection et extraction automatique |
| Articles transmis manuellement | Interface simple de dépôt (formulaire web ou email dédié) |

**Traitement de chaque article :**
- Résumé en 5 lignes
- Identification du sujet et du lien avec votre territoire
- Enjeux et angles de communication possibles
- Points de vigilance

**Synthèse quotidienne :** chaque matin à l'heure de votre choix, vous recevez par email un récapitulatif structuré avec les points clés, les alertes et les opportunités de communication.

---

## 4. Détection des sujets sensibles

Le système analyse en continu :
- Les demandes citoyennes (ton négatif, mots-clés sensibles, récurrence d'un sujet)
- La veille (articles négatifs, polémiques naissantes)
- Les tendances (augmentation soudaine de demandes sur un même thème)

**Alerte immédiate** par email ou notification avec :
- Résumé du sujet sensible
- Contexte et historique
- Proposition de réponse ou d'éléments de langage

---

## 5. Génération de contenu

- Réponses aux sollicitations (ton institutionnel, adaptable)
- Publications pour réseaux sociaux (à partir d'un sujet ou d'une actualité)
- Notes de synthèse pour réunions
- Éléments de langage pour prises de parole

Chaque contenu généré est une proposition que vous validez ou modifiez avant diffusion.

---

## 6. Évolutions prévues (phase 2)

| Fonctionnalité | Description |
|---------------|------------|
| Tableau de bord | Volume de demandes, délais, thématiques récurrentes |
| Rapports périodiques | Génération automatique hebdomadaire ou mensuelle |
| Cartographie | Visualisation géographique des demandes sur le territoire |
| Base de connaissance | Capitalisation sur les réponses déjà apportées |
| Préparation de réunions | Notes de synthèse automatiques par thématique |

---

## Outils recommandés

| Composant | Outil | Pourquoi |
|-----------|-------|---------|
| Orchestration | **n8n** (auto-hébergé) | Gratuit, RGPD, flexible, visuel |
| Intelligence artificielle | **Claude** (Anthropic) | Meilleur pour le ton institutionnel et la nuance |
| OCR | **PaddleOCR** ou **Tesseract** | Gratuit, sur vos serveurs, pas de données envoyées au cloud |
| CRM | **Qomon** (votre outil actuel) | Intégration via API native |
| Email | **Outlook** (votre messagerie actuelle) | Connexion IMAP/SMTP |
| Hébergement | **VPS dédié** ou **serveur mairie** | Données souveraines, RGPD |

**Coût mensuel de fonctionnement :** 15 à 40 euros par mois (API Claude selon le volume de demandes). Tous les autres outils sont gratuits.

---

## Références

Mon système JARVIS gère au quotidien :
- 600+ opérations IA autonomes
- 65 workflows n8n en production
- OCR, classification, génération de réponses, veille automatisée
- Intégration multi-API (email, CRM, bases de données, Telegram)
- Traitement de plusieurs centaines de documents par jour

C'est exactement le même type d'architecture que celle que je vous propose, adaptée à votre contexte d'élu.

Portfolio : github.com/Turbo31150 (35 dépôts publics, 320 000 lignes en production)

---

## Planning

| Phase | Contenu | Durée |
|-------|---------|-------|
| **Semaine 1** | Installation n8n + connexion Outlook + OCR + premiers workflows (email → résumé → notification) | 5 jours |
| **Semaine 2** | Intégration Qomon + catégorisation IA + génération de réponses + veille RSS | 5 jours |
| **Semaine 3** | Synthèse quotidienne + détection sujets sensibles + alertes + ajustements | 3-4 jours |
| **Semaine 4** | Tests sur vos vrais documents + formation + documentation | 2-3 jours |

**Livraison :** système opérationnel en 3 à 4 semaines.

---

## Budget

| Poste | Montant |
|-------|---------|
| Développement complet (phases 1 à 4) | 1 800 € |
| Formation (2 sessions visio d'1 heure) | Inclus |
| Documentation utilisateur complète | Inclus |
| Support et ajustements (1 mois après livraison) | Inclus |
| **Total** | **1 800 € TTC** |

**Option phase 2 (dashboards, cartographie, rapports) :** devis séparé, estimé entre 800 et 1 200 euros selon le périmètre.

**Maintenance mensuelle (optionnelle) :** 120 euros par mois (surveillance, mises à jour, ajustements continus).

---

## Prochaine étape

Je vous propose un appel de 30 minutes pour :
1. Voir un exemple concret de traitement (je peux vous montrer un document scanné analysé en direct)
2. Préciser les catégories de demandes spécifiques à votre territoire
3. Valider la compatibilité avec votre version d'Outlook
4. Définir ensemble le planning de démarrage

Je suis disponible cette semaine aux horaires qui vous conviennent.

Cordialement,

Franck Delmas
Développeur IA et automatisation
Toulouse — Disponible en remote
github.com/Turbo31150
