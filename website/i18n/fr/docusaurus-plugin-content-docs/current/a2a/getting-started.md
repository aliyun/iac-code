---
sidebar_position: 2
title: Bien démarrer
description: Démarrez le serveur A2A et envoyez votre premier message.
---

# Bien démarrer avec A2A

## Prérequis

1. **iac-code installé** — Consultez le guide [Installation](/docs/getting-started/installation).

2. **Identifiants LLM configurés** — Consultez le guide [Authentication](/docs/configuration/authentication) pour configurer les identifiants de votre fournisseur de modèle.

3. **Dépendances du serveur A2A** — Installez iac-code avec l'extra `a2a` :

```bash
uv sync --extra a2a
```

## Démarrer le serveur A2A

Démarrez le serveur sur l'interface locale par défaut :

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

Utilisez un fichier de configuration YAML lorsque vous avez besoin d'état local, de stockage d'artefacts, de livraison de notifications push ou d'Agent Cards signées :

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
thinking-exposure:
  - tool-trace
  - raw-thinking
```

Exécutez-le avec :

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` active les méthodes de configuration des notifications push de tâche A2A et la livraison des états terminaux. Utilisez `push-queue: redis-streams` avec `push-redis-url` lorsque plusieurs workers doivent coordonner la livraison push.

`thinking-exposure` contrôle les signaux d'exécution hors réponse exposés via `metadata.iac_code` pendant le streaming. La valeur par défaut est `tool-trace` ; n'ajoutez `raw-thinking` que pour les clients de confiance autorisés à voir le contenu de raisonnement du provider.

Le serveur expose :

| Route | Objectif |
|-------|---------|
| `GET /health` | Vérification de santé |
| `GET /.well-known/agent-card.json` | Découverte de l'Agent Card |
| `POST /` | Endpoint A2A JSON-RPC |

Le serveur HTTP enregistre également les routes REST du SDK A2A et annonce les interfaces `JSONRPC` et `HTTP+JSON` dans l'Agent Card.

## Vérifier la découverte

Récupérez l'Agent Card :

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Vous devriez voir `name: "iac-code"`, les interfaces `JSONRPC` et `HTTP+JSON`, des en-têtes de cache comme `ETag`, l'extension optionnelle `urn:iac-code:a2a:artifact-metadata:v1`, les modes d'entrée pris en charge, et des compétences comme `iac_generation`, `iac_review`, `aliyun_ros_operations` et `terraform_ros_conversion`.

Vérifiez l'endpoint de santé :

```bash
curl http://127.0.0.1:41242/health
```

Réponse attendue :

```json
{"status":"healthy"}
```

## Exiger l'authentification

L'authentification est optionnelle. Si aucune option d'authentification A2A ni variable d'environnement n'est définie, les requêtes n'ont pas besoin d'authentification. Lorsqu'un schéma d'authentification est configuré, chaque requête, y compris la découverte de l'Agent Card, doit satisfaire l'un des schémas configurés.

### Jeton Bearer

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

La clé de configuration YAML équivalente est `token`.

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Le nom d'utilisateur et le mot de passe doivent tous deux être présents. Les clés de configuration YAML équivalentes sont `basic-username` et `basic-password`.

### Clé API

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

L'en-tête de clé API par défaut est :

```text
X-API-Key: <api-key>
```

Remplacez-le avec la clé de configuration YAML `api-key-header` ou `IACCODE_A2A_API_KEY_HEADER` :

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## Appeler un agent A2A distant

Placez les paramètres stables de connexion client et d'authentification dans un fichier YAML :

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
```

Utilisez `a2a-client call` pour un appel client Phase 1 direct :

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

Utilisez `--stream` lorsque vous voulez des événements incrémentaux au lieu d'une réponse finale unique :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

Les options de ligne de commande remplacent les valeurs de configuration lorsque vous avez besoin d'une cible ou d'un jeton ponctuel :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

Pour le routage multi-agent, prévisualisez la sélection de route avant l'appel :

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

Consultez la [référence des commandes](./command-reference.md) pour toutes les commandes A2A, y compris la gestion des tâches, le CRUD de configuration push, les Agent Cards étendues et les options de transport.

## Envoyer un premier message avec curl

Passez le répertoire de l'espace de travail via `message.metadata.iac_code.cwd` ; le chemin doit être absolu, exister déjà et se trouver dans une racine d'espace de travail autorisée. Par défaut, les racines autorisées sont le répertoire du processus serveur et le répertoire temporaire système. Remplacez-les avec `IACCODE_A2A_ALLOWED_CWDS`.

Le serveur accepte les parties de type texte, les parties de données JSON, le texte UTF-8 brut, les fichiers texte locaux `file://` de l'espace de travail et les pièces jointes multimodales bornées. L'ingestion d'URL distante n'est pas prise en charge ; les parties `url` doivent être des URL locales `file://` dans l'espace de travail autorisé.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Generate a ROS VPC template with two vSwitches."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Pour la sortie en streaming, utilisez `SendStreamingMessage` :

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Review my Terraform files and suggest ROS equivalents."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Exemple minimal avec le SDK Python

L'exemple ci-dessous utilise `a2a-sdk>=1.0.2,<2`, qui est la plage de versions utilisée par l'extra `a2a`.

```python
"""Minimal iac-code A2A client using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Generate a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("status_update"):
                status = event.status_update.status
                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

:::tip
Pour les serveurs authentifiés, construisez le `httpx.AsyncClient` avec `headers={"Authorization": "Bearer <token>"}` afin que la découverte de l'Agent Card et les appels JSON-RPC incluent tous deux le jeton.
:::

## Étapes suivantes

- [Référence des commandes](./command-reference.md) — Référence complète des commandes et options CLI.
- [Référence du protocole](./protocol-reference.md) — Détails des méthodes, routes, états et métadonnées.
- [Transport HTTP](./http-transport.md) — Comportement HTTP JSON-RPC, authentification bearer et workflows curl.
- [Exemples](./examples.md) — Exemples SDK, HTTP direct, suivi, annulation et gestion des métadonnées.
