---
title: Options de ligne de commande
description: Référence pour les options de démarrage et les paramètres d'exécution unique d'IaC Code.
---

# Options de ligne de commande

Les options de ligne de commande modifient le démarrage d'IaC Code. Utilisez-les avant d'entrer dans le REPL interactif, ou combinez-les avec `--prompt` pour une automatisation ponctuelle.

| Option | Objectif |
|---|---|
| `-h`, `--help` | Afficher l'aide CLI et quitter. Utilisez ceci pour inspecter les options prises en charge par votre version installée. |
| `-v`, `-V`, `--version` | Afficher la version installée d'IaC Code et quitter. |
| `-m <model>`, `--model <model>` | Démarrer avec un modèle LLM spécifique. Ceci remplace le modèle enregistré pour l'exécution en cours. |
| `-p <prompt>`, `--prompt <prompt>` | Exécuter un seul prompt et quitter. Ceci active le mode non interactif. Utilisez `--prompt -` pour lire le prompt depuis l'entrée standard. |
| `--output-format <format>` | Définir le format de sortie pour le mode non interactif. Les valeurs prises en charge sont `text`, `json` et `stream-json`. La valeur par défaut est `text`. |
| `--max-turns <number>` | Limiter le nombre maximum de tours de l'agent en mode non interactif. La valeur par défaut est `100`. |
| `-d`, `--debug` | Activer la journalisation de débogage pour l'exécution en cours. En mode interactif, utilisez `/debug` pour inspecter ou modifier la journalisation de débogage après le démarrage. |
| `-r <id-ou-nom-de-session>`, `--resume <id-ou-nom-de-session>` | Reprendre une session précédente par identifiant exact, préfixe d'identifiant unique ou nom de session unique. Les sessions résolues dans un autre projet affichent une commande `cd ... && iac-code --resume <id>` au lieu de basculer le projet courant à chaud. |
| `-c`, `--continue` | Reprendre la session la plus récente. Ne peut pas être utilisé avec `--resume`. |
| `--allowed-tools <patterns>` | Modèles de permissions d'outils séparés par des virgules à autoriser, ex. `'bash(git *),write_file'`. |
| `--disallowed-tools <patterns>` | Modèles de permissions d'outils séparés par des virgules à refuser, ex. `'bash(rm *)'`. |
| `--permission-mode <mode>` | Mode de permission : `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |

## Modes de permission

Le paramètre `--permission-mode` contrôle comment l'agent gère les vérifications de permissions des outils :

| Mode | Comportement |
|---|---|
| `default` | L'agent demande une confirmation lorsqu'une action d'outil nécessite une approbation. |
| `accept_edits` | Approuver automatiquement les commandes du système de fichiers considérées comme des modifications (ex. `mkdir`, `cp`). Les autres actions demandent toujours confirmation. |
| `bypass_permissions` | Approuver automatiquement toutes les actions d'outils sauf les vérifications de sécurité. Destiné à l'automatisation de confiance. |
| `dont_ask` | Refuser silencieusement toute action qui nécessiterait normalement une confirmation. Utile pour les exécutions strictement en lecture seule. |

## Commandes de démarrage courantes

Démarrer le REPL interactif avec le modèle enregistré :

```bash
iac-code
```

Démarrer avec un modèle spécifique pour cette exécution :

```bash
iac-code --model qwen3.6-plus
```

Exécuter un prompt unique :

```bash
iac-code --prompt "Create an OSS Bucket"
```

Lire le prompt depuis l'entrée standard :

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Reprendre la dernière session :

```bash
iac-code --continue
```

Autoriser uniquement les commandes git et bash en lecture seule :

```bash
iac-code --allowed-tools 'bash(git *)'
```

Exécuter en automatisation sans prompts interactifs :

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
