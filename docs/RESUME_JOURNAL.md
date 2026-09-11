# AgentGrid — Journal de reprise (session autonome)

## Contexte de session
- SHA de départ (local == origin/main) : `26bc86c2845deb74fdee0363ba6f3229bcc5be89`
- Début de session : 2026-09-11T18:53Z
- Seuil minimal (6h) : 2026-09-12T00:53Z
- Échéance maximale (8h) : 2026-09-12T02:53Z
- Remote : https://github.com/samy-fadel/agentgrid.git — branche `main`
- Contrainte : pas de reset destructif, pas de force-push, pas de réécriture d'historique.

## Checklist de fin (critères d'acceptation)

### §2 Dashboard
- [x] JSX compile (Babel) — bloc `activeTab === 'mission'` refermé
- [x] Compilation frontend ajoutée au contrôle CI/tests
- [x] Onglets Plans / Capacité / Diagnostic / Historique rendus
- [x] États chargement / vide / erreur distincts ; jamais erreur affichée comme résultat
- [x] Approbation + exécution branchées avec réponse serveur réelle
- [x] Paramètres mission structurés (budget, deadline, mode) transmis indépendamment
- [x] Origine API locale préservée

### §3 Contrôle backend
- [x] Source de vérité serveur pour mode + politique (défaut `validation`)
- [x] Tous les chemins de mutation (anciens outils MCP inclus) passent par les mêmes règles
- [x] `advisory` : aucune mutation possible
- [x] `validation` : plan enregistré (id unique + empreinte contenu, lié au workload) avant approbation
- [x] Approbation persistée serveur ; booléen du modèle non suffisant
- [x] Modification matérielle du plan approuvé invalide l'approbation
- [x] `delegation` : plafonds cumulés + restrictions appliqués à chaque action ET repli
- [x] Paramètres incompatibles rejetés explicitement

### §4 Faux succès / doublons
- [x] Contrats RuntimeAdapter/simulateur/Slurm/MCP harmonisés (`machine_type`)
- [x] Erreur de programmation/validation/politique != timeout ambigu
- [x] Recherche post-timeout sur identité de soumission stable
- [x] Vérification stricte du job attendu + état observé
- [x] Idempotence à travers requêtes HTTP + redémarrage (stockage persistant)
- [x] Rejeu distinct d'une nouvelle tentative autorisée

### §5 Plans + quotas
- [x] Moteur de plans relié aux candidats compatibles + contraintes profil + capacités runtime
- [x] CPU/GPU/mémoire/machines/régions/zones/parallélisme/interruptibilité/provisionnement vérifiés
- [x] Pas de plan Slurm non exécutable présenté comme exécutable
- [x] Budget zéro / invalide / aucun candidat / contradictions => explication structurée sans exception
- [x] Coûts simulateur identifiés comme hypothèses
- [x] `check_quota_availability` : QUOTA_UNKNOWN sans accès, jamais "verified" par défaut
- [x] Valeurs synthétiques réservées au mode démo explicite
- [x] Catalogue / quota / signal / allocation restent séparés

### §6 Exécution ↔ cycle de vie ↔ historique
- [x] Parcours réel : profil → plans → approbation → tentative → observations → fin → historique
- [x] `workload_id` / `plan_id` / `attempt_id` / `job_id` distincts et associés
- [x] Coûts sans double comptage ; estimation / usage / facturation séparés
- [x] Chiffre LLM non accepté comme mesure
- [x] Réhydratation après redémarrage
- [x] Checkpoint réellement vérifié, pas juste un chemin construit
- [x] Repli soumis à budget/compatibilité/autorisation
- [x] Downscale refusé retourne refus
- [x] Diagnostics conservent source ; QoS Slurm != quota GCP

### §7 Slurm / télémétrie
- [x] 1. Resize mauvais workload_id rejeté avant mutation
- [x] 2. 999999 CPU sur 128 rejeté explicitement
- [x] 3. Normalisation STANDARD / "100% Standard" ; BANANA rejeté
- [x] 4. 1h sans allocation observée => pas de coût 0,20€ inventé
- [x] 5. Nœuds occupés => pas de 124/128 CPU libres
- [x] 6. Perte d'observation => observed_cpu inconnu
- [x] 7. MCP indisponible => erreur explicite, pas de simulateur silencieux

