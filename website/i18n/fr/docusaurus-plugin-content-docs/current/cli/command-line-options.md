---
title: Options de ligne de commande
description: Référence pour les options de démarrage et les paramètres d'exécution unique d'IaC Code.
---

# Options de ligne de commande

La commande principale démarre le web par défaut. Les options ci-dessous concernent le démarrage web, l’exécution headless ponctuelle et les sous-processus stream-json. Configurez les conversations interactives dans Web ou bureau.

La colonne d’entrée indique où chaque option est acceptée. `headless` désigne `iac-code --prompt ...` ; `process` désigne le mode sous-processus stream-json. Le modèle et les permissions de Web se règlent dans l’interface.

| Option | Objectif | Entrée |
|---|---|---|
| `-h`, `--help` | Afficher l'aide CLI et quitter. Utilisez ceci pour inspecter les options prises en charge par votre version installée. | `iac-code`, `web` |
| `-v`, `-V`, `--version` | Afficher la version installée d'IaC Code et quitter. | `iac-code` |
| `-m <model>`, `--model <model>` | Démarrer avec un modèle LLM spécifique. Ceci remplace le modèle enregistré pour l'exécution en cours. | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | Exécuter un seul prompt et quitter. Ceci active le mode non interactif. Utilisez `--prompt -` pour lire le prompt depuis l'entrée standard. | `iac-code` |
| `--output-format <format>` | Définir le format de sortie pour le mode non interactif. Les valeurs prises en charge sont `text`, `json` et `stream-json`. La valeur par défaut est `text`. | `headless`, `process` |
| `--max-turns <number>` | Limiter le nombre maximum de tours de l'agent en mode non interactif. La valeur par défaut est `100`. | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | Contrôler si les requêtes non interactives ponctuelles activent explicitement le thinking. La valeur par défaut est `--thinking-enabled` ; utilisez `--no-thinking-enabled` pour envoyer `thinking_enabled=false` pour cette exécution sans réécrire `settings.yml`. | `headless` |
| `-d`, `--debug` | Activer les journaux de débogage pour cette exécution. | `headless`, `process`, `web` |
| `--verbose` | Afficher la progression du mode headless (non interactif) sur la sortie d'erreur standard. | `headless` |
| `-r <id-ou-nom-de-session>`, `--resume <id-ou-nom-de-session>` | Reprendre par ID exact, préfixe unique ou nom unique. Exécutez les commandes headless depuis le répertoire du projet de la session. | `headless` |
| `-c`, `--continue` | Reprendre la session la plus récente. Ne peut pas être utilisé avec `--resume`. | `headless` |
| `--allowed-tools <patterns>` | Modèles de permissions d'outils séparés par des virgules à autoriser, ex. `'bash(git *),write_file'`. | `headless`, `process` |
| `--disallowed-tools <patterns>` | Modèles de permissions d'outils séparés par des virgules à refuser, ex. `'bash(rm *)'`. | `headless`, `process` |
| `--permission-mode <mode>` | Mode de permission : `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. | `headless`, `process` |

## Modes de permission

Le paramètre `--permission-mode` contrôle comment l'agent gère les vérifications de permissions des outils :

| Mode | Comportement |
|---|---|
| `default` | L'agent demande une confirmation lorsqu'une action d'outil nécessite une approbation. |
| `accept_edits` | Approuver automatiquement les commandes du système de fichiers considérées comme des modifications (ex. `mkdir`, `cp`). Les autres actions demandent toujours confirmation. |
| `bypass_permissions` | Approuver automatiquement les actions d'outils sauf les vérifications de sécurité. Toute décision allow nécessitant un enregistrement d'audit échoue en mode fail-closed si la persistance de l'audit échoue. Destiné à l'automatisation de confiance. |
| `dont_ask` | Refuser les actions nécessitant une approbation, sauf si une règle d’autorisation explicite s’applique. |

Les appels d'API d'écriture Alibaba Cloud effectués via `aliyun_api` sont protégés séparément : une règle allow nue `aliyun_api` ne les approuve pas globalement, et hors `bypass_permissions`, les règles allow d'écriture doivent correspondre exactement à la paire canonique `product:action`. `bypass_permissions` les approuve automatiquement, mais comme les autres décisions allow auditées, l'action est refusée si l'enregistrement d'audit des permissions ne peut pas être persisté. Utilisez des règles exactes comme `aliyun_api(ros:CreateStack)` lorsque l'automatisation de confiance ne doit autoriser qu'une API d'écriture précise.

## Commandes de démarrage courantes

Démarrer l’application web locale :

```bash
iac-code
```

Démarrer avec un modèle spécifique pour cette exécution :

```bash
iac-code --prompt "Créer un VPC" --model qwen3.7-max
```

Exécuter un prompt unique :

```bash
iac-code --prompt "Create an OSS Bucket"
```

Exécuter un prompt unique avec le thinking désactivé pour cette requête :

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

Lire le prompt depuis l'entrée standard :

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Continuer la dernière session avec un prompt unique :

```bash
iac-code --continue --prompt "Générer un modèle VPC"
```

Préautoriser les commandes bash correspondant à `git *` :

```bash
iac-code --prompt "Afficher l’état de git" --allowed-tools 'bash(git *)'
```

Préautoriser une API d’écriture Alibaba Cloud précise :

```bash
iac-code --prompt "Déployer la pile" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Exécuter en automatisation sans prompts interactifs :

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
