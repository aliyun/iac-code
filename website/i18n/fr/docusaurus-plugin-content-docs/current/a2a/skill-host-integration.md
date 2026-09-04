---
sidebar_position: 3
title: Référence d'intégration hôte du Skill IaC Code
description: Intégrez le pont du Skill IaC Code à un agent hôte compatible.
---

# Référence d'intégration hôte du Skill IaC Code

Ce document s'adresse aux développeurs d'agents et de systèmes de distribution de Skills. Les utilisateurs doivent
consulter [Installer et utiliser le Skill IaC Code](./skill-integration.md).

## Modèle d'intégration et configuration

Le paquet contient `SKILL.md` et le pont `scripts/iac_code.py`, fondé uniquement sur la bibliothèque standard. Exécutez
le pont avec CPython 3.8 à 3.14. Considérez stdout comme le résultat JSON stable et stderr comme les diagnostics et la
progression. Conservez `jobId`, `contextId`, le cursor et les champs de corrélation. En cas d'erreur, n'utilisez ni un
autre Runtime ni un appel direct aux API cloud.

Le distributeur peut placer ce `config.json` à côté de `SKILL.md` :

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

Le pont préfixe `channel` par `skill/`. `pipelineName` vaut par défaut `selling_solution_first` ; `selling` est réservé à
un besoin explicite du flux historique. `null` signifie une attente illimitée. Les champs inconnus ou invalides sont
refusés. Cette politique d'installation ne doit pas être dérivée d'une demande utilisateur, exposée ou modifiée durant
une tâche.

## Démarrer et suivre une tâche

Écrivez la demande complète dans un fichier UTF-8 de l'espace de travail et utilisez un chemin absolu :

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

Utilisez `normal` par défaut et `pipeline` seulement pour le parcours de comparaison, confirmation et déploiement.
La langue peut être `en`, `zh`, `es`, `fr`, `de`, `ja`, `pt` ou `auto` ; conservez ensuite `preferredLanguage`.
`llm_not_configured` arrête avant la création et `cloud_credentials_not_configured` signale les identifiants manquants
en Pipeline.

`--follow` retourne au prochain seuil de présentation ou d'interaction, à `turn_completed`, ou à l'état terminal d'un
Pipeline. Avec `boundaryReached: true`, affichez toutes les chaînes de `userUpdates`, puis suivez le même job :

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

`boundaryReached` n'est pas une fin. `presentationRequired` impose d'afficher la mise à jour avant l'appel suivant.
En mode normal, utilisez `finalText` et `artifacts` à `turn_completed`. Pour un Pipeline terminal, utilisez
`pipelineResult` et `artifacts` et signalez tout échec de nettoyage. Pour le diagnostic ou la reprise uniquement :

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

Si l'état est `input-required` sans `inputRequired`, signalez le dernier texte ou l'erreur et laissez le job inchangé.

## Traiter les entrées utilisateur

Chaque `inputRequired` est une frontière stricte : affichez-la dans l'interface native de l'hôte et attendez une réponse
explicite. Ne déduisez jamais de valeur par défaut. Conservez `kind`, `inputId`, `requestTaskId`, `contextId` et, s'il
existe, `toolUseId`.

| `kind` | Informations à afficher | Réponse |
|---|---|---|
| `permission` | But, effet, cible, lecture seule, résumés de déploiement et sécurité, actions | `allow_once` / `deny` |
| `ask_user_question` | Question, choix et texte libre s'il est autorisé | Réponse |
| `candidate_selection` | Tous les résumés, diagrammes Mermaid, total mensuel et postes | ID ou numéro |
| `deployment_confirmation` | Solution, URL, devis, paramètres effectifs et modifiés, Preview, actions | `confirm` / `adjust` / `reselect` / `cancel` |

Écrivez la réponse corrélée dans un nouveau fichier JSON UTF-8 et reprenez le même job :

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

Omettez `parameterOverrides` sans ajustement. La demande initiale ou l'approbation de l'hôte ne vaut pas confirmation.

## Continuer, annuler et reprendre

Après un tour normal ou le passage d'un Pipeline terminé au mode normal, continuez le job existant :

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

Conservez `jobId` et `contextId` ; un nouveau `taskId` est normal. Cela permet aussi de reprendre après une attente
d'autorisation ou une interruption de l'hôte. Pour tout annuler :

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

Cette annulation diffère du refus d'une autorisation.

## Erreurs et Runtime

Une erreur avant création est définitive pour cet appel. Pour `incompatible_host`, affichez les informations de
compatibilité et arrêtez, sans basculer vers pip, un autre Runtime ou les API directes. Le Runtime est mis en cache dans
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`. Sa structure et son intégrité sont définies
par `skill-runtime/skill-package-contract.json` et le manifeste de version. Le nettoyage doit être demandé
explicitement ; les paquets courants ou actifs sont protégés.

Le Runtime utilise un port aléatoire de `127.0.0.1` et un Bearer token propre au processus. N'exposez pas le token,
l'état local, les identifiants, l'environnement ou les entrées/sorties brutes des outils.

## Documentation associée

- [Présentation des Skills IaC Code officiels](./skill-overview.md)
- [Installer et utiliser le Skill IaC Code](./skill-integration.md)
- [Présentation A2A](./overview.md)
- [Référence A2A](./protocol-reference.md)
