---
sidebar_position: 4
title: OAuth et sécurité
description: Authentifier les serveurs MCP distants et comprendre le modèle de sécurité MCP dans IaC Code.
---

# OAuth et sécurité

MCP peut démarrer des processus locaux et appeler des services distants. IaC Code traite donc la configuration et l'authentification de MCP comme étant sensibles à la sécurité.

## OAuth

Les servers distants `http` et `sse` peuvent utiliser OAuth. Les servers conformes aux standards qui publient OAuth metadata et prennent en charge Dynamic Client Registration ne demandent pas de client id fourni a l'avance. Ajoutez le server, puis lancez auth :

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Si un serveur nécessite un client pré-provisionné, configurez les métadonnées OAuth dans la configuration du serveur :

```json
{
  "mcpServers": {
    "secure-reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "iac-code",
        "clientSecretEnv": "MCP_CLIENT_SECRET",
        "callbackPort": 38487,
        "authServerMetadataUrl": "https://auth.example.com/.well-known/oauth-authorization-server"
      }
    }
  }
}
```

Supported OAuth fields:

| Field | Purpose |
|---|---|
| `clientId` | OAuth client id. |
| `clientSecretEnv` | Variable d'environnement qui contient le secret client. |
| `callbackPort` | Port de rappel de bouclage en option. Utilisez « 0 » ou omettez-le pour choisir un port libre. |
| `authServerMetadataUrl` | URL de métadonnées du serveur d'autorisation explicite facultative. |
| `clientMetadataUrl` | URL facultative du document de métadonnées du client HTTPS pour les serveurs d'autorisation qui prennent en charge les documents de métadonnées d'ID client. |

Le texte brut `oauth.clientSecret` est rejeté. Utilisez `clientSecretEnv` ou l'invite CLI sécurisée.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code ouvre ou imprime une URL d'autorisation et démarre un serveur de rappel de bouclage sur `127.0.0.1`. Si le navigateur ne peut pas s'ouvrir ou si le rappel ne peut pas se terminer automatiquement, collez l'URL de rappel ou le code d'autorisation dans l'invite CLI. Après autorisation, IaC Code échange le code contre des jetons et les stocke en toute sécurité.

Pour les serveurs compatibles DCR, IaC Code enregistre un client OAuth auprès du serveur et stocke l'ID client renvoyé et le secret client facultatif via le stockage secret MCP. L'échange et l'actualisation de jetons incluent le paramètre de ressource sélectionné par la sémantique du SDK MCP lorsque les métadonnées de ressources protégées l'exigent.

Si un serveur a besoin d'une authentification lors d'une session normale, IaC Code enregistre un outil d'authentification :

```text
mcp__<server>__authenticate
```

Le modèle peut appeler cet outil pour fournir à l'utilisateur l'URL OAuth. Une fois le flux terminé, IaC Code reconnecte le serveur MCP et actualise les fonctionnalités découvertes.

## Token Storage

IaC Code stocke les jetons OAuth et les secrets du client MCP via `MCPSecretStorage` :

1. Il essaie le trousseau de clés du système d'exploitation lorsqu'il est disponible.
2. Si le trousseau de clés est désactivé ou indisponible, il stocke les données de secours chiffrées sous `<config-dir>/mcp/`.
3. Les autorisations de fichiers sont limitées pour la clé de secours et le magasin de secrets chiffrés.

Définissez `IAC_CODE_MCP_DISABLE_KEYRING=1` pour forcer le stockage de secours chiffré, ce qui est utile pour les tests isolés.

Utilisez cette commande pour effacer l'état d'authentification stocké :

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` efface, pour le scope persistant selectionne, OAuth token state, dynamic client registration state,
le `client_id` stocke, le `client_secret` optionnel et OAuth signature index, tout en conservant le server config.
La suppression d'un server persistant effectue le meme auth-state cleanup avant de supprimer la configuration:

```bash
iac-code mcp remove secure-reviewer --scope user
```

Utilisez `reset-auth` pour reautoriser un server existant. Utilisez `mcp remove` lorsque le server config doit aussi
disparaitre; les deux chemins nettoient les entrees keyring et encrypted fallback entries gerees par `MCPSecretStorage`.

## Project Trust

Les fichiers du projet `.mcp.json` ne sont pas automatiquement approuvés car un référentiel peut ajouter un serveur `stdio` qui exécute du code local arbitraire. L'approbation interactive s'effectue par signature de configuration du serveur. La modification de la commande, des arguments, de l'environnement, de l'URL, des en-têtes ou de la configuration OAuth invalide l'approbation précédente.

Les modes serveur sans tête et de protocole ignorent les serveurs de projet non approuvés plutôt que les invites.

## Secret Handling

IaC Code protège les secrets de plusieurs manières :

- La sortie de configuration de `iac-code mcp get` et `iac-code mcp get --config-only` masque les clés qui ressemblent à des tokens, secrets, mots de passe, clés API ou en-têtes d'autorisation.
- Les valeurs sensibles d'en-têtes ou d'environnement en clair sont rejetées lors de l'ajout de serveurs via `iac-code mcp add` ou `mcp add-json`, sauf si elles utilisent une référence à une variable d'environnement. Les fichiers de configuration modifiés à la main ne sont pas revalidés au chargement ; évitez d'y stocker des secrets en clair.
- Les serveurs MCP stdio n'héritent que d'une allowlist de variables d'environnement sûres plus l'environnement explicite du serveur.
- Les variables proxy contenant un nom d'utilisateur ou un mot de passe ne sont pas héritées par les serveurs MCP stdio.
- Les commandes `headersHelper` s'exécutent sans shell, sans stdin, avec un environnement minimal, une capture stdout/stderr bornée et des diagnostics stderr privés masqués.
- Les fichiers d'artefact MCP sont écrits dans le répertoire privé de configuration runtime de IaC Code.

## Permissions

Les outils MCP utilisent le même cadre d'autorisation que les outils intégrés. Un serveur MCP distant ne peut pas contourner les contrôles d'autorisation du code IaC simplement en annonçant un outil. Gardez ces règles à l’esprit :

- Les outils MCP en lecture seule peuvent être automatiquement autorisés en fonction de la politique d'autorisation active.
- Les outils MCP destructeurs doivent nécessiter une approbation, sauf autorisation explicite.
- Dans l'automatisation sans tête, combinez `--permission-mode`, `--allowed-tools` et `--disallowed-tools` pour restreindre ce que les outils MCP peuvent faire.
- Les compétences MCP à distance n'accordent pas leurs propres `allowed_tools`.

## Fonctionnalités sensibles à la sécurité non prises en charge

IaC Code rejette ou omet intentionnellement ces fonctionnalités MCP pour le moment :

- Enterprise managed MCP policy.
- IDE and SDK transports.
- Headers WebSocket, `headersHelper` WebSocket et OAuth WebSocket.
- IaC Code acting as an MCP server.
