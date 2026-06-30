---
sidebar_position: 3
title: Outils, ressources, prompts et compétences
description: Comprendre comment les capacités MCP apparaissent dans IaC Code.
---

# Outils, ressources, prompts et compétences

Les serveurs MCP connectés peuvent exposer quatre types de capacités à IaC Code.

## Outils

Chaque outil MCP devient un outil IaC Code :

```text
mcp__<server>__<tool>
```

La description de l'outil et le schéma JSON d'entrée viennent du serveur MCP. IaC Code transmet l'entrée d'outil du modèle au serveur MCP, puis convertit les blocs de contenu MCP en résultat d'outil normal.

Les annotations MCP sont respectées quand c'est possible :

| Annotation MCP | Comportement IaC Code |
|---|---|
| `readOnlyHint: true` | L'outil est traité comme lecture seule et sûr pour la concurrence. |
| `destructiveHint: true` | L'outil est traité comme destructif pour les décisions d'autorisation. |

Les outils MCP passent toujours par le système d'autorisations existant de IaC Code. Configurez la politique avec les settings `permissions` ou les flags CLI comme `--allowed-tools`, `--disallowed-tools` et `--permission-mode`.

Les notifications de progression MCP sont exposées dans le rendu interactif, la sortie de progression headless, les mises à jour de progression ACP et les métadonnées d'outil A2A.

## Résultats d'outils et artefacts

IaC Code convertit les blocs de contenu MCP en texte visible par le modèle :

| Contenu MCP | Résultat IaC Code |
|---|---|
| Contenu texte | Inclus directement dans le résultat d'outil. |
| `structuredContent` | Rendu comme JSON formaté dans une section de contenu structuré. |
| Ressources texte | Rendues avec la provenance du serveur et de l'URI. |
| `resource_link` | Rendu comme lien de ressource avec URI et type MIME. |
| Images, audio et blobs | Stockés comme fichiers d'artefact privés et référencés par id d'artefact. |

Les artefacts binaires sont stockés sous :

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

Le modèle voit l'id d'artefact et les métadonnées, pas les données base64 brutes.

## Ressources

Lorsqu'au moins un serveur connecté expose des ressources, IaC Code enregistre deux outils globaux :

| Outil | Usage |
|---|---|
| `list_mcp_resources` | Liste les ressources des serveurs MCP connectés. Filtrage optionnel par nom de serveur. |
| `read_mcp_resource` | Lit une ressource avec `server` et `uri`. |

Les lignes de ressource incluent le nom du serveur, l'URI, un nom de ressource optionnel et un type MIME optionnel.

## Prompts

Les prompts MCP deviennent des commandes slash :

```text
/mcp__<server>__<prompt> key=value
```

À l'invocation, IaC Code appelle MCP `prompts/get`, rend les messages de prompt retournés, injecte le prompt rendu dans la conversation et laisse le modèle continuer. Les arguments peuvent être passés ainsi :

```text
template_name=prod-vpc region=cn-hangzhou
```

ou en JSON :

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Les arguments obligatoires sont validés avant l'appel MCP. Les valeurs entre guillemets sont prises en charge, y compris les chemins Windows avec antislashs.

## Compétences

Les ressources MCP avec des URI `skill://` deviennent des commandes de compétence :

```text
$mcp__<server>__<skill>
```

IaC Code lit la ressource de compétence distante, analyse le frontmatter et l'enregistre comme commande de compétence normale. Les compétences MCP distantes sont limitées pour la sécurité :

- Les `allowed_tools` distants sont effacés.
- Les règles d'auto-activation par chemins distants sont effacées.
- Le corps et la description de compétence distante sont bornés en longueur.
- Si la compétence distante entre en conflit avec une commande existante, elle est ignorée avec un avertissement MCP.

Les ressources de compétence MCP peuvent être lues au démarrage pour que la commande soit enregistrée avant son invocation par l'utilisateur.

## Mises à jour dynamiques

Si un serveur MCP envoie `tools/list_changed`, `resources/list_changed` ou `prompts/list_changed`, IaC Code rafraîchit la liste de capacités concernée et met à jour le registre d'outils ou de commandes. Les échecs de rafraîchissement sont signalés comme avertissements MCP et n'arrêtent pas la session active.
