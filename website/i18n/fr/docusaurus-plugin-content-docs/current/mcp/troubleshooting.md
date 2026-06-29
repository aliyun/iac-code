---
sidebar_position: 5
title: Dépannage MCP
description: Diagnostiquez les problèmes de configuration, connexion, authentification et découverte de capacités MCP.
---

# Dépannage MCP

Les avertissements MCP ne sont pas fatals sauf si toutes les capacités dont vous avez besoin sont indisponibles. Un serveur défaillant ne doit pas empêcher les autres serveurs MCP ou les outils intégrés d'IaC Code de fonctionner.

## Inspecter la configuration

Lister les serveurs configurés :

```bash
iac-code mcp list
```

Inspecter une configuration de serveur masquée :

```bash
iac-code mcp get my-server --scope local
```

Supprimer un mauvais serveur :

```bash
iac-code mcp remove my-server --scope local
```

Effacer les choix d'approbation de projet :

```bash
iac-code mcp reset-project-choices
```

## Serveur de projet en attente

Symptôme :

```text
Project MCP server 'name' is pending approval.
```

Correction :

```bash
iac-code mcp approve name
```

ou démarrez le REPL interactif dans ce projet et répondez `y` à la question. Appuyer sur Entrée signifie `N` et rejette le serveur.

Si l'approbation fonctionnait puis a cessé de fonctionner, vérifiez si `.mcp.json` a changé. L'approbation est liée à la signature de configuration.

## Variable d'environnement manquante

Symptôme :

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Corrigez avec l'une de ces options :

```bash
export TOKEN=...
```

ou utilisez une valeur par défaut :

```json
"Authorization": "${TOKEN:-}"
```

Les serveurs avec des variables d'environnement requises manquantes sont ignorés.

## Échec de connexion

Pour les serveurs stdio :

- Vérifiez que `command` existe sur le `PATH`.
- Utilisez des chemins absolus pour les scripts quand le lancement se fait depuis plusieurs répertoires.
- Sous Windows, lancez les serveurs Node avec `cmd /c npx`.
- Vérifiez que toutes les variables d'environnement requises sont configurées.

Pour les serveurs HTTP ou SSE :

- Vérifiez l'URL et le type de transport.
- Vérifiez TLS et les paramètres proxy.
- Confirmez que les headers statiques sont présents et ne contiennent pas de secrets en clair.
- Exécutez `iac-code mcp auth <server>` si le serveur exige OAuth.

## Authentification requise

Symptôme :

```text
MCP server 'name' requires authentication.
```

Correction :

```bash
iac-code mcp auth name --scope user
```

Si le serveur utilise des refresh tokens OAuth et exige une réauthentification, IaC Code efface les tokens obsolètes et demande un nouveau flux.

## Échec de découverte de capacité

Les symptômes peuvent inclure :

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

Le serveur est connecté, mais une liste de capacités a échoué. D'autres capacités du même serveur peuvent encore fonctionner. Corrigez l'erreur côté serveur, puis redémarrez IaC Code ou déclenchez un reconnect/auth refresh.

## Ressources manquantes

`list_mcp_resources` est enregistré seulement lorsqu'au moins un serveur connecté expose des ressources. Si l'outil manque :

- Confirmez que le serveur est connecté.
- Confirmez que le serveur prend en charge `resources/list`.
- Vérifiez les avertissements de démarrage pour des erreurs de discovery de ressources.

## Commande prompt ou compétence manquante

Les commandes de prompt et de compétence apparaissent seulement après une discovery réussie. Vérifiez :

- Le prompt ou la ressource `skill://` existe sur le serveur MCP.
- Le nom de commande normalisé n'entre pas en conflit avec une commande intégrée.
- La ressource de compétence distante peut être lue avant le timeout de démarrage.
- La description et le corps de compétence respectent les limites de sécurité d'IaC Code.

## Logs et artefacts

Les logs runtime se trouvent par défaut dans :

```text
<config-dir>/logs/
```

ou dans `IAC_CODE_LOG_DIR` si défini.

Les artefacts binaires produits par les résultats d'outils MCP sont stockés sous :

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Évitez de partager les répertoires config, log ou artefact sans vérifier qu'ils ne contiennent pas de secrets.
