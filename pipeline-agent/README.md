# Open Pipeline Agent

**Base open source collaborative, sous licence MIT. Version de travail du 20 octobre 2026.**

## Démarrer avec Claude

Copiez-collez le lien de ce dossier dans Claude et dites-lui :

> Aide-moi à mettre en place l’agent pipeline. Lis le guide et commence par la
> démonstration fictive. Ensuite, guide-moi pour l’adapter à mon CRM.

Avec Claude Code et une copie locale, `CLAUDE.md` à la racine donne le point de
départ. Dans une conversation sans accès à votre ordinateur, Claude peut expliquer
les étapes ; vous exécutez les commandes localement.

## À quoi sert cette base ?

Scanner les opportunités ouvertes, comprendre ce qui mérite l’attention du commercial
ou de l’account manager et préparer le travail utile pour les faire avancer.
**Aucune limite de deals et aucun jour ou horaire imposé.**

Un deal qu’on retrouve trop tard, une relance promise, une prochaine action dispersée
dans les notes : ces problèmes se retrouvent dans des équipes qui vendent très différemment.
Cette base partage les mécanismes de revue. Chacun garde ses étapes, ses règles et son CRM.
Le commercial décide des envois et des modifications.

Le connecteur HubSpot est livré. Pour un autre CRM, `snapshot.py` analyse un export
JSON mappé au format commun. Les connecteurs API Attio et Folk restent à développer :
leur nom dans les tests ou les visuels n’est pas une validation de compatibilité API.

[Contribuer](CONTRIBUTING.md) · [Guidelines](docs/guidelines.md) ·
[Ajouter son CRM](docs/snapshot-contract.md) · [Licence MIT](LICENSE)

**Nouveaux fondements :** mémoire locale des dossiers déjà examinés, recherche
optionnelle du contexte indirect HubSpot avec attribution contrôlée, engagements
des deux côtés extraits par votre assistant et validés par leurs citations.
[Fonctionnement, commandes et limites](docs/community-foundations.md).
Les cas métier proposés par la communauté peuvent devenir des tests exécutables.

Une contribution peut être une règle métier, un cas mal interprété, une configuration
ou un connecteur. Le projet contient du code exécutable et des tests, avec des limites
documentées. Les exemples sont fictifs et ne mesurent pas un gain de temps ou de revenu.

[Guide de démarrage](QUICKSTART.md) : utiliser votre assistant existant avec
`assistant-review.txt`, ou choisir l’API Anthropic. Le premier parcours ne demande
pas de clé API de modèle supplémentaire. Il nécessite un assistant autorisé à lire
vos données CRM et conserve toutes les préparations localement.

L’itération actuelle contient un connecteur HubSpot partiel, un import indépendant du CRM,
un moteur de signaux, un rapport local et un calculateur de calendrier configurable. Les connexions
supplémentaires, les vrais brouillons en messagerie et la livraison automatique restent
à développer. [État des connexions et contrat d’extension](docs/snapshot-contract.md).

## Essayer sans API, clé ni coût

Depuis ce dossier, avec Python 3.9 ou plus :

```bash
python3 demo.py
python3 community_demo.py
python3 snapshot.py --input examples/neutral-snapshot.json --output output/neutral-analysis.json
python3 preview_recap.py --input output/neutral-analysis.json --output output/neutral-recap.json --timezone Europe/Paris
python3 -m unittest discover -s tests -v
```

Ouvrir `output/demo-recap.html` : aperçu de travail du futur récapitulatif Slack,
pas un dashboard destiné aux commerciaux. Douze opportunités fictives, regroupées par priorité,
avec les préparations consultables. `output/demo-recap.json` contient l’aperçu Slack.
Aucun envoi n’est effectué. La démonstration utilise une date fixe pour rester reproductible.
`output/neutral-recap.html` montre trois cas du format commun : un document promis,
un report demandé par l’acheteur et un rendez-vous déjà prévu. Aucun accès réseau requis.

Pour un fichier d’analyse sauvegardé :

```bash
python3 preview_recap.py --input output/analysis_adhoc.json --timezone Europe/Paris
```

Le rendu conserve tous les résultats. Les limites techniques Slack déterminent uniquement
le découpage en messages destinés à un même fil ; aucun deal n’est éliminé pour faire court.
Les résultats incertains, les erreurs et les collectes interrompues restent visibles.

## Choisir son rythme

La configuration utilise `"schedule": {"mode": "manual"}` par défaut : aucun horaire activé.
Pour définir un rythme, choisir `scheduled`, un fuseau IANA et les créneaux d’analyse et
de livraison. Chaque créneau contient `weekdays` (0 = lundi, 6 = dimanche) et `at` (HH:MM).
Tous les jours, certains jours ou plusieurs fois par jour : les créneaux sont au choix.

