---
sidebar_position: 5
title: Dépannage MCP
description: Diagnostiquer les problèmes de configuration, connexion, authentification et découverte de capacités MCP.
---

# Dépannage MCP

Les MCP warnings ne sont pas fatals sauf si toutes les capabilities dont vous avez besoin sont indisponibles. Un server en echec ne doit pas empecher les autres MCP servers ou les tools integres de IaC Code de fonctionner.

## Inspect Configuration

Inspectez les servers configures sans vous connecter:

```bash
iac-code mcp list
```

Executez des bounded health diagnostics pour les servers configures:

```bash
iac-code mcp list --check
```

Inspectez une configuration de serveur expurgée sans vous connecter :

```bash
iac-code mcp get my-server --scope local
```

Exécutez des diagnostics d’intégrité limités pour un serveur :

```bash
iac-code mcp get my-server --scope local --check
```

Inspectez la configuration explicitement, sans vous connecter :

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

Reconnectez un serveur ou tous les serveurs persistants :

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Symptome:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Correction:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

Utilisez le `--scope` exact indique par la liste de configuration. Pour un fichier persistant non standard, ajoutez
aussi le `--source-path` correspondant. Si le server a ete supprime, ajoutez-le de nouveau au lieu d'authentifier une configuration absente.

## Pending Project Server

Etat ou warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

ou démarrez le REPL interactif dans ce projet et répondez « y » lorsque vous y êtes invité. Appuyer sur Entrée signifie `N` et rejette le serveur.

Si l'approbation fonctionnait mais s'est arrêtée, vérifiez si `.mcp.json` a changé. L'approbation est liée à la signature de configuration.

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

Les serveurs pour lesquels les variables d'environnement requises sont manquantes sont ignorés.

## Connection Failed

Etat ou warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
- Utilisez des chemins absolus pour les scripts lors du lancement à partir de différents répertoires.
- Sous Windows, exécutez les serveurs basés sur Node via `cmd /c npx`.
- Vérifiez que toutes les variables d'environnement requises sont configurées.

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- Vérifiez que les en-têtes statiques sont présents et ne contiennent pas de secrets en texte brut.
- Exécutez `iac-code mcp auth <server>` si le serveur nécessite OAuth.

## Needs Authentication

Etat: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

Si le serveur utilise des jetons d'actualisation OAuth et qu'une réauthentification est requise, IaC Code efface les jetons obsolètes et demande un nouveau flux.

## OAuth Auth Failed

Symptome (`auth-failed`):

```text
MCP auth failed for 'name':
```

Le OAuth flow a demarre mais ne s'est pas termine proprement: le callback URL peut etre incomplet, le authorization code
peut avoir expire, ou le authorization server peut avoir renvoye une erreur. Si un nouveau flow echoue avant la fin,
IaC Code restaure le auth state precedent.

Correction:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

Reessayez d'abord `auth`. Utilisez `reset-auth` avant de reessayer seulement si le token enregistre ou le dynamic client state est obsolete.

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

Le code IaC efface le client OAuth stocké et l'état du jeton pour ce serveur. Exécutez à nouveau l'authentification :

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

Le serveur a demandé des étendues OAuth supplémentaires. Dans la session en cours, ouvrez `/mcp` et choisissez `S'authentifier` ou
`Se réauthentifier` pour ce serveur ; Le code IaC inclut les étendues signalées par le défi du serveur dans ce flux. Le
La commande autonome `iac-code mcp auth name` démarre un flux d'authentification normal et ne transporte pas les étendues de défi uniquement à partir d'un
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

Relancez avec la commande `--scope` exacte imprimee dans l'erreur. C'est une scope ambiguity: le server name est valide, mais la commande doit choisir un seul scope persistant.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

Le serveur s'est connecté, mais une liste de fonctionnalités a échoué. D'autres fonctionnalités du même serveur peuvent toujours fonctionner. Corrigez l’erreur côté serveur, puis redémarrez IaC Code ou déclenchez une actualisation de reconnexion/authentification.

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

En cas d'échecs répétés, vérifiez si le serveur distant a abandonné la session ou redémarré.

## Headers Helper Failed

Les symptômes peuvent inclure des erreurs d'analyse d'assistance, un délai d'attente, un état de sortie différent de zéro, un JSON non valide ou des valeurs d'en-tête autres que des chaînes. Vérifiez que la commande d'assistance est valide à partir du répertoire source de configuration et imprime un objet JSON tel que :

```json
{"X-Org": "platform"}
```

Le stderr de type secret est rédigé dans les diagnostics.

## WebSocket Config Rejected

Les serveurs WebSocket MCP prennent en charge la configuration URL uniquement. Supprimez `headers`, `headersHelper` et `oauth` des serveurs `type: "ws"`.

## Resources Are Missing

`list_mcp_resources` est enregistré uniquement lorsqu'au moins un serveur connecté expose des ressources. Si l'outil est manquant :

- Confirm the server connected.
- Confirmez que le serveur prend en charge `resources/list`.
- Vérifiez les avertissements de démarrage pour les erreurs de découverte de ressources.

## Prompt or Skill Command Missing

Les commandes d'invite et de compétence n'apparaissent qu'après une découverte réussie. Vérifiez :

- L'invite ou la ressource `skill://` existe sur le serveur MCP.
- Le nom de commande normalisé n'entre pas en conflit avec une commande intégrée.
- La ressource de compétence distante peut être lue dans le délai d'expiration du démarrage.
- La description des compétences et les limites de sécurité du code IaC.

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

Les artefacts binaires MCP issus des résultats de l'outil sont stockés dans le répertoire appartenant à la session pour les sessions v2 :

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

Sessions héritées sans utilisation de marqueur de mise en page pris en charge :

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Évitez de partager des répertoires de configuration, de journaux ou d'artefacts sans les examiner pour rechercher des secrets.
