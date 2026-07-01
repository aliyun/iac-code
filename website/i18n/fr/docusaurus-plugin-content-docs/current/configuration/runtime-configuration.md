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

Vous pouvez le déplacer en définissant la variable d'environnement `IAC_CODE_CONFIG_DIR` (prend en charge l'expansion de `~` et `$VAR`). Une fois définie, tous les artefacts persistés — identifiants, paramètres, historique, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — suivent le nouvel emplacement. Les journaux de démarrage/débogage vont par défaut dans `<config-dir>/logs/` et peuvent être déplacés séparément avec `IAC_CODE_LOG_DIR` ; les enregistrements d'audit des permissions restent dans `<config-dir>/logs/`.

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
    thinkingEnabled: true
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingEnabled: false
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Champ | Portée | Description |
|---|---|---|
| `thinkingEnabled` | Fournisseur ou modèle | Interrupteur booléen optionnel pour le thinking. `true` demande aux fournisseurs/modèles compatibles de l'activer ; `false` demande de le désactiver ; si le champ est omis, le défaut du fournisseur/modèle est conservé. |
| `thinkingBudget` | Fournisseur ou modèle | Budget de reasoning/thinking sous forme d'entier positif, transmis aux fournisseurs qui le prennent en charge. |
| `maxCompletionTokens` | Fournisseur ou modèle | Valeur positive entière qui remplace `max_completion_tokens` pour les fournisseurs/modèles utilisant ce champ de requête. |
| `effort` | Fournisseur ou modèle | Surcharge optionnelle de l'effort de thinking, uniquement pour les modèles qui prennent en charge le contrôle d'effort. |

Les valeurs valides définies au niveau du modèle sous `providers.<provider>.models.<model>` remplacent les valeurs définies au niveau du fournisseur. Les valeurs numériques invalides sont ignorées ; IaC Code revient alors à la valeur du fournisseur ou à la politique intégrée du modèle.

Pour Alibaba Cloud DashScope et DashScope Token Plan, IaC Code définit une valeur intégrée `thinkingBudget=8192` pour `glm-5.2` et `kimi-k2.7-code`. Si `maxCompletionTokens` n'est pas défini, la limite de requête est calculée comme la limite normale de tokens de réponse plus le thinking budget effectif.

Les requêtes A2A peuvent remplacer ces réglages pour un seul tour de message via `message.metadata.iac_code.thinking` ou les flags `--thinking-enabled`, `--thinking-effort` et `--thinking-budget` de `iac-code a2a-client call`. Si aucune métadonnée de thinking A2A explicite n'est envoyée, le runtime utilise les réglages ci-dessus et les valeurs par défaut normales du provider. Pour les providers génériques `openai_compatible` avec une base URL en mode compatible DashScope, iac-code ne bascule vers le format wire natif de thinking DashScope que lorsqu'une politique de thinking explicite est présente.

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| Champ | Description |
|---|---|
| `mode` | Mode de permission : `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Liste des modèles de permissions d'outils à approuver automatiquement. |
| `deny` | Liste des modèles de permissions d'outils à refuser automatiquement. |
| `ask` | Liste des modèles de permissions d'outils nécessitant toujours une confirmation. |
| `additional_directories` | Répertoires supplémentaires au-delà de cwd dans lesquels l'agent peut écrire. |
| `audit` | Paramètres locaux du journal d'audit des permissions. |

### Syntaxe des modèles

Les modèles de permissions d'outils suivent le format `tool_name(rule)` :

| Modèle | Signification |
|---|---|
| `bash` | Correspondre à toutes les commandes bash (nom d'outil nu). |
| `bash(git *)` | Correspondre aux commandes bash commençant par `git`. |
| `bash(curl:*)` | Correspondre aux commandes bash commençant par `curl`. |
| `write_file` | Correspondre à tous les appels d'outil write_file. |
| `aliyun_api(ros:CreateStack)` | Correspondre à une paire produit/action d'API Alibaba Cloud. |

Les règles sont évaluées dans l'ordre : **deny → ask → allow → comportement par défaut**. Les arguments CLI (`--allowed-tools`, `--disallowed-tools`) ont la priorité la plus élevée.

### Permissions d'API Alibaba Cloud

`aliyun_api` distingue les appels d'API en lecture seule des appels qui peuvent modifier des ressources cloud. Les actions d'API en lecture seule sont autorisées automatiquement. Les appels d'API qui ne sont pas en lecture seule nécessitent une confirmation ou une règle allow exacte pour ce produit/action, par exemple :

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

Une règle allow nue `aliyun_api` n'approuve pas globalement les API d'écriture Alibaba Cloud. Hors `bypass_permissions`, les règles allow d'écriture doivent correspondre exactement à la paire canonique `product:action`. En mode `bypass_permissions`, les API d'écriture Alibaba Cloud protégées sont approuvées automatiquement, mais toute décision allow nécessitant un enregistrement d'audit échoue toujours en mode fail-closed si la persistance de l'audit échoue. Les jokers peuvent toujours servir aux règles deny ou ask, ainsi qu'aux correspondances de règles en lecture seule.

Les requêtes de style ROA sont traitées comme lecture seule uniquement lorsque la méthode est `GET` et que la requête n'a pas de body. Les requêtes ROA qui ne sont pas en lecture seule suivent la même exigence de règle allow canonique exacte `product:action` que les API d'écriture de style RPC : une règle exacte comme `aliyun_api(cs:CreateCluster)` peut approuver l'écriture, tandis que les règles allow avec jokers n'approuvent toujours pas les appels qui ne sont pas en lecture seule.

### Journal d'audit des permissions

Les décisions de permissions qui traversent des prompts utilisateur, des frontières de cache d'outils, une approbation d'automatisation ou une approbation de resolver sont ajoutées à :

```text
<config-dir>/logs/permission-audit.jsonl
```

Par défaut, ce chemin est `~/.iac-code/logs/permission-audit.jsonl`. Le journal d'audit des permissions suit `IAC_CODE_CONFIG_DIR` ; `IAC_CODE_LOG_DIR` ne déplace que les journaux de démarrage/débogage. Le writer d'audit ajoute des enregistrements JSONL avec verrouillage de fichier, effectue la rotation du fichier et restreint les permissions locales lorsque le système d'exploitation le permet. Les autorisations automatiques routinières en lecture seule peuvent être omises, mais les refus, prompts, décisions mises en cache, approbations d'automatisation, approbations de resolver et autres frontières de permissions auditées sont enregistrés.

Les paramètres d'audit se configurent sous `permissions.audit` :

| Champ | Défaut | Description |
|---|---:|---|
| `include_tool_input` | `false` | Inclure dans les enregistrements d'audit JSONL une entrée d'outil sous forme uniquement. Les chaînes sont stockées sous forme de type, longueur et empreinte ; les clés ressemblant à des secrets sont expurgées ; les noms de champs hors liste blanche peuvent être représentés par une empreinte ; les chaînes de payload métier brutes ne sont pas écrites. Les entrées d'API Alibaba Cloud conservent aussi un résumé d'opération sûr. |
| `max_file_bytes` | `10485760` | Faire tourner `permission-audit.jsonl` lorsqu'il dépasse cette taille. |
| `max_files` | `5` | Nombre de fichiers d'audit tournés à conserver. Les valeurs au-dessus du maximum intégré sont plafonnées. |

Si une décision allow nécessitant un enregistrement d'audit ne peut pas être persistée dans le journal d'audit, IaC Code échoue en mode fail-closed et refuse l'action au lieu de l'exécuter sans trace d'audit.
