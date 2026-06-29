---
title: Configuration
description: Ordre de configuration à l'exécution et fichiers locaux.
---

# Configuration

IaC Code lit la configuration depuis les arguments CLI, les variables d'environnement et les fichiers dans le répertoire de configuration à l'exécution.

Priorité de configuration :

```text
Arguments CLI > variables d'environnement > fichiers de configuration
```

Le répertoire d'exécution par défaut est :

```text
~/.iac-code/
```

Vous pouvez le déplacer en définissant la variable d'environnement `IAC_CODE_CONFIG_DIR` (prend en charge l'expansion de `~` et `$VAR`). Une fois définie, tous les artefacts persistés — identifiants, paramètres, historique, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — suivent le nouvel emplacement.

Fichiers courants :

| Fichier | Description |
|---|---|
| `.credentials.yml` | Identifiants LLM |
| `.cloud-credentials.yml` | Identifiants du fournisseur cloud |
| `settings.yml` | Fournisseur sélectionné, modèle et paramètres associés |
| `AGENTS.md` | Mémoire utilisateur chargée comme instructions persistantes |
| history files | Historique de saisie pour les flux de travail interactifs |

Évitez de commiter ou de partager les fichiers de ce répertoire car ils peuvent contenir des secrets ou des préférences locales.

## Fichiers mémoire

IaC Code possède deux emplacements mémoire publics :

| Emplacement | Objectif |
|---|---|
| `<project-root>/AGENTS.md` | Mémoire de projet. Elle peut être commitée lorsque les instructions sont utiles à toutes les personnes travaillant sur le projet. |
| `<config-dir>/AGENTS.md` | Mémoire utilisateur. Elle suit `IAC_CODE_CONFIG_DIR` et reste privée pour l'utilisateur local. |

Définissez `IAC_CODE_INSTRUCTION_MEMORY_FILE` pour utiliser un autre nom de fichier de mémoire d'instructions, par exemple `IAC-CODE.md`.

Les fichiers de sujets auto-memory du projet sont stockés sous :

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` dans ce dossier est l'index des sujets utilisé par les side calls auto-memory. Il n'est pas chargé comme contexte permanent. Lorsque auto-memory est activé, IaC Code peut sélectionner des fichiers de sujets pertinents et les ajouter comme contexte de conversation masqué.

## Paramètres du projet

En plus du fichier `~/.iac-code/settings.yml` au niveau utilisateur, IaC Code charge les paramètres au niveau projet depuis le répertoire de travail courant :

| Fichier | Portée |
|---|---|
| `.iac-code/settings.yml` | Paramètres partagés du projet (sûr à commiter). |
| `.iac-code/settings.local.yml` | Surcharges locales (doit être dans .gitignore). |

Ordre de fusion : **paramètres utilisateur → paramètres projet → paramètres locaux projet → arguments CLI** (les sources ultérieures remplacent les précédentes).

## Politique de requête du fournisseur

Les entrées de fournisseur dans `settings.yml` peuvent contenir des champs de politique de requête pour les fournisseurs compatibles OpenAI. Ces réglages sont utiles lorsqu'un modèle distingue les tokens de réponse visibles des tokens de reasoning/thinking.

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Champ | Portée | Description |
|---|---|---|
| `thinkingBudget` | Fournisseur ou modèle | Budget de reasoning/thinking sous forme d'entier positif, transmis aux fournisseurs qui le prennent en charge. |
| `maxCompletionTokens` | Fournisseur ou modèle | Valeur positive entière qui remplace `max_completion_tokens` pour les fournisseurs/modèles utilisant ce champ de requête. |
| `effort` | Fournisseur ou modèle | Surcharge optionnelle de l'effort de thinking, uniquement pour les modèles qui prennent en charge le contrôle d'effort. |

Les valeurs valides définies au niveau du modèle sous `providers.<provider>.models.<model>` remplacent les valeurs définies au niveau du fournisseur. Les valeurs numériques invalides sont ignorées ; IaC Code revient alors à la valeur du fournisseur ou à la politique intégrée du modèle.

Pour Alibaba Cloud DashScope et DashScope Token Plan, IaC Code définit une valeur intégrée `thinkingBudget=8192` pour `glm-5.2` et `kimi-k2.7-code`. Si `maxCompletionTokens` n'est pas défini, la limite de requête est calculée comme la limite normale de tokens de réponse plus le thinking budget effectif.

## Configuration des permissions d'outils

La section `permissions` dans `settings.yml` configure quelles actions d'outils sont autorisées, refusées ou nécessitent une confirmation :

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Champ | Description |
|---|---|
| `mode` | Mode de permission : `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Liste des modèles de permissions d'outils à approuver automatiquement. |
| `deny` | Liste des modèles de permissions d'outils à refuser automatiquement. |
| `ask` | Liste des modèles de permissions d'outils nécessitant toujours une confirmation. |
| `additional_directories` | Répertoires supplémentaires au-delà de cwd dans lesquels l'agent peut écrire. |

### Syntaxe des modèles

Les modèles de permissions d'outils suivent le format `tool_name(rule)` :

| Modèle | Signification |
|---|---|
| `bash` | Correspondre à toutes les commandes bash (nom d'outil nu). |
| `bash(git *)` | Correspondre aux commandes bash commençant par `git`. |
| `bash(curl:*)` | Correspondre aux commandes bash commençant par `curl`. |
| `write_file` | Correspondre à tous les appels d'outil write_file. |

Les règles sont évaluées dans l'ordre : **deny → ask → allow → comportement par défaut**. Les arguments CLI (`--allowed-tools`, `--disallowed-tools`) ont la priorité la plus élevée.
