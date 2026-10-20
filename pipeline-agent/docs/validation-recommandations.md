# Contrôles des recommandations

Itération du 29 septembre 2026. La collecte et la génération IA d’origine sont conservées.
La modification porte sur le contrat de sortie et sa validation locale avant affichage.
Aucun appel CRM ou modèle externe n’a été effectué pour cette itération.

## Comportement

La génération produit une décision, un motif, une proposition, une éventuelle date et
des citations. Chaque citation référence une activité réellement fournie au modèle,
ou le champ de prochaine action, avec un extrait vérifié dans le contenu transmis.
Les identifiants d’activités sont conservés lors de la collecte.

Les contrôles vérifient :
- Le format de la réponse et la présence des champs nécessaires.
- L’existence des sources citées et la correspondance textuelle des extraits.
- La prise en compte, par une citation obligatoire, de la prochaine action renseignée.
- Les dates explicites : validité, absence de date passée, présence dans l’extrait cité.
  L’horodatage d’un email ou d’une réunion n’est pas considéré comme une échéance promise.
- Le respect des actions datées existantes et des prochaines actions non datées.
- Les messages entrants restant à interpréter, les indices reconnus de refus/report,
  les brouillons identifiés par leur statut et le contexte explicitement incomplet.

Une décision « conserver » reprend l’action enregistrée ; une décision « attendre »
affiche une consigne d’attente. Elles ne peuvent pas faire afficher comme action une
nouvelle relance rédigée dans un champ texte contradictoire. Une attente ne peut pas
avancer une date d’action existante.

Les dates sont séparées du texte de l’action, puis rendues lisiblement par le programme.
La validation reconnaît des dates explicites ISO, françaises et anglaises. Les formats
non reconnus ne sont pas interprétés arbitrairement ; une échéance non étayée est refusée.

Une réponse invalide ou tronquée devient un point « Vérification requise », avec le motif
du blocage. La copie rejetée n’est pas affichée comme une action à exécuter. Les extraits
retenus et les dates vérifiées apparaissent dans les aperçus Slack et HTML existants.
Les sorties JSON privées conservent les identifiants de preuve et le détail des contrôles.

## Limites et validation humaine

`checks_passed` signifie que les contrôles programmés ont réussi, pas que le modèle a
compris parfaitement le deal. Un extrait exact peut être mal interprété ; un contrôle de
citation ne démontre pas à lui seul la relation entre cet extrait et une conclusion.
Les indices de refus/report reposent sur des formulations françaises/anglaises reconnues,
pas sur un interpréteur exhaustif de toutes les langues et de toutes les intentions.

Par défaut, la collecte utilise les associations directes ; la recherche indirecte
optionnelle demande une confirmation pour les liens incertains.
Les extraits longs sont raccourcis
et les activités fournies au modèle sélectionnées ; ces limites sont déclarées et bloquent
une proposition de nouveau suivi lorsqu’une omission ou troncature est connue. Cela ne
prouve pas que toutes les interactions importantes figurent dans le CRM.

Toutes les propositions nécessitent donc une relecture. Aucun envoi, création de brouillon,
changement CRM ou déclenchement de planification n’est ajouté.

La vérification hors ligne utilise des réponses modèle simulées : elle teste les contrôles
et leur intégration, sans prétendre mesurer la qualité d’une génération réelle récente.

## Exécuter les contrôles

```bash
python3 -m unittest discover -s tests -v
```

Les cas couvrent citations inventées, échéances passées/non étayées, rendez-vous déjà prévu,
action non datée, réponse entrante, refus dans une source différente de celle sélectionnée,
brouillon existant, contexte tronqué, sortie modèle interrompue et affichage des preuves.
