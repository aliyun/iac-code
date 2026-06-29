---
sidebar_position: 2
title: Configuration MCP
description: Configurez les serveurs MCP avec des commandes CLI, des fichiers settings, des fichiers projet et des sessions ACP.
---

# Configuration MCP

Les serveurs MCP sont configurés sous l'objet `mcpServers`. IaC Code prend en charge un schéma central compatible avec Claude Code pour les serveurs `stdio`, `http` et `sse`.

## Sources de configuration

IaC Code lit les serveurs MCP depuis ces sources :

| Source | Scope | Fichier ou point d'entrée | Modèle de confiance |
|---|---|---|---|
| Settings utilisateur | `user` | `~/.iac-code/settings.yml` ou `IAC_CODE_CONFIG_DIR/settings.yml` | Approuvé par l'utilisateur courant. |
| Settings locaux du projet | `local` | `<workspace>/.iac-code/settings.local.yml` | Privé au checkout local. |
| Fichier MCP de projet | `project` | `<workspace>/.mcp.json` | Partagé avec le projet et nécessite une approbation locale. |
| Configuration de session ACP | `session` | `mcp_servers` transmis par un client ACP | S'applique uniquement au runtime de cette session ACP. |

La priorité est user, project, local, puis session. Les sources plus tardives remplacent les précédentes par nom de serveur. Les configurations équivalentes sont aussi dédupliquées par signature de contenu.

Les fichiers `.mcp.json` de projet sont découverts depuis la racine du workspace jusqu'au répertoire courant. Les fichiers enfant remplacent les fichiers parent par nom de serveur.

## Commandes CLI

Utilisez `iac-code mcp` pour gérer la configuration MCP persistée :

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

Commandes disponibles :

| Commande | Usage |
|---|---|
| `iac-code mcp add` | Ajoute un serveur depuis des flags CLI structurés. |
| `iac-code mcp add-json` | Ajoute un serveur depuis un objet JSON. |
| `iac-code mcp list` | Liste les serveurs configurés, scopes, transports et états d'approbation. |
| `iac-code mcp get` | Affiche une configuration de serveur avec secrets masqués. |
| `iac-code mcp remove` | Supprime un serveur d'un scope persistant. |
| `iac-code mcp approve` | Approuve un serveur `.mcp.json` de projet. |
| `iac-code mcp reject` | Rejette un serveur `.mcp.json` de projet. |
| `iac-code mcp reset-project-choices` | Efface les choix d'approbation de projet enregistrés. |
| `iac-code mcp auth` | Démarre l'authentification OAuth pour un serveur. |
| `iac-code mcp reset-auth` | Supprime les tokens OAuth et le client secret enregistrés pour un serveur. |

Quand `--scope` est omis, IaC Code écrit dans `local` à l'intérieur d'un projet et dans `user` en dehors d'un projet.

## Serveurs Stdio

Les serveurs stdio lancent une commande locale :

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

Le champ `type` peut être omis quand `command` est présent. IaC Code transmet un environnement hérité sûr plus le `env` du serveur. Sous Windows, préférez `cmd /c npx` à `npx` seul pour les serveurs Node.

## Serveurs HTTP et SSE

Les serveurs distants nécessitent `type` et `url` :

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

Utilisez `type: "sse"` pour les serveurs SSE. Les headers statiques sont pris en charge. Les commandes dynamiques `headersHelper` sont rejetées, car elles exigent une conception séparée d'exécution de confiance.

## Expansion d'environnement

Les chaînes prennent en charge :

```text
${VAR}
${VAR:-default-value}
```

Les variables manquantes sans valeur par défaut produisent un avertissement MCP et le serveur concerné est ignoré. L'expansion s'applique récursivement aux chaînes dans les listes et objets.

Ne stockez pas de secrets en clair dans les headers ou les valeurs env. Utilisez des références à des variables d'environnement ou le stockage secret OAuth.

## Approbation de projet

Un `.mcp.json` de projet peut être commité dans un dépôt ; IaC Code ne lui fait donc pas confiance automatiquement.

Au démarrage du REPL interactif, IaC Code demande :

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Appuyer sur Entrée garde la valeur par défaut `N` et rejette cette configuration de serveur de projet. Tapez `y` ou `yes` pour l'approuver. L'approbation est stockée localement dans le répertoire de configuration IaC Code et inclut le chemin du workspace, le chemin du fichier projet, le nom du serveur et la signature de configuration. Si la configuration du serveur dans `.mcp.json` change, l'approbation est invalidée et le serveur redevient pending.

Les démarrages headless, ACP et A2A ne posent jamais de question d'approbation interactive. Les serveurs de projet non approuvés sont ignorés et signalés comme avertissements.