### Éléments non cochés / non prouvés
- **Validation visuelle en navigateur** : non réalisée (voir blocage B1).
- **Intégration Slurm réelle** : non validée (voir blocage B3).
- **Provisionnement GCP réel** : non réalisé (voir blocage B4). Seule la lecture de quota
  a été exercée contre l'API GCP réelle.
- **Checkpoint `gs://`** : non vérifiable ici (voir blocage B2) ; la reprise est déclarée
  « non prouvée » plutôt que supposée.

## Défauts reproduits (preuves)

Tous reproduits sur le SHA de départ `26bc86c` avant toute correction.

| Réf | Défaut | Preuve observée |
|---|---|---|
| §2 | JSX ne compile pas | `TS1005: ')' expected` ligne 1081 du bloc babel (~1155 du fichier) |
| §4-A | `machine_type` non supporté par le simulateur | `TypeError: SimulatedRuntime.submit_job() got an unexpected keyword argument 'machine_type'` |
| §4-B | Faux succès | TypeError avalée -> traitée comme timeout -> `_discover_active_job()` renvoie `mc-001` -> `status=submitted, verified=True, recovered_after_timeout=True`, aucun job créé |
| §4-C | Vérification laxiste | `active_job is not None` suffit -> n'importe quel job vérifie n'importe quelle soumission |
| §3-D | Approbation auto-déclarée | `is_operator_approved=True` et `approved_plan_id=<son propre id>` ouvrent la porte sans approbation serveur |
| §5-A | Plans incompatibles | demande 10000 CPU / 8 GPU / 10 000 000 Mo -> `is_feasible=True`, plans 2/88/16 CPU et 0 GPU |
| §5-B | Budget zéro | budget 0.0 -> 3 plans payants acceptés |
| §5-C | Crash | `cluster_total_cpu=1` -> `ValueError: min() iterable argument is empty` |
| §5-D | Quota malhonnête | projet `totally-fake-project-xyz` / région `mars-north9` -> `QUOTA_AVAILABLE`, "Quota verified" (défauts 256/8) |
| §6 | Historique absent | parcours complet -> `/api/history?workload_id=...` renvoie **404** |
| §6-bis | plan_id non unique | comparateur renvoie `plan-cost-optimized` (collision inter-workloads) |
| §6-ter | approbation fantôme | `/api/plans/approve` renvoie HTTP 200 avec `status: not_found` |
| §3-E | Porte MCP non gouvernée | `POST /plans/compare` (MCP) renvoie des plans jamais enregistrés -> aucun ne peut être exécuté ; `POST /plans/approve` (MCP) écrit seulement dans l'historique, que la porte d'exécution ne lit pas, et répond HTTP 200 avec `status: not_found` |
| §4-D | Contrainte avalée en silence | `WorkloadProfile` ignorait les clés inconnues : `budget_eur` / `cpu_count` (noms plausibles, inexistants) étaient supprimés et la réponse restait `is_feasible: True` sans que le budget ait jamais été vu |
| §5-E | Contraintes de localisation perdues | `POST /api/capacity-search` ne transmettait ni `allowed_regions`, ni `target_region`, ni `allow_region_change` -> une demande limitée à `europe-west4` recevait des candidats `us-central1` présentés comme compatibles |
| §6-A | Checkpoint jamais vérifié | `can_resume_from_checkpoint` répondait « Previous attempt #1 can be recovered without restarting from 0% » sur la seule foi de trois booléens ; aucun accès au stockage |
| §6-B | Reprise fictive enregistrée | la 2e tentative enregistrait `checkpoint_recovered_from = "<location>/step_latest"`, chemin fabriqué depuis un gabarit, jamais cherché |
| §6-C | Coût cumulé figé | `finish_attempt` ne recalculait pas les totaux : après 2 tentatives à 3,00 € et 2,00 €, le manager annonçait 2,00 € |
| §6-D | Sondage destructeur | `update_progress` écrasait `cost_calculated_eur` et `elapsed_minutes` avec les valeurs par défaut `0.0` ; un simple sondage d'état effaçait le coût réel |
| §6-E | **Perte d'historique** | `track_workload_lifecycle` ré-enregistrait tout workload absent de la mémoire du process : après redémarrage, un run terminé passait de 7,50 € / 1 tentative / COMPLETED à 0,00 € / 0 tentative / DEFINED |
| §6-F | Estimation de durée nulle | un plan sans ETA calculée donnait `estimated_duration_minutes = 0` -> 65 minutes observées devenaient un dépassement de 100 % |
| §1 | Dépendances de test non déclarées | `pip install -e ".[dev]"` puis `pytest` échouait sur un poste neuf : ni `httpx` (requis par `fastapi.testclient`) ni `dukpy` (requis par la porte JSX) n'étaient déclarés |

