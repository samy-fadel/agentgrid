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
      — le cumul est **dérivé du registre de soumissions serveur** (`get_commitments`), pas
      fourni par l'appelant ; celui-ci ne peut que resserrer la borne (défaut §3-F reproduit)
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
| §3-F | **Plafond délégué non cumulatif** | mode `delegation`, `max_budget_eur = 10,00 €` : trois plans distincts à 8,00 € ont tous été acceptés (`submitted` × 3, trois job ids réels) soit 24,00 € engagés sous un plafond de 10,00 €. `check_policy_bounds` acceptait bien un `accumulated_cost_eur`, mais aucun appelant hors échelle de repli n'en fournissait : le plafond ne voyait jamais qu'un plan isolé. Même famille que les drapeaux d'approbation auto-déclarés — une borne qui dépend du bon vouloir de l'appelant n'est pas une borne |
| §2-A | **Enregistrement Slurm ignoré** | `slurm_job_details` figurait dans la signature de `diagnose_blockers`, l'outil MCP le transmettait, et la fonction ne le lisait jamais. Un enregistrement slurmrestd complet (`state_reason: BadConstraints`, `admin_comment: ZONE_RESOURCE_POOL_EXHAUSTED…`, `dependency: afterok:4700`) renvoyait `category: unknown`, `observed_facts: "Job state: UNKNOWN, Reason: None"`, `confirmed: false`. De plus `POST /api/diagnose` ne transmettait pas du tout le champ : deux façons de perdre la même preuve. Slurm-GCP écrit les erreurs du fournisseur dans `admin_comment` — c'est souvent le seul endroit où une pénurie est consignée |
| §1-A | **Plafond GPU global jamais vérifié** | `GPUS_ALL_REGIONS` figurait dans la liste des métriques **régionales** (`_GPU_QUOTA_METRICS`). C'est un plafond **projet**, jamais renvoyé par `regions.get` : il n'était donc jamais contrôlé. Reproduction (réponse régionale `CPUS 512/0`, `NVIDIA_L4_GPUS 8/0`, demande 24 vCPU + 4 GPU) : `{"status": "QUOTA_AVAILABLE", "is_known": true, "reason": "Request fits within quota (verified against live Compute Engine regional quota)"}` — alors qu'avec `GPUS_ALL_REGIONS = 0` (valeur par défaut fréquente sur un projet neuf) **toutes** les créations de VM auraient échoué. Contrat vérifié dans la documentation officielle avant intégration : [regions.get](https://cloud.google.com/compute/docs/reference/rest/v1/regions/get) et [projects.get](https://cloud.google.com/compute/docs/reference/rest/v1/projects/get) — même forme `quotas[] = {metric, limit, usage, owner}` ; `quotaStatusWarning` est un **objet** `{code, message, data[]}` (il était interpolé tel quel, imprimant un `repr` de dict) ; `regions.get` **échoue en mode ouvert** (HTTP 200 sans champ `quotas`) sauf si la contrainte d'organisation `compute.requireBasicQuotaInResponse` est appliquée |
| §3-H | **Course sur les plafonds délégués (TOCTOU)** | les plafonds étaient évalués sur une connexion, puis la réclamation était insérée sur une autre. Quatre soumissions **concurrentes** de quatre plans *différents* ne partagent pas de clé de soumission : la clé primaire ne les sépare donc pas. Reproduction avec 4 threads et `max_retries = 1` : `['submitted', 'submitted', 'submitted', 'submitted']`. Corrigé en évaluant les plafonds dans la transaction `BEGIN IMMEDIATE` qui insère la réclamation |
| §3-G | **Plafond de tentatives jamais appliqué** | `max_retries` était vérifié par `check_policy_bounds`, mais aucun appelant ne fournissait `attempts_used` : la valeur par défaut `0` désarmait le test. Reproduction : `max_retries = 1`, quatre plans distincts, **quatre jobs lancés**. Aggravant : `release_submission` faisait un `DELETE`, donc l'historique des lancements s'effaçait lui-même — une boucle « relâcher puis relancer » était illimitée, et une soumission `uncertain` (qui a peut-être créé un job) cessait d'être imputée au budget |
| §4-D | **Contraintes du workload non opposables à l'exécution** | seule la `DelegationPolicy` était consultée avant de lancer. Elle est facultative et distincte du profil : rien n'obligeait l'opérateur à y recopier ses contraintes. Reproduction avec un profil `allowed_regions=["europe-west4"]`, `allow_region_change=False`, `allow_spot=False`, `allow_fallback_to_standard=False` — (A) un plan SPOT en `us-central1` a été **soumis** (`status: submitted`, `job_id: agentgrid-wl-loc-2ea0098ca1f3`, `verified: true`) ; (B) l'échelle de repli, qui ne recevait jamais le profil, a **choisi** `us-central1` en STANDARD : le pas censé être le plus contrôlé était le moins contrôlé ; (C) en mode `validation` l'échelle renvoyait `selected` sans dire que le plan devait encore être enregistré et approuvé. Corrigé par un contrôleur partagé `check_profile_constraints`, appliqué à la soumission dans **tous** les modes (une approbation autorise un plan, elle n'abroge pas une contrainte de localisation) et à chaque barreau de l'échelle (barreau non conforme ignoré, motif consigné dans `skipped_candidates`, `constraint_breach` s'il ne reste rien). `profile_constraints_source` indique quand aucun profil n'a pu être lu, plutôt que de laisser croire à une conformité vérifiée |
| §4-E | **Contraintes fournies par l'appelant au moment d'exécuter** | `/api/execute-plan` et l'outil MCP `execute_plan_controlled` prennent le profil dans le corps de la requête. Un appelant pouvait donc élargir `allowed_regions` au moment de lancer et franchir sa propre contrainte — même famille que les drapeaux d'approbation auto-déclarés. Aggravant : `/api/plans/compare` **ne persistait pas le profil**, donc le serveur n'avait aucune trace des contraintes ayant servi à construire les plans. Corrigé : les deux routes de comparaison (HTTP et MCP) enregistrent le profil (`profile_persisted: true`), et `submit_plan` évalue le plan contre le profil persisté **et** contre celui de la requête — l'appelant ne peut que resserrer. Le refus indique lequel des deux a tranché (`constraint_source`) |
| §3-A | **Dimension « capacité » absente de la comparaison de plans** | la capacité 3 du périmètre est une comparaison **coût–délai–capacité**, et la docstring d'`ExecutionPlan` annonce « cost, latency, and capacity ». Le modèle ne portait aucun champ de capacité et `plan_engine` ne consultait aucun quota. Reproduction sur un workload de 64 vCPU en `europe-west4` : deux plans à **88 vCPU** renvoyés avec `capacity-related keys: []` — l'opérateur devait choisir entre deux plans sans savoir si 88 vCPU sont seulement obtenables. Corrigé en câblant `check_quota_availability` (module existant) : chaque plan porte `capacity_status` / `capacity_detail` / `capacity_source`, un quota illisible reste `QUOTA_UNKNOWN` (jamais `QUOTA_AVAILABLE`), un quota dépassé est aussi versé dans `unverified_points`, et une seule requête est faite par forme distincte. **Vérifié contre l'API GCP réelle** : la même demande répond `QUOTA_EXCEEDED: requested 88 vCPUs, available 0/0 (verified against live Compute Engine regional quota)` avec `data_provenance: gcp_live_api`. La vérification est désactivée dans la suite de tests (`AGENTGRID_PLAN_CAPACITY_CHECK=false` dans `conftest.py`) pour la même raison que `MOCK_SLURM` |
| §1-B | **Quota lue dans un projet que l'opérateur n'a jamais nommé** | `capacity_advisor` retombait sur un identifiant de projet codé en dur (`dubai-489009`) à deux endroits distincts quand ni `GOOGLE_CLOUD_PROJECT` ni `PROJECT_ID` n'étaient définis, puis annonçait « verified against live Compute Engine regional quota » — une phrase vraie sur *ce* projet et trompeuse sur celui de l'opérateur. Observé pendant le parcours en direct : `/api/capacity-search` répondait `QUOTA_EXCEEDED` avec `data_provenance: gcp_live_api` alors que le serveur n'avait aucun projet configuré, pendant que la comparaison de plans, elle, répondait honnêtement `no_project_configured` : **les deux fonctionnalités ne lisaient pas la même chose**. Corrigé par un résolveur unique `resolve_quota_project()` renvoyant `(project_id, source)` avec `caller` / `environment` / `built_in_default` ; chaque verdict porte `quota_project` et `quota_project_source`, la raison le dit explicitement quand le défaut interne a servi, et le tableau de bord affiche « ⚠ projet par défaut » |
| §4-F | **Le repli contrôlé n'était exposé nulle part** | `evaluate_fallback_ladder_with_details` était implémenté et testé, et **aucun appelant ne pouvait l'atteindre** : `grep -rn "evaluate_fallback_ladder" src/agentic_compute/mcp_server.py compute_agent/app.py` ne renvoyait rien, et le tableau de bord n'avait aucun contrôle. Pendant ce temps la docstring de `execute_plan_controlled` annonçait « fallback handling ». La capacité 4 du périmètre est « exécution **avec plan de repli contrôlé** » : l'opérateur comme l'agent en étaient privés. Corrigé par câblage : `POST /api/workloads/{id}/fallback`, outil MCP `select_fallback_plan`, bouton « Proposer un repli » et encart de verdict dans le tableau de bord. La décision ne soumet rien (`submitted: false`, `next_step` renvoie vers `/api/execute-plan`), et aucune règle de l'échelle n'a été assouplie au passage |
| §2-B | **Le correctif §2-A était inaccessible à l'agent** | `diagnose_blockers` lit l'enregistrement slurmrestd complet et `POST /api/diagnose` le transmet, mais l'outil MCP `diagnose_blockers_tool` — la seule surface que voit l'agent — n'exposait pas `slurm_job_details`. Le paramètre existait dans la bibliothèque, pas dans l'outil : pour l'agent, `admin_comment` restait invisible et la pénurie se classait `unknown`. Corrigé, avec un test qui compare la signature de l'outil à celle de la bibliothèque pour que l'écart ne puisse pas réapparaître |
| §1-C | **`check_project_quota` jetait les chiffres** | l'aide lisait `res["limit"]` et `res["usage"]` alors que le verdict expose `quota_limit` / `quota_usage` : elle renvoyait donc toujours `(status, None, None)`, sans jamais échouer |
| §2-C | **Une limite de l'ordonnanceur imputée au fournisseur cloud** | `diagnose_blockers` cherchait les sous-chaînes `qos` et `assocmax` dans *tout* le texte d'erreur collecté, puis émettait un constat de `source: gcp_compute_quota` dont la première action était « Request GCP quota increase for the target region ». Reproduction : `state_reason = "QOSMaxJobsPerUserLimit"` -> `category: quota`, `source: gcp_compute_quota`, action « Request GCP quota increase ». Or un `QOSMaxJobsPerUserLimit` est un plafond détenu par la base de comptabilité du cluster (slurmdbd) : **aucune** augmentation de quota cloud ne le déplace, et l'opérateur est envoyé demander la mauvaise chose à la mauvaise personne. Le module frère `agentic_compute.diagnostic` classait déjà le même signal correctement (`source: slurm_controller`, remèdes côté cluster) : **les deux chemins de diagnostic se contredisaient** pour une entrée identique. Deux défauts secondaires dans la même détection — (a) `assocmax` seul ne couvrait pas la famille `AssocGrp*`, si bien que `AssocGrpCPURunMinutes`, blocage très courant sur un cluster partagé, tombait en `category: unknown` ; (b) la recherche portant sur le journal applicatif, une trace qui *mentionnait* la QoS fabriquait une limite inexistante. Corrigé : détection sur le seul champ `state_reason` via `_is_scheduler_limit_reason` (préfixes `qos`, `assocmax`, `assocgrp`, `assocjob`, `assocnode`), constat distinct sourcé `slurm_controller` avec trois remèdes réels (réduire le parallélisme, attendre ses propres jobs, faire relever la QoS par l'administrateur du cluster), et cumul possible des deux constats quand un vrai quota GCP est présent en plus. La suppression du constat de pénurie ne dépend plus que du quota **cloud** |
| §1-D | **Une seule région interrogée sur toutes celles autorisées** | `search_compatible_capacity` réduisait `allowed_regions` à `allowed_regions[0]`. Reproduction avec `allowed_regions = ["europe-west4", "europe-west1", "us-central1"]` : `regions actually searched: ['europe-west4']`. Un opérateur qui déclare trois régions acceptables recevait une réponse portant sur une seule ; si celle-là était en pénurie, le produit annonçait « aucune capacité compatible » sans avoir jamais interrogé deux régions explicitement autorisées. La capacité 1 du périmètre est de *trouver* la capacité compatible dans les emplacements permis, pas d'expliquer qu'on n'a pas regardé — et la route HTTP se contentait précisément de cela, via une note « were not explored ». Second défaut : quand la région demandée était hors liste et `allow_region_change` faux, la fonction renvoyait un `[]` nu, indiscernable de « rien dans le catalogue ne correspond à votre matériel ». Corrigé par un résolveur unique `resolve_search_regions(profile, region)` renvoyant `(régions, note)`, utilisé **à la fois** par la recherche et par la route HTTP — ils calculaient auparavant chacun leur idée de la région interrogée, ce qui les faisait diverger. Une cible explicite continue de restreindre la recherche à cette seule région ; le nombre de régions par recherche est plafonné (`MAX_SEARCH_REGIONS = 5`) et les régions écartées sont nommées. La réponse expose `searched_regions` (et conserve `searched_region` pour les appelants existants) |
| §1-E | **La même page de quota relue une fois par type de machine** | `check_quota_availability` est appelée dans la boucle par type de machine, et chaque appel émettait une requête `compute.regions.get` neuve. La réponse décrit pourtant la région entière : une seule recherche faisait huit allers-retours HTTP pour le même document. Le correctif §1-D multiplie cela par le nombre de régions — un clic sur le tableau de bord devenait des dizaines d'appels réels. Mesure avant/après sur les 12 tests de `tests/test_multi_region_search.py`, exécutés contre l'API GCP réelle : **88,72 s -> 5,08 s**. Corrigé par un cache à durée de vie courte (`QUOTA_CACHE_TTL_SECONDS`, 30 s par défaut, `AGENTGRID_QUOTA_CACHE_TTL=0` pour désactiver) placé dans un enveloppeur `_fetch_quotas` autour du corps réseau inchangé, renommé `_fetch_quotas_uncached`. **Seules les lectures réussies sont mémorisées** : une panne, un identifiant refusé ou un HTTP 503 doivent remonter à chaque fois, faute de quoi une erreur passagère se figerait en réponse fausse durable. `reset_quota_cache()` force une relecture |
| §6-G | **Chiffres déclarés présentés comme mesurés** | `reconcile_costs` additionnait *toutes* les tentatives dans `tier2_calculated_from_usage_eur`, y compris celles enregistrées avec `cost_status = "estimated"` — c'est-à-dire les montants qu'un appelant (le modèle de langage, via l'outil MCP) a simplement énoncés. Reproduction : 4,00 € mesurés + 9,00 € déclarés -> `tier2 = 13.0` accompagné de la note « Observed cost is calculated from **authoritative** active node-hours and a known rate ». Second mensonge dans la même charge utile : `reconcile_costs(wl, billed_cost_eur=99.0)` renvoyait `billed_reconciliation_status: reconciled_billed` alors qu'aucune intégration de facturation n'existe dans ce projet — n'importe quel nombre passé en argument devenait une « facturation réconciliée ». Corrigé : séparation par base (`tier2` mesuré / `declared_unverified_cost_eur` / `total_recorded_cost_eur`), note conditionnelle en trois variantes, et statut `caller_supplied_unverified` sauf si `billed_cost_source = "gcp_billing_export"` |

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
| `tests/test_delegation_budget_ceiling.py` | 15 | plafonds délégués réellement cumulatifs (budget **et** tentatives), dérivés du registre serveur ; claim relâché archivé, pas supprimé |
| `tests/test_concurrent_submission.py` | 4 | idempotence et plafonds sous concurrence réelle (8 threads), pas seulement en rejeu séquentiel |
| `tests/test_quota_api_contract.py` | 13 | plafond GPU projet réellement lu, formes de réponse documentées, `quotaStatusWarning` rendu correctement |
| `tests/test_fallback_exposure.py` | 11 | le repli contrôlé réellement atteignable en HTTP et en MCP, sans assouplir une seule règle de l'échelle |
| `tests/test_mcp_tool_surface.py` | 4 | ce que l'agent peut atteindre correspond à ce que la bibliothèque sait faire (4/4 échouent avant correction) |
| `tests/test_plan_capacity_dimension.py` | 12 | troisième axe de comparaison réellement présent ; `QUOTA_UNKNOWN` jamais confondu avec `QUOTA_AVAILABLE` |
| `tests/test_profile_constraints.py` | 20 | les contraintes déclarées sur le workload (région, zone, Spot, bascule vers Standard) opposables à la soumission **et** au repli |
| `tests/test_cost_reconciliation_honesty.py` | 11 | un coût déclaré n'entre pas dans le palier « mesuré », et un chiffre facturé sans source n'est pas une réconciliation (10/11 échouent avant correction) |
| `tests/test_diagnostics_slurm_record.py` | 14 | `slurm_job_details` réellement lu, `admin_comment` classé, scalaires slurmrestd non inventés (8/14 échouent avant correction ; les 6 autres sont des garde-fous) |
| `tests/test_scheduler_limit_attribution.py` | 39 | une limite de l'ordonnanceur garde sa véritable origine (20/39 échouent avant correction) |
| `tests/test_multi_region_search.py` | 12 | toutes les régions autorisées réellement interrogées, un seul résolveur pour la recherche et pour l'explication |
| `tests/test_quota_fetch_cache.py` | 8 | un document de quota lu une fois par recherche ; une panne n'est jamais mise en cache |
| `tests/test_mcp_contract.py` | 15 | contrat MCP + outils historiques soumis à la gouvernance |
| `tests/test_slurm.py` | 23 | adaptateur Slurm |
| **Suite complète** | **227 passed** (run du 2026-09-11T21:35Z, 4 min 58 s) | |

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
- `tests/test_evolved_capabilities.py::test_scenario_2_...` (ligne ~140) et
  `tests/test_agentgrid_evolutions.py` (persistance 3 paliers) : ces deux tests appelaient
  `reconcile_costs(..., billed_cost_eur=...)` **sans source** et attendaient
  `reconciled_billed`. C'est exactement le défaut §6-G : un nombre fourni par l'appelant ne
  prouve rien. Leur intention (vérifier le palier 3) est conservée en nommant explicitement
  la source `billed_cost_source="gcp_billing_export"`. Assertion **renforcée**, pas affaiblie :
  la voie non qualifiée est désormais couverte séparément et attend
  `caller_supplied_unverified`.
- `tests/test_delegation_budget_ceiling.py` et `tests/test_concurrent_submission.py` : les profils
  de ces tests ne déclaraient pas `allowed_regions`, alors que leurs plans s'étalent sur
  plusieurs régions européennes pour obtenir des empreintes distinctes. Le défaut par défaut du
  modèle est `["us-central1"]` : après la correction §4-D, ces soumissions sont légitimement
  refusées pour cause de localisation. Les profils déclarent désormais les régions qu'ils
  utilisent. Aucune assertion sur les plafonds n'a changé — un test de budget ne doit pas se
  transformer par accident en test de localisation.
- `tests/test_capacity_location_constraints.py::test_a_multi_region_allow_list_admits_it_only_searched_one`
  **encodait le défaut §1-D** : il exigeait `searched_region == "europe-west4"` et une note
  disant qu'`europe-west1` n'avait « pas été explorée ». Il figeait donc la limitation qu'il
  décrivait. Renommé `..._is_searched_in_full` et **renforcé** : les deux régions doivent
  désormais apparaître dans `searched_regions` *et* dans les candidats renvoyés. Aucune
  assertion n'a été retirée — celles sur la région hors liste et sur `allow_region_change`
  sont inchangées et passent toujours.
- `tests/conftest.py` : la fixture autouse vide désormais le cache de quota
  (`capacity_advisor.reset_quota_cache()`) avant et après chaque test. Sans cela, la réponse
  simulée d'un test était servie au test suivant, qui simule une réponse *différente* pour la
  même URL — trois tests de `tests/test_quota_api_contract.py` échouaient pour cette seule
  raison. C'est une correction d'isolation, pas un assouplissement : les trois assertions
  concernées sont inchangées.
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
