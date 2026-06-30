---
title: Mode non interactif
description: Exécuter des prompts uniques depuis des arguments ou stdin.
---

# Mode non interactif

Le mode non interactif exécute un seul prompt et quitte. Utilisez-le quand vous souhaitez qu'IaC Code produise une sortie pour une tâche répétable sans rester dans le REPL.

Utilisez `--prompt` pour passer le prompt directement :

```bash
iac-code --prompt "Create an OSS Bucket"
```

Utilisez `--prompt -` pour lire le prompt depuis l'entrée standard :

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Utilisez `--output-format` quand l'appelant a besoin d'une sortie structurée :

```bash
iac-code --prompt "Create an OSS Bucket" --output-format json
```

Utilisez `--max-turns` pour limiter la durée de travail de l'agent :

```bash
iac-code --prompt "Create a VPC" --max-turns 20
```

Les formats de sortie pris en charge sont :

| Format | Objectif |
|---|---|
| `text` | Sortie lisible par l'homme. C'est la valeur par défaut. |
| `json` | Un seul résultat JSON pour les appelants qui analysent la réponse finale. |
| `stream-json` | Événements JSON en streaming pour les appelants qui traitent la progression incrémentale. |

## Contrôle des permissions en automatisation

Lors de l'exécution en mode non interactif, utilisez `--permission-mode` pour contrôler comment l'agent gère les approbations d'outils :

```bash
iac-code --prompt "Deploy the stack" --permission-mode bypass_permissions
```

En `bypass_permissions`, les actions d'outils sont approuvées automatiquement sauf les vérifications de sécurité, mais toute décision allow nécessitant un enregistrement d'audit continue d'échouer en mode fail-closed si la persistance de l'audit échoue. Les API d'écriture Alibaba Cloud restent protégées séparément hors `bypass_permissions` ; pour une automatisation de confiance plus restreinte, n'utilisez pas `bypass_permissions` et autorisez explicitement chaque API d'écriture requise :

```bash
iac-code --prompt "Deploy the stack" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

Pour restreindre ce que l'agent peut faire, combinez `--allowed-tools` et `--disallowed-tools` :

```bash
iac-code --prompt "Check the stack status" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

Pour tous les paramètres de démarrage, consultez [Options de ligne de commande](../cli/command-line-options.md).