`examples/schedule.example.json` illustre deux passages par semaine ; ce n’est pas une
recommandation d’horaire. Pour vérifier les prochaines occurrences sans installer de tâche :

```bash
python3 scheduling.py --config examples/schedule.example.json
```

Les heures inexistantes au changement d’heure sont sautées ; une heure répétée est prise
une seule fois, à sa première occurrence. L’installation d’un ordonnanceur, le recontrôle
avant livraison et l’envoi Slack ne sont pas encore implémentés.

## Configurer une lecture HubSpot

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
cp config.example.json config.json
python3 probe.py --list-stages
```

Renseigner le jeton dans le `.env` de ce projet, puis le pipeline et ses étapes ouvertes
avec leurs IDs réels dans `config.json`. Par défaut, seul ce `.env` est chargé. L’option
explicite `--env-file` permet de choisir un autre fichier privé. Les métadonnées du CRM
valident les étapes ouvertes, indépendamment de leur nom.
Le parcours import JSON et les tests hors ligne utilisent seulement la bibliothèque
standard Python ; les dépendances API sont listées dans `requirements.txt`.

Options : `next_step_property`, `next_step_date_property`, `rules` et `stage_rules` pour
les champs et seuils ; `stage_context` pour expliquer le sens des étapes à la génération
IA sans imposer un cycle type ; `hubspot_portal_id` pour construire les liens vers les deals.
La date CRM doit être un timestamp UTC ou ISO avec fuseau, pas une date locale ambiguë.

```bash
# Toutes les opportunités ouvertes de ce responsable dans les étapes configurées
python3 analyze.py --owner-email rep@example.com

# Sélection explicite, sans plafond de nombre d’IDs
python3 analyze.py --deal-ids DEAL_ID_1,DEAL_ID_2

# Sélection depuis un JSON privé et stockage hors du dépôt
python3 analyze.py --deal-ids-file private-scope.json --output-dir ~/.local/share/open-pipeline-agent/run

# Suggestions IA optionnelles, pour tout le périmètre choisi
python3 analyze.py --owner-email rep@example.com --generate
```

`--generate` transmet des extraits CRM à Anthropic et consomme des tokens ; il
requiert `ANTHROPIC_API_KEY` et `ANTHROPIC_MODEL`. Aucun budget automatique n’est implémenté.
Les réponses IA sont contrôlées avant affichage : preuves citées, dates et actions existantes.
Une proposition non étayée devient un point à vérifier. Un contrôle réussi exige encore
une relecture humaine ; il ne garantit pas la justesse de l’interprétation commerciale.
`--only-empty-next-step` reste un filtre optionnel pour l’inventaire ciblé par responsable.

L’agent sauvegarde chaque résultat localement et marque les traitements incomplets.
Une limitation d’API ou une erreur doit être signalée, jamais dissimulée derrière un rapport
prétendument complet. Les sorties `output/`, la configuration et les secrets sont ignorés par Git.
Pour un pilote contenant des données réelles, utiliser `--output-dir` hors du dépôt. Le dossier
et les fichiers créés sont limités au compte local.

## Fichiers

| Fichier | Rôle |
|---|---|
| `analyze.py` | Collecte HubSpot et orchestration de l’analyse, sans écriture CRM |
| `snapshot.py` | Import et analyse d’un export CRM mappé au contrat commun |
| `signals.py` | Diagnostic et préparations locales à partir des faits |
| `recommendations.py` | Contrôle des citations, échéances et actions déjà prévues |
| `review.py` | Validation locale des propositions de votre assistant existant |
| `scheduling.py` | Validation du rythme et prochaines occurrences, sans installation |
| `preview_recap.py` | Rapport HTML et aperçus Slack complets |
| `demo.py` | Démonstration fictive sans réseau |
| `probe.py` | Inventaire des étapes et des champs prochaine action vides |
| `runtime.py` | Configuration locale et transport limité à la lecture |

## Construire ensemble

Priorités ouvertes : connecteurs Attio/Folk, attribution prudente des échanges liés à
l’entreprise, meilleurs briefs et préparations, sources documentaires facultatives,
livraison Slack choisie par l’utilisateur. Aucun ordre n’impose un CRM ou un cycle type.

Partagez un cas fictif et le comportement attendu avant d’ajouter une nouvelle règle.
Le [guide de contribution](CONTRIBUTING.md) explique comment proposer une amélioration,
tester un adaptateur et garder les données clients hors du dépôt.

[Contrôles des recommandations](docs/validation-recommandations.md) ·
[Guidelines générales](docs/guidelines.md) · [Format d’import commun](docs/snapshot-contract.md).
