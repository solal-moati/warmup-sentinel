# Reprendre le fil d’un deal

Version du 20 octobre 2026. Fondements exécutables localement, tests fictifs.
Le nouveau parcours de contexte HubSpot est testé avec des réponses simulées,
pas sur un compte réel.

## Une mémoire de revue

`followup_state.py` conserve vos décisions dans un fichier privé : examiné,
écarté pour ce contexte, reporté jusqu’à une date, ou rouvert. « Examiné » ne
signifie jamais « tâche exécutée ». La mémoire porte sur le contexte du deal
entier, pas sur chaque action individuellement.

Une nouvelle activité, un changement de prochaine étape, une échéance passant en
retard ou la fin d’un report remet le dossier en avant. Le compteur de jours qui
augmente seul ne répète pas l’alerte. Les dossiers déjà examinés restent
consultables dans une section repliée. Les avertissements restent à vérifier.

```bash
python3 followup_state.py \
  --input /private/run/reviewed.json --output /private/run/remembered.json \
  --state /private/pipeline-memory.json --scope hubspot/account/pipeline/owner
python3 preview_recap.py --input /private/run/remembered.json \
  --output /private/run/recap.json --timezone Europe/Paris
```

Remplacer `/private` par un dossier privé de votre choix. Le `scope` désigne un
périmètre stable : CRM, compte, pipeline et responsable. Ne pas réutiliser cette
valeur pour une sélection ponctuelle différente. L’analyse et son `.meta.json`
doivent couvrir tout ce périmètre sans erreur.

Passer `--decisions decisions.json` pour enregistrer un retour :

```json
[{
  "deal_id": "copied-from-analysis",
  "fingerprint": "copied-from-followup_memory",
  "status": "reviewed",
  "until": null,
  "note": "Document préparé ; je vérifie son envoi avant de relancer."
}]
```

Statuts : `reviewed`, `dismissed`, `snoozed`, `reopen`. Seul `snoozed` utilise `until`,
un timestamp futur avec fuseau. Le récapitulatif affiche les dates en langage
lisible. Une décision visant une ancienne empreinte est refusée.
L’interface de retour est ce fichier local, pas des boutons Slack actifs.
Un assistant peut le préparer à partir de votre décision, sans l’inventer.

Écriture atomique, permissions privées, verrou local macOS/Linux (`fcntl`).
Une machine unique est prévue, pas une base multi-utilisateur. Garder le fichier
hors dépôt. Sa suppression réinitialise les décisions locales.

## Retrouver le contexte autour du deal

HubSpot reste en lecture directe par défaut. Activer `"indirect_context": true`
dans la configuration privée pour parcourir les contacts du deal, ses entreprises
et leurs contacts, même absents du deal, puis leurs notes, emails, appels,
réunions et tâches lisibles par votre jeton.

Les associations paginées sont parcourues jusqu’au bout. Une réponse incomplète
fait échouer la collecte du deal. Le parcours demande davantage de lectures et
peut nécessiter des permissions supplémentaires. Il ne récupère pas les pièces
jointes ou comptes rendus absents du CRM.

Un échange associé à un autre deal est écarté. Un échange non associé apparaît
dans « Contexte à rattacher », avec sa provenance, mais ne devient pas un fait.
Même entreprise ne signifie pas même opportunité.

Après lecture humaine, ajouter une confirmation à `context_confirmations`
dans la configuration privée :

```json
[{
  "deal_id": "copied-deal-id",
  "source_id": "notes:copied-id",
  "content_hash": "copied-from-context_scan-candidate",
  "reason": "Compte rendu relu : il concerne cette opportunité."
}]
```

Relancer la collecte. La confirmation ne s’applique que si le contenu n’a pas changé.
Elle ne crée aucune association dans HubSpot. Dans le format JSON commun, une
interaction `ambiguous` reste visible, exclue des faits. Après vérification par le
producteur de l’export, utiliser `verified` avec `association_reason`.

Référence : [pagination du SDK officiel HubSpot](https://github.hubspot.com/hubspot-api-nodejs/classes/crm_associations_v4.BatchApi.html).

## Qui attend quoi de qui ?

`commitments.py` importe une extraction effectuée par votre assistant existant :
notre équipe, côté client ou responsable inconnu ; action attendue ; date
explicitement promise ; dépendance à un autre engagement ; citations exactes.

Exemple : notre équipe doit envoyer le document sécurité ; le client pourra ensuite
valider le pilote. Une préparation de relance est bloquée si un engagement de notre
équipe reste à vérifier, si son responsable est inconnu ou si une dépendance
reste non résolue.

L’assistant prépare l’extraction selon `assistant-review.txt`. Le collecteur seul
ne l’effectue pas. Le validateur vérifie les sources, dates, dépendances et le lien
avec une capture de moins de 24 heures. Il ne prouve pas que l’interprétation est
juste : le commercial relit toujours.

```bash
python3 commitments.py --input /private/run/analysis.json \
  --proposals /private/run/commitments.json --output /private/run/enriched.json
python3 review.py --input /private/run/enriched.json \
  --proposals /private/run/proposals.json --output /private/run/reviewed.json
```

Une exécution déclarée exige une autre source datée, postérieure ou simultanée à
la promesse. Une absence de relance ou une date dépassée ne prouve rien.
Une date inconnue reste inconnue.

## Contribuer sans être développeur

Utiliser [le modèle de cas métier](business-case-template.md), ou l’issue « Sales
case » quand ce dossier sera publié comme dépôt indépendant. Un mainteneur traduit
le cas en `examples/community-case-*.json`. La suite de tests charge ces fichiers
automatiquement et vérifie les groupes et préparations attendus.

Chaque apport suit ce chemin : situation fictive, sources disponibles, résultat
attendu, exemple exécutable, test. Aucun export client dans les issues.

## Vérifier trois passages

```bash
python3 community_demo.py
python3 -m unittest discover -s tests -v
```

Ouvrir `output/community-demo/cycle-1.html`, puis `cycle-2.html` et `cycle-3.html`.
Les trois deals restent présents. Dans le scénario, ils sont examinés au deuxième
passage ; une nouvelle réponse et une date de reprise remettent deux dossiers en
avant au troisième. L’extraction des engagements est explicitement simulée.
Ce scénario n’est ni un résultat client ni une mesure de performance.

Aucun ordonnanceur, envoi ou brouillon en messagerie n’est créé. Les connecteurs
Attio/Folk et la livraison Slack restent des extensions.
