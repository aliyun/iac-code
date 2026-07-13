---
sidebar_position: 2
title: Configuration MCP
description: Configurer les serveurs MCP avec des commandes CLI, fichiers de paramètres, fichiers projet et sessions ACP.
---

# Configuration MCP

Les MCP servers sont configures sous le `mcpServers` object. IaC Code prend en charge un core schema compatible Claude Code pour `stdio`, `http`, `sse`, and URL-only `ws` servers.

## Demarrage rapide

Pour un serveur MCP HTTP distant tel que Yuque, ajoutez le serveur avec la forme URL positionnelle, puis lancez OAuth :

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Pour les wrappers stdio tels que `mcp-remote`, placez la commande subprocess apres `--` :

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

IaC Code lit les serveurs MCP à partir de ces sources :

| Source | Portée | Fichier ou point d'entrée | Modèle de confiance |
|---|---|---|---|
| Paramètres utilisateur | `user` | `~/.iac-code/settings.yml` ou `IAC_CODE_CONFIG_DIR/settings.yml` | Approuvé par l'utilisateur actuel. |
| Paramètres locaux du projet | `local` | `<workspace>/.iac-code/settings.local.yml` | Privé à la caisse locale. |
| Fichier MCP du projet | `project` | `<workspace>/.mcp.json` | Partagé avec le projet et nécessite une approbation locale. |
| Configuration de session ACP | `session` | `mcpServers` transmis par un client ACP | S'applique uniquement à l'exécution de cette session ACP. |

La priorité est l'utilisateur, le projet, le local, puis la session. Les sources ultérieures remplacent les sources antérieures par le nom du serveur. Les configurations équivalentes sont également dédupliquées par signature de contenu.

Les fichiers du projet `.mcp.json` sont découverts depuis la racine de l'espace de travail jusqu'au répertoire actuel. Les fichiers de projet enfants remplacent les fichiers parents par nom de serveur.

## CLI Commands

Utilisez `iac-code mcp` pour gérer la configuration MCP persistante :

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

Des serveurs HTTP distants peuvent être ajoutés avec le formulaire d'URL positionnelle de style Claude :

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Les serveurs SSE et WebSocket utilisent le même formulaire d'URL positionnelle avec leur propre transport :

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

Pour les wrappers stdio tels que `mcp-remote`, placez la commande subprocess après `--` :

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Commandes disponibles :

| Commande | Objectif |
|---|---|
| `iac-code mcp add` | Ajoutez un serveur à partir d'indicateurs CLI structurés. |
| `iac-code mcp add-json` | Ajoutez un serveur à partir d'un objet JSON. |
| `iac-code mcp list` | Liste les servers configurés, scopes, transports et état approbation sans connexion. |
| `iac-code mcp list --config-only` | Alias de la liste de configuration par défaut. |
| `iac-code mcp list --check` | Se connecte brièvement et affiche des diagnostics health bornés. |
| `iac-code mcp get` | Imprimez une configuration de serveur rédigée sans vous connecter. |
| `iac-code mcp get --config-only` | Imprimez une configuration de serveur rédigée sans vous connecter. |
| `iac-code mcp get --check` | Connectez-vous brièvement et affichez les diagnostics d’intégrité limités pour un serveur. |
| `iac-code mcp remove` | Supprimez un serveur d’une étendue persistante. |
| `iac-code mcp approve` | Approuver un projet serveur `.mcp.json`. |
| `iac-code mcp reject` | Rejeter un projet serveur `.mcp.json`. |
| `iac-code mcp reset-project-choices` | Effacez les choix d’approbation de projet stockés. |
| `iac-code mcp auth` | Démarrez l'authentification OAuth pour un serveur. |
| `iac-code mcp reset-auth` | Supprimez les jetons OAuth stockés et le secret client pour un serveur. |
| `iac-code mcp reconnect` | Reconnectez un serveur ou tous les serveurs persistants avec `--all`. |
| `iac-code mcp disable` | Désactivez un serveur persistant sans modifier la configuration du projet partagé. |
| `iac-code mcp enable` | Réactivez un serveur persistant. |

## Options de commande

Le jeu d'options ci-dessous suit `iac-code mcp <command> --help` :

| Commande | Options |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options ; seulement `--help`. |
| `iac-code mcp reject` | No command-specific options ; seulement `--help`. |
| `iac-code mcp reset-project-choices` | No command-specific options ; seulement `--help`. |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

Lorsque `--scope` est omis, le code IaC écrit dans `local` à l'intérieur d'un projet et dans `user` en dehors d'un projet.

