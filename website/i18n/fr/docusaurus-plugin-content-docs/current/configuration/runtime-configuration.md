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
| history files | Historique de saisie pour les flux de travail interactifs |

Évitez de commiter ou de partager les fichiers de ce répertoire car ils peuvent contenir des secrets ou des préférences locales.

## Paramètres du projet

En plus du fichier `~/.iac-code/settings.yml` au niveau utilisateur, IaC Code charge les paramètres au niveau projet depuis le répertoire de travail courant :

| Fichier | Portée |
|---|---|
| `.iac-code/settings.yml` | Paramètres partagés du projet (sûr à commiter). |
| `.iac-code/settings.local.yml` | Surcharges locales (doit être dans .gitignore). |

Ordre de fusion : **paramètres utilisateur → paramètres projet → paramètres locaux projet → arguments CLI** (les sources ultérieures remplacent les précédentes).

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
