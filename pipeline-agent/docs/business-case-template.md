# Un cas métier utile à la communauté

Pas besoin de code. Utiliser des personnes, entreprises, dates et échanges fictifs.
Ne pas joindre de capture CRM ou de données de client.

1. **Situation** : que vend l’équipe, et que signifie cette étape de son cycle ?
2. **Sources disponibles** : notes, emails, tâches, rendez-vous, prochaine action.
3. **Ce qui manque ou se contredit** : personne non liée au deal, note non datée,
   plusieurs opportunités sur un compte, action exécutée ailleurs.
4. **Ce que l’agent devrait préparer** : résultat concret pour le commercial.
5. **Ce qu’il devrait préserver** : report client, rendez-vous prévu, décision déjà prise.
6. **Au passage suivant** : qu’est-ce qui devrait faire réapparaître le deal ?

Exemple : « Le client attend notre questionnaire sécurité. La dernière réponse
date de deux semaines. Je veux voir le questionnaire à préparer et la promesse
source avant qu’on me propose de demander au client où il en est. »

Le mainteneur répond avec le comportement retenu et un cas exécutable. Copier
`examples/community-case-planned-meeting.json` dans `community-case-NOM.json`.
Les champs `expected_buckets` et `expected_action_kinds` constituent le résultat
attendu ; la suite les teste automatiquement.
