---
sidebar_position: 4
title: OAuth et sécurité
description: Authentifiez les serveurs MCP distants et comprenez le modèle de sécurité MCP dans IaC Code.
---

# OAuth et sécurité

MCP peut démarrer des processus locaux et appeler des services distants. IaC Code traite donc la configuration MCP et l'authentification comme sensibles.

## OAuth

Les serveurs distants `http` et `sse` peuvent utiliser OAuth. Configurez les métadonnées OAuth dans la configuration du serveur :

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

Champs OAuth pris en charge :

| Champ | Usage |
|---|---|
| `clientId` | Identifiant client OAuth. |
| `clientSecretEnv` | Variable d'environnement contenant le secret client. |
| `callbackPort` | Port de callback loopback optionnel. Utilisez `0` ou omettez-le pour choisir un port libre. |
| `authServerMetadataUrl` | URL optionnelle explicite des métadonnées du serveur d'autorisation. |

`oauth.clientSecret` en clair est rejeté. Utilisez `clientSecretEnv` ou le prompt CLI sécurisé.

## Authentification

Exécutez :

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code ouvre ou affiche une URL d'autorisation et démarre un serveur de callback loopback sur `127.0.0.1`. Après la redirection du fournisseur avec un code d'autorisation, IaC Code l'échange contre des tokens et les stocke de façon sécurisée.

Si un serveur a besoin d'authentification pendant une session normale, IaC Code enregistre un outil d'authentification :

```text
mcp__<server>__authenticate
```

Le modèle peut appeler cet outil pour fournir l'URL OAuth à l'utilisateur. Une fois le flux terminé, IaC Code reconnecte le serveur MCP et rafraîchit les capacités découvertes.

## Stockage des tokens

IaC Code stocke les tokens OAuth et les secrets client MCP via `MCPSecretStorage` :

1. Il essaie d'abord le keyring du système d'exploitation quand il est disponible.
2. Si le keyring est désactivé ou indisponible, il stocke des données fallback chiffrées sous `<config-dir>/mcp/`.
3. Les permissions de fichier sont restreintes pour la clé fallback et le magasin de secrets chiffré.

Définissez `IAC_CODE_MCP_DISABLE_KEYRING=1` pour forcer le stockage fallback chiffré, utile pour les tests isolés.

Utilisez cette commande pour effacer l'état d'authentification stocké :

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## Confiance de projet

Les fichiers `.mcp.json` de projet ne sont pas approuvés automatiquement, car un dépôt peut ajouter un serveur `stdio` qui exécute du code local arbitraire. L'approbation interactive est liée à la signature de configuration du serveur. Modifier command, args, env, URL, headers ou OAuth config invalide l'approbation précédente.

Les modes headless et serveur de protocole ignorent les serveurs de projet non approuvés au lieu de demander confirmation.

## Gestion des secrets

IaC Code protège les secrets de plusieurs façons :

- La sortie de `iac-code mcp get` masque les clés qui ressemblent à des tokens, secrets, mots de passe, clés API et headers d'autorisation.
- Les valeurs sensibles de headers ou env en clair sont rejetées sauf si elles utilisent une référence à une variable d'environnement.
- Les serveurs MCP stdio héritent seulement d'une allowlist de variables d'environnement sûres plus l'env explicite du serveur.
- Les variables proxy contenant un nom d'utilisateur ou un mot de passe ne sont pas héritées par les serveurs MCP stdio.
- Les fichiers d'artefact MCP sont écrits sous le répertoire privé de configuration runtime de IaC Code.

## Autorisations

Les outils MCP utilisent le même système d'autorisations que les outils intégrés. Un serveur MCP distant ne peut pas contourner les contrôles d'IaC Code simplement en annonçant un outil. Gardez en tête :

- Les outils MCP en lecture seule peuvent être autorisés automatiquement selon la politique active.
- Les outils MCP destructifs doivent demander approbation sauf s'ils sont explicitement autorisés.
- En automatisation headless, combinez `--permission-mode`, `--allowed-tools` et `--disallowed-tools` pour limiter ce que les outils MCP peuvent faire.
- Les compétences MCP distantes n'accordent pas leurs propres `allowed_tools`.

## Fonctions sensibles non prises en charge

IaC Code rejette ou omet volontairement ces fonctions MCP pour l'instant :

- Commandes dynamiques `headersHelper`.
- Interface d'elicitation MCP.
- Transports WebSocket, IDE et SDK.
- Politique MCP d'entreprise gérée.
- IaC Code comme serveur MCP.