Pour les commandes qui fonctionnent sur un serveur persistant existant, IaC Code peut trouver un serveur unique dans les étendues persistantes lorsque `--scope` est omis. Si le même nom existe dans plusieurs portées, la commande échoue avec les commandes `--scope` exactes pour lever l'ambiguïté.

## Gestionnaire MCP interactif

Dans le REPL interactif, `/mcp` ouvre un gestionnaire MCP en plein écran. Il regroupe les serveurs par source et affiche l'état de connexion, l'état d'authentification, les diagnostics de configuration, les détails d'échec et l'emplacement configuré.

Depuis le gestionnaire, vous pouvez inspecter les tools, resources et prompts d'un serveur connecté ; authentifier, ré-authentifier ou effacer l'authentification des serveurs distants ; reconnecter les serveurs ; activer ou désactiver les serveurs persistants ; approuver ou rejeter les serveurs `.mcp.json` de projet ; et supprimer les entrées persistantes. Les flux OAuth affichent l'URL d'autorisation, prennent en charge sa copie et acceptent une URL de callback ou un code d'autorisation collé lorsque la redirection du navigateur ne peut pas atteindre le listener callback local.

`/mcp enable <name>`, `/mcp disable <name>` et `/mcp reconnect <name>` exécutent des actions rapides sans ouvrir le gestionnaire. Si `/mcp` arrive par stdin redirigé ou une autre entrée non TTY, IaC Code affiche un message indiquant qu'un terminal est requis ; utilisez `iac-code mcp <command>` pour l'automatisation non interactive.

## Stdio Servers

Stdio servers launch a local command:

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

Le champ `type` peut être omis lorsque `command` est présent. Le code IaC transmet un environnement hérité sécurisé ainsi que le serveur `env`. Sous Windows, préférez `cmd /c npx` au lieu de `npx` nu pour les serveurs basés sur Node.

## HTTP and SSE Servers

Les serveurs distants nécessitent `type` et `url` :

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

Utilisez `type: "sse"` pour les serveurs SSE. Les en-têtes statiques sont pris en charge avec la syntaxe CLI `KEY=VALUE` ou `Name: Value`.

Des en-têtes dynamiques peuvent être fournis par `headersHelper` :

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

Le helper doit afficher un JSON object dont les clés et les valeurs sont des chaînes. Les en-têtes dynamiques remplacent les en-têtes statiques du même nom. IaC Code exécute les helpers sans shell, sans stdin, avec un environnement hérité minimal, le répertoire de la source de configuration comme cwd, un timeout de 5 secondes et des diagnostics stderr expurgés. La chaîne de commande `headersHelper` ne fait pas de substitution de variables environnement ; les variables référencées sont transmises dans environnement du helper, et le helper doit les lire lui-même. Les helpers dans project `.mcp.json` exigent une approbation du projet avant exécution.

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

Le transport WebSocket MCP SDK installé accepte uniquement une URL. IaC Code rejette les configurations WebSocket qui définissent également `headers`, `headersHelper` ou `oauth`.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Les variables manquantes sans valeur par défaut produisent un MCP warning et le server concerné est ignoré. La substitution environnementale est appliquée récursivement aux chaînes dans les listes et objets, sauf à la chaîne de commande `headersHelper`, qui reste littérale et reçoit les variables référencées via environnement du helper.

Ne stockez pas les secrets en texte brut dans les en-têtes ou les valeurs d'environnement. Utilisez des références de variables d'environnement ou un stockage secret OAuth.

## Project Approval

Le projet `.mcp.json` peut être validé dans un référentiel, donc IaC Code ne lui fait pas confiance automatiquement.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Appuyer sur Entrée conserve le `N` par défaut et rejette cette configuration exacte du serveur de projet. Tapez « y » ou « oui » pour l'approuver. L'approbation est stockée localement dans le répertoire de configuration IaC Code et inclut le chemin de l'espace de travail, le chemin du fichier de projet, le nom du serveur et la signature de configuration. Si la configuration du serveur `.mcp.json` change, l'approbation est invalidée et le serveur redevient en attente.

Les startups Headless, ACP et A2A ne posent jamais de questions d’approbation interactives. Les serveurs de projet en attente sont ignorés et signalés sous forme d'avertissements.

## Disabled Servers

`iac-code mcp disable <name>` stocke une entrée privée d'état désactivé dans le répertoire de configuration IaC Code. Pour les serveurs à l'échelle du projet, cela ne modifie pas le fichier `.mcp.json` partagé. Les entrées désactivées sont saisies par portée, fichier source, nom du serveur et signature de configuration, donc la modification de la configuration du serveur invalide l'état désactivé obsolète.