## Décisions
- **D1** : Node.js absent de l'environnement. La compilation JSX est vérifiée hors-ligne via le
  compilateur TypeScript 5.7.3 embarqué dans `dukpy` (`tools/check_jsx.py`), qui détecte la
  classe d'erreur exacte (TS2657 = "JSX expressions must have one parent element", équivalent
  Babel "Adjacent JSX elements..."). Le Babel embarqué de dukpy (6.26) a été écarté : trop
  ancien, faux positifs sur l'optional chaining. Tests de contrôle négatif inclus.

## Améliorations optionnelles (journal préalable, maximum 3)

### AO-1 — Harnais de rendu hors navigateur (`tools/render_check.py`) — **implémentée**
- **Problème** : aucun navigateur ni Node dans l'environnement. Une erreur de rendu
  (référence indéfinie, champ inexistant) ne serait détectée par personne.
- **Bénéfice** : les quatre nouveaux panneaux sont réellement exécutés ; une régression du
  type `c.cpu` alors que le modèle expose `cpu_count` est attrapée automatiquement.
- **Solution minimale** : transpiler le bloc babel avec le TypeScript embarqué de `dukpy`,
  l'exécuter dans duktape contre un React minimal, rendre chaque onglet.
- **Dépendances** : `dukpy` (déjà présent, désormais déclaré dans l'extra `dev`).
- **Test d'acceptation** : deux contrôles négatifs prouvent que le harnais détecte une
  référence indéfinie et un panneau manquant. `tests/test_dashboard_jsx.py` : 17 passed.
- **Limite déclarée** : ce n'est pas un navigateur. CSS, mise en page, dispatch d'événements
  réels et comportement réseau ne sont pas couverts.

### AO-2 — Contraintes de région exposées dans le tableau de bord — **implémentée**
- **Problème** : le backend honore désormais `allowed_regions` / `allow_region_change`, mais
  l'opérateur n'avait aucun moyen de les exprimer ni de savoir quelle région avait été
  interrogée. Une réponse mono-région pouvait passer pour exhaustive.
- **Bénéfice** : la contrainte de localisation, qui fait partie du périmètre (capacité 1),
  devient utilisable sans écrire de JSON à la main.
- **Solution minimale** : deux champs dans le panneau Capacité et un encart qui affiche
  `searched_region`, les régions autorisées et la note du serveur.
- **Dépendances** : aucune.
- **Test d'acceptation** : `tools/render_check.py` rend l'onglet sans erreur ;
  `tests/test_capacity_location_constraints.py` couvre le contrat serveur (7 tests).

*(Aucune autre amélioration optionnelle n'est ouverte. Plafond restant : 1.)*

## Tests et résultats

| Fichier | Tests | Rôle |
|---|---|---|
| `tests/test_dashboard_jsx.py` | 17 | compilation JSX, rendu de chaque onglet, contrat UI/modèle par panneau, 3 contrôles négatifs |
| `tests/test_http_journey.py` | 8 | parcours complet via l'application FastAPI réelle |
| `tests/test_mcp_http_governance.py` | 7 | gouvernance de la surface HTTP MCP (6/7 échouent avant correction) |
| `tests/test_capacity_location_constraints.py` | 7 | contraintes de localisation + paliers de quota (4/7 échouent avant correction) |
| `tests/test_checkpoint_verification.py` | 6 | vérification réelle du checkpoint (5/6 échouent avant correction) |
| `tests/test_cost_accounting_journey.py` | 5 | coût compté une fois par tentative, reprise honnête (5/5 échouent avant correction) |
| `tests/test_slurm_telemetry_defects.py` | 9 | les 7 défauts §7 (6/9 échouent avant correction) |
| `tests/test_mcp_contract.py` | 15 | contrat MCP + outils historiques soumis à la gouvernance |
| `tests/test_slurm.py` | 23 | adaptateur Slurm |
| **Suite complète** | **136 passed** (run du 2026-09-11T20:09Z, 4 min 36 s) | |

### Vérifications hors pytest
- **Environnement neuf** : `pip install --target /tmp/agtarget ".[dev]"` puis import de
  `fastapi`, `dukpy`, `httpx`, `uvicorn`, `pytest`, `agentic_compute`, `google.adk` avec
  `PYTHONNOUSERSITE=1` -> OK. Les deux dépendances de test manquantes ont été déclarées.
- **Démarrage réel** : `PORT=8099 python3 -m compute_agent.app` -> `Application startup
  complete`. `/health` -> `{"status":"healthy"}`. `/` avec `Accept: text/html` -> 93 785
  octets contenant les 5 onglets ; `/` sans en-tête HTML -> métadonnées JSON (négociation
  de contenu volontaire, documentée dans le README).
- **Parcours complet sur le serveur en cours d'exécution** (curl, pas de TestClient) :
  1. `/api/capacity-search` avec `allowed_regions: ["europe-west4"]` -> 16 candidats,
     `searched_region: europe-west4`, `data_provenance: gcp_live_api`,
     `quota_status: QUOTA_EXCEEDED`, `state_stage: catalog_proposed`.
     **Ce point utilise l'API GCP réelle** (des identifiants sont disponibles sur ce poste).
  2. `/api/plans/compare` avec `budget_eur` -> **HTTP 400** nommant le champ refusé.
  3. `/api/plans/compare` valide -> 3 plans, `registered: true`,
     `cost_basis: modelled_flat_rate_hypothesis`.
  4. `/api/workloads/wl-live/control` -> `validation`, `source: server_default`.
  5. exécution **avant** approbation, avec `is_operator_approved: true` et le bon
     `approved_plan_id` -> `status: blocked`, « caller-supplied approval flags are ignored ».
  6. approbation d'un plan inventé -> **HTTP 404**.
  7. approbation réelle -> `status: approved` + empreinte.
  8. exécution -> `status: submitted`, `job_id: agentgrid-wl-live-20d2a7846f55`,
     `verified: true`, `attempt_id: wl-live-att-1`, `history_linked: true`.
  9. double-clic -> `status: already_submitted`, **même** `job_id`, aucune seconde soumission.
  10. `/api/history?workload_id=wl-live` -> **HTTP 200**, 1 tentative, estimation et coût
      observé distincts.

## Tests modifiés (et pourquoi)
- `tests/test_evolved_capabilities.py::test_scenario_10_...` : l'assertion
  `att2.checkpoint_recovered_from is not None` **encodait le défaut §6-B**. Le scénario
  couvre désormais les deux issues honnêtes : un checkpoint réellement présent produit une
  reprise, un `gs://` non inspectable ne revendique rien. Aucune assertion affaiblie : le cas
  positif est testé sur un checkpoint local réellement écrit.
- `tests/test_dashboard_jsx.py::test_panels_only_read_fields_...` : passé d'un balayage du
  fichier entier à un balayage **par panneau**. `c` désigne un `CandidateAllocation` dans
  l'onglet mission et un `CapacityCandidate` dans l'onglet capacité ; le balayage global
  produisait 8 faux positifs qui auraient noyé les vrais. Un contrôle négatif prouve que la
  version restreinte détecte toujours un champ inexistant.
- `tests/test_slurm.py` (5 tests) et `tests/test_mcp_contract.py` (2 tests) : ajout explicite
  de `MOCK_SLURM=true` avec commentaire. Ce sont des tests unitaires hors-ligne ; la nouvelle
  garde §7-7 (« pas de simulateur silencieux quand le contrôleur est injoignable ») les
  faisait échouer parce qu'ils ne stubbaient pas `requests.get`. La garde n'a pas été affaiblie.

## Blocages
- **B1** : pas de Node.js/npm, pas de navigateur headless. La **validation visuelle en
  navigateur n'a pas été réalisée**. `tools/check_jsx.py` et `tools/render_check.py` la
  remplacent partiellement (syntaxe, rendu, références) mais ne couvrent ni le CSS, ni la
  mise en page, ni le dispatch d'événements réels, ni le comportement réseau du navigateur.
- **B2** : `google-cloud-storage` n'est pas installé sur ce poste. La vérification d'un
  checkpoint `gs://` retourne donc `unverified` avec le motif exact, et la reprise est
  déclarée « non prouvée ». Le chemin local est, lui, réellement vérifié et testé.
- **B3** : pas de cluster Slurm réel ni de `slurmrestd` joignable. Tout le §7 est prouvé sur
  des réponses HTTP simulées, pas sur un cluster. L'intégration Slurm réelle **n'est pas
  validée**.
- **B4** : l'exécution utilise le simulateur (`SimulatedRuntime`). Aucune VM n'a été
  provisionnée. Seule la lecture de quota GCP a été exercée en réel.
