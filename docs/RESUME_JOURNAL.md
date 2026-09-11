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
- [ ] JSX compile (Babel) — bloc `activeTab === 'mission'` refermé
- [ ] Compilation frontend ajoutée au contrôle CI/tests
- [ ] Onglets Plans / Capacité / Diagnostic / Historique rendus
- [ ] États chargement / vide / erreur distincts ; jamais erreur affichée comme résultat
- [ ] Approbation + exécution branchées avec réponse serveur réelle
- [ ] Paramètres mission structurés (budget, deadline, mode) transmis indépendamment
- [ ] Origine API locale préservée

### §3 Contrôle backend
- [ ] Source de vérité serveur pour mode + politique (défaut `validation`)
- [ ] Tous les chemins de mutation (anciens outils MCP inclus) passent par les mêmes règles
- [ ] `advisory` : aucune mutation possible
- [ ] `validation` : plan enregistré (id unique + empreinte contenu, lié au workload) avant approbation
- [ ] Approbation persistée serveur ; booléen du modèle non suffisant
- [ ] Modification matérielle du plan approuvé invalide l'approbation
- [ ] `delegation` : plafonds cumulés + restrictions appliqués à chaque action ET repli
- [ ] Paramètres incompatibles rejetés explicitement

### §4 Faux succès / doublons
- [ ] Contrats RuntimeAdapter/simulateur/Slurm/MCP harmonisés (`machine_type`)
- [ ] Erreur de programmation/validation/politique != timeout ambigu
- [ ] Recherche post-timeout sur identité de soumission stable
- [ ] Vérification stricte du job attendu + état observé
- [ ] Idempotence à travers requêtes HTTP + redémarrage (stockage persistant)
- [ ] Rejeu distinct d'une nouvelle tentative autorisée

### §5 Plans + quotas
- [ ] Moteur de plans relié aux candidats compatibles + contraintes profil + capacités runtime
- [ ] CPU/GPU/mémoire/machines/régions/zones/parallélisme/interruptibilité/provisionnement vérifiés
- [ ] Pas de plan Slurm non exécutable présenté comme exécutable
- [ ] Budget zéro / invalide / aucun candidat / contradictions => explication structurée sans exception
- [ ] Coûts simulateur identifiés comme hypothèses
- [ ] `check_quota_availability` : QUOTA_UNKNOWN sans accès, jamais "verified" par défaut
- [ ] Valeurs synthétiques réservées au mode démo explicite
- [ ] Catalogue / quota / signal / allocation restent séparés

### §6 Exécution ↔ cycle de vie ↔ historique
- [ ] Parcours réel : profil → plans → approbation → tentative → observations → fin → historique
- [ ] `workload_id` / `plan_id` / `attempt_id` / `job_id` distincts et associés
- [ ] Coûts sans double comptage ; estimation / usage / facturation séparés
- [ ] Chiffre LLM non accepté comme mesure
- [ ] Réhydratation après redémarrage
- [ ] Checkpoint réellement vérifié, pas juste un chemin construit
- [ ] Repli soumis à budget/compatibilité/autorisation
- [ ] Downscale refusé retourne refus
- [ ] Diagnostics conservent source ; QoS Slurm != quota GCP

### §7 Slurm / télémétrie
- [ ] 1. Resize mauvais workload_id rejeté avant mutation
- [ ] 2. 999999 CPU sur 128 rejeté explicitement
- [ ] 3. Normalisation STANDARD / "100% Standard" ; BANANA rejeté
- [ ] 4. 1h sans allocation observée => pas de coût 0,20€ inventé
- [ ] 5. Nœuds occupés => pas de 124/128 CPU libres
- [ ] 6. Perte d'observation => observed_cpu inconnu
- [ ] 7. MCP indisponible => erreur explicite, pas de simulateur silencieux

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

## Décisions
- **D1** : Node.js absent de l'environnement. La compilation JSX est vérifiée hors-ligne via le
  compilateur TypeScript 5.7.3 embarqué dans `dukpy` (`tools/check_jsx.py`), qui détecte la
  classe d'erreur exacte (TS2657 = "JSX expressions must have one parent element", équivalent
  Babel "Adjacent JSX elements..."). Le Babel embarqué de dukpy (6.26) a été écarté : trop
  ancien, faux positifs sur l'optional chaining. Tests de contrôle négatif inclus.

## Tests et résultats
- `tests/test_dashboard_jsx.py` : 5 passed (dont 2 contrôles négatifs + 1 anti-faux-positif).

## Blocages
- **B1** : pas de Node.js/npm -> pas de Babel réel ni de navigateur headless. La validation
  visuelle en navigateur est donc **non réalisée** ; seule la compilation JSX est prouvée.

## Prochaine action exacte
Corriger §3/§4 : source de vérité serveur (mode, plan, approbation, empreinte),
harmonisation des contrats runtime, idempotence persistante.
