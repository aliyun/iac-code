---
sidebar_position: 2
title: Démarrage
description: Lancer le serveur ACP et connecter votre premier client.
---

# Démarrage avec ACP

## Prérequis

1. **iac-code installé** -- Consultez le guide d'[Installation](../getting-started/installation.md).

2. **Identifiants LLM configurés** — Configurez **Paramètres → Modèles** dans bureau ou web, ou fournissez les paramètres pour l’automatisation. Voir [Authentification](../configuration/authentication.md).

3. **SDK Python ACP** (optionnel, pour les clients programmatiques)

   Le SDK Python officiel est publié sur PyPI sous le nom **`agent-client-protocol`** (importé en tant que `acp`). Les exemples de cette page sont vérifiés avec la version `0.9.0` :

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## Démarrage du serveur ACP

### Mode Stdio (par défaut)

```bash
iac-code acp
```

Le serveur communique via stdin/stdout en utilisant JSON-RPC. C'est le mode utilisé lorsqu'un IDE lance iac-code en tant que sous-processus.

### Mode HTTP+SSE

```bash
iac-code acp --transport http --port 8765
```

Écoute sur le port spécifié. Les clients se connectent via HTTP pour les requêtes et reçoivent les mises à jour en streaming via Server-Sent Events. Adapté aux scénarios distants ou multi-clients.

Vous pouvez sécuriser le point de terminaison HTTP en définissant la variable d'environnement `IACCODE_ACP_HTTP_TOKEN` -- le serveur exigera un en-tête `Authorization: Bearer <token>` correspondant.

### Vérifier le fonctionnement

```bash
# Stdio : le processus doit démarrer et attendre une entrée JSON-RPC sur stdin
iac-code acp

# HTTP : vérifier le point de terminaison de santé
curl http://127.0.0.1:8765/health
```

## Exemple minimal

Un exemple Python minimal utilisant le SDK officiel `agent-client-protocol`. Pour un guide plus complet (rendu des appels d'outils, blocs de réflexion, transport HTTP+SSE), consultez les [Exemples](./examples.md).

```python
"""Minimal iac-code ACP client using agent-client-protocol==0.9.0."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        # Stream assistant text to stdout; ignore other update kinds in this minimal demo.
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration — use interactive approval in production.
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main() -> None:
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize — negotiate capabilities
        init_result = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Protocol version: {init_result.protocol_version}")

        # 2. Create a session tied to your project directory
        session = await conn.new_session(cwd="/path/to/project")
        print(f"Session ID: {session.session_id}")

        # 3. Send a prompt; streaming output is delivered via MyClient.session_update
        result = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Generate a VPC template with 2 VSwitches",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Clean up
        await conn.close_session(session_id=session.session_id)


asyncio.run(main())
```

Points clés :

- `acp.spawn_agent_process` lance `iac-code acp` en tant que sous-processus et gère le cycle de vie de son stdio.
- `new_session(cwd=...)` limite les opérations sur les fichiers au répertoire donné.
- Les mises à jour en streaming (blocs de texte, réflexions, appels d'outils) arrivent via le callback `session_update` de votre sous-classe `acp.Client` -- `prompt()` lui-même retourne un seul `PromptResponse` une fois le tour terminé, avec le `stop_reason` final.
- Lorsqu'une demande de permission arrive, `request_permission` doit retourner soit un `AllowedOutcome(outcome="selected", option_id=...)` soit un `DeniedOutcome(outcome="cancelled")` -- toute autre valeur déclenche une `pydantic.ValidationError`.

## Configuration client

iac-code fonctionne avec tout éditeur ou client compatible ACP. La configuration ci-dessous s'applique à **Zed** et **VSCode** :

```json
{
  "agent_servers": {
    "iac-code": {
      "type": "custom",
      "command": "iac-code",
      "args": ["acp"]
    }
  }
}
```

- **Zed** -- Ajoutez l'extrait à votre fichier `settings.json` de Zed. Zed prend nativement en charge les serveurs d'agents ACP.
- **VSCode** -- Vous devez d'abord installer une extension client ACP (toute extension prenant en charge le Agent Client Protocol), puis appliquer la même configuration dans les paramètres de l'extension.

## Prochaines étapes

- [Référence du protocole](./protocol-reference.md) -- Documentation complète des méthodes et événements
- [Transport HTTP+SSE](./http-transport.md) -- Déploiement distant et authentification par jeton
