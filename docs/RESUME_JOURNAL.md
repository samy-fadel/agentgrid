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

## Décisions
(à compléter)

## Tests et résultats
(à compléter)

## Blocages
(à compléter)

## Prochaine action exacte
Reproduire les défauts §2 (JSX) et §4 (faux succès simulateur).
