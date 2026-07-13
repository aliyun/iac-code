---
sidebar_position: 3
title: Outils, ressources, prompts et compétences
description: Comprendre comment les capacités MCP apparaissent dans IaC Code.
---

# Outils, ressources, prompts et compétences

Les MCP servers connectes peuvent exposer quatre types de capabilities a IaC Code.

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

Les descriptions d'outils et les schémas d'entrée JSON proviennent du serveur MCP. IaC Code transmet l'entrée de l'outil du modèle au serveur MCP, puis convertit les blocs de contenu MCP en un résultat d'outil normal.

Les invites d'autorisation et les métadonnées d'audit incluent le nom du serveur MCP, le nom de l'outil d'origine, le nom de l'outil normalisé public et les annotations en lecture seule/destructives.

Les annotations de l'outil MCP sont honorées dans la mesure du possible :

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | L'outil est traité comme étant en lecture seule et sécurisé pour la concurrence. |
| `destructiveHint: true` | L'outil est traité comme destructeur pour les décisions d'autorisation. |

Les outils MCP passent toujours par le système d'autorisation existant d'IaC Code. Configurez la politique d'autorisation avec des paramètres `permissions` normaux ou des indicateurs CLI tels que `--allowed-tools`, `--disallowed-tools` et `--permission-mode`.

Les notifications de progression MCP apparaissent dans le rendu interactif, la sortie de progression sans tête, les mises à jour de progression de l'outil ACP et les métadonnées de l'outil A2A.

## Tool Results and Artifacts

IaC Code convertit les blocs de contenu MCP en texte visible par le modèle :

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; le grand texte est enregistré comme artifact privé `.txt`, `.json` ou `.md`. |
| `structuredContent` | Rendu au format JSON dans une section de contenu structuré. |
| Ressources textuelles | Rendu avec le serveur et la provenance de l'URI. |
| `resource_link` | Rendu sous forme de lien de ressource avec URI et type MIME. |
| Données d'image, audio et blob | Stocké sous forme de fichiers d'artefacts privés et référencé par l'identifiant de l'artefact. |

Les artefacts binaires sont stockés dans le répertoire des résultats de l'outil MCP appartenant à la session pour les sessions v2 :

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

Les anciennes sessions sans marqueur de mise en page pris en charge continuent d'utiliser :

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. Les artifacts de grand texte incluent un path so the full output can be read without flooding the conversation.

## Resources

Lorsqu'un serveur connecté expose des ressources, IaC Code enregistre deux outils globaux :

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | Répertorie les ressources des serveurs MCP connectés. Filtrez éventuellement par nom de serveur. |
| `read_mcp_resource` | Lit une ressource par `server` et `uri`. |

Les lignes de ressources incluent le nom du serveur, l'URI, le nom de la ressource facultatif et le type MIME facultatif.

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

Lorsqu'il est invoqué, IaC Code appelle MCP `prompts/get`, restitue les messages d'invite renvoyés, injecte l'invite rendue dans la conversation et laisse le modèle continuer. Les arguments d'invite peuvent être transmis sous la forme :

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Les arguments d'invite requis sont validés avant l'appel MCP. Les valeurs entre guillemets sont prises en charge, y compris les chemins Windows avec des barres obliques inverses.

## Skills

Les ressources MCP avec les URI `skill://` deviennent des commandes de compétence :

```text
$mcp__<server>__<skill>
```

IaC Code lit la ressource de compétence distante, analyse le frontmatter et l'enregistre en tant que commande de compétence normale. Les compétences MCP à distance sont limitées en termes de sécurité :

- Remote `allowed_tools` are cleared.
- Les règles de chemin de déclenchement automatique à distance sont effacées.
- Le corps des compétences à distance et la longueur de la description sont limités.
- Si la compétence distante entre en conflit avec une commande existante, elle est ignorée avec un avertissement MCP.

Les ressources des compétences MCP peuvent être lues au démarrage afin que la commande puisse être enregistrée avant que l'utilisateur ne l'invoque.

Lorsqu'il n'y a pas de conflit de commandes, les compétences MCP reçoivent également un alias de compatibilité :

```text
$<server>:<skill>
```

Par exemple, `$mcp__yuque__search` et `$yuque:search` peuvent aboutir à la même compétence distante.

## Server Instructions (instructions serveur)

Si un serveur connecté renvoie des « instructions » lors de l'initialisation, IaC Code les injecte dans l'invite de l'agent en tant que section d'instructions du serveur MCP dédiée. Ces instructions sont traitées comme des conseils à l'échelle du serveur et ne remplacent pas les instructions du projet local.

## Elicitation (demandes interactives)

Les sessions interactives peuvent router les demandes MCP elicitation vers utilisateur. En mode URL, elicitation peut demander à utilisateur de terminer un flux URL externe, puis réessayer le MCP tool call original dans une limite bornée. Les contextes non interactifs annulent elicitation sans risque.

## Dynamic Updates

Si un serveur MCP envoie `tools/list_changed`, `resources/list_changed` ou `prompts/list_changed`, IaC Code actualise la liste des capacités affectées et met à jour le registre des outils ou des commandes. Les échecs d'actualisation sont signalés sous forme d'avertissements MCP et n'arrêtent pas la session active.
