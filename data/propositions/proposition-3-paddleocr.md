# PaddleOCR en production — API complète, extraction de tableaux, zéro coût par page

Bonjour,

Je déploie des services d'intelligence artificielle conteneurisés en production au quotidien, sur un cluster de 6 GPU NVIDIA sous Linux. PaddleOCR fait partie des outils que je maîtrise et que j'utilise dans mon infrastructure.

---

## Ce que je vous livre

### 1. Conteneur Docker prêt à déployer

```bash
docker compose up -d
# → Votre API OCR est disponible sur http://votre-serveur:8000
```

Deux versions fournies :
- **Version CPU** (serveur standard) : environ 500 ms par page, optimisation MKLDNN activée
- **Version GPU** (si disponible) : moins de 100 ms par page, optimisation TensorRT

### 2. API REST complète avec documentation interactive

| Point d'accès | Fonction |
|----------------|---------|
| `POST /ocr` | Image ou PDF → texte brut |
| `POST /ocr/structured` | Image ou PDF → JSON structuré (blocs, positions, niveau de confiance) |
| `POST /ocr/table` | Extraction de tableaux → JSON ou CSV |
| `POST /ocr/layout` | Analyse de mise en page (titres, paragraphes, images, tableaux) |
| `GET /health` | Vérification de l'état du service |
| `GET /docs` | Documentation Swagger interactive et testable |

### 3. PP-OCRv4 + PP-StructureV3 — au-delà du simple OCR

La plupart des prestataires installeront PaddleOCR dans sa version de base (reconnaissance de texte uniquement). Mon déploiement inclut le module **PP-StructureV3**, qui gère :

| Fonctionnalité | PP-StructureV3 | Azure Document Intelligence |
|----------------|---------------|---------------------------|
| Reconnaissance de texte | 95 %+ sur documents structurés | ~96 % |
| Extraction de tableaux | Cellules, en-têtes, fusion | Oui |
| Analyse de mise en page | Titres, paragraphes, images | Oui |
| Formules mathématiques | Oui | Oui |
| Export Markdown automatique | Oui | Non |
| Coût par page | **0 €** | 0,005 à 0,05 € |
| Hébergement des données | **100 % sur vos serveurs** | Cloud Microsoft |

### 4. Benchmark sur vos documents

Avant la livraison finale, je teste PaddleOCR sur un échantillon de vos vrais documents et vous fournis un rapport comparatif détaillé : taux de reconnaissance, latence, cas d'erreur. Vous décidez en connaissance de cause.

**Note de transparence :** sur des documents très complexes (manuscrits, mise en page dense sur plusieurs colonnes), Azure reste légèrement en tête (96 % contre 91 %). Sur des documents structurés classiques (factures, formulaires, courriers), la différence est généralement négligeable.

---

## Planning

| Jour | Livrable |
|------|---------|
| J1 | Conteneur Docker fonctionnel avec PaddleOCR et FastAPI |
| J2 | PP-StructureV3 : extraction de tableaux et analyse de mise en page |
| J3 | Optimisation des performances (MKLDNN pour CPU ou TensorRT pour GPU) |
| J4 | Benchmark comparatif sur vos documents |
| J5 | Déploiement sur votre serveur, documentation et formation |

---

## Budget : 690 € forfait

Ce tarif comprend :
- Conteneur Docker complet (versions CPU et GPU)
- API REST avec 6 points d'accès et documentation Swagger
- Module PP-StructureV3 (tableaux, mise en page, formules)
- Rapport de benchmark sur vos documents
- Guide de déploiement et de maintenance
- 2 semaines de support après livraison

---

## Pourquoi me faire confiance

- **Cluster de 6 GPU NVIDIA** en production permanente — le déploiement de services conteneurisés est mon quotidien
- **87 outils MCP** déployés via Docker depuis 18 mois
- **7 conteneurs Docker** actifs en permanence sur mon infrastructure
- Article technique publié : « MCP en production : retour d'expérience après 87 outils connectés »

**Portfolio :** github.com/Turbo31150

Je peux organiser une **démonstration en visioconférence** : vous m'envoyez un document, je le traite en direct devant vous avec PaddleOCR.

Franck Delmas
Architecte IA et systèmes Linux — Toulouse
Cluster multi-GPU — 6 NVIDIA, 46 Go de VRAM
