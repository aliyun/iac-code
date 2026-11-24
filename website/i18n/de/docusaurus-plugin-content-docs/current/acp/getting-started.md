---
sidebar_position: 2
title: Erste Schritte
description: Starten Sie den ACP-Server und verbinden Sie Ihren ersten Client.
---

# Erste Schritte mit ACP

## Voraussetzungen

1. **iac-code installiert** -- Siehe die [Installationsanleitung](../getting-started/installation.md).

2. **LLM-Zugangsdaten eingerichtet** — Konfigurieren Sie **Einstellungen → Modelle** in Desktop oder Web oder stellen Sie Modellparameter für Automatisierung bereit. Siehe [Authentifizierung](../configuration/authentication.md).

3. **Python ACP SDK** (optional, fuer programmatische Clients)

   Das offizielle Python SDK ist auf PyPI als **`agent-client-protocol`** veroeffentlicht (importiert als `acp`). Die Beispiele auf dieser Seite sind gegen Version `0.9.0` verifiziert:

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## Starten des ACP-Servers

### Stdio-Modus (Standard)

```bash
iac-code acp
```

Der Server kommuniziert ueber stdin/stdout mittels JSON-RPC. Dies ist der Modus, der verwendet wird, wenn eine IDE iac-code als Unterprozess startet.

### HTTP+SSE-Modus

```bash
iac-code acp --transport http --port 8765
```

Lauscht auf dem angegebenen Port. Clients verbinden sich ueber HTTP fuer Anfragen und empfangen Streaming-Updates ueber Server-Sent Events. Geeignet fuer entfernte oder Multi-Client-Szenarien.

Sie koennen den HTTP-Endpunkt absichern, indem Sie die Umgebungsvariable `IACCODE_ACP_HTTP_TOKEN` setzen -- der Server erfordert dann einen passenden `Authorization: Bearer <token>`-Header.

### Funktionsfaehigkeit ueberpruefen

```bash
# Stdio: Der Prozess sollte starten und auf JSON-RPC-Eingabe ueber stdin warten
iac-code acp

# HTTP: Den Health-Endpunkt pruefen
curl http://127.0.0.1:8765/health
```

## Minimales Beispiel

Ein minimales Python-Beispiel mit dem offiziellen `agent-client-protocol` SDK. Fuer eine ausfuehrlichere Anleitung (Tool-Aufruf-Darstellung, Denkabschnitte, HTTP+SSE-Transport) siehe [Beispiele](./examples.md).

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

Wichtige Punkte:

- `acp.spawn_agent_process` startet `iac-code acp` als Unterprozess und verwaltet dessen Stdio-Lebenszyklus.
- `new_session(cwd=...)` beschraenkt Dateioperationen auf das angegebene Verzeichnis.
- Streaming-Updates (Textabschnitte, Gedanken, Tool-Aufrufe) kommen ueber den `session_update`-Callback Ihrer `acp.Client`-Unterklasse -- `prompt()` selbst gibt eine einzelne `PromptResponse` zurueck, sobald der Durchgang endet, mit dem finalen `stop_reason`.
- Wenn eine Berechtigungsanfrage eintrifft, muss `request_permission` entweder ein `AllowedOutcome(outcome="selected", option_id=...)` oder ein `DeniedOutcome(outcome="cancelled")` zurueckgeben -- jeder andere Wert loest einen `pydantic.ValidationError` aus.

## Client-Konfiguration

iac-code funktioniert mit jedem ACP-kompatiblen Editor oder Client. Die folgende Konfiguration gilt fuer **Zed** und **VSCode**:

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

- **Zed** -- Fuegen Sie den Ausschnitt zu Ihrer Zed `settings.json` hinzu. Zed unterstuetzt nativ ACP-Agentserver.
- **VSCode** -- Sie muessen zuerst eine ACP-Client-Erweiterung installieren (jede Erweiterung, die das Agent Client Protocol unterstuetzt), und dann die gleiche Konfiguration in den Einstellungen der Erweiterung anwenden.

## Naechste Schritte

- [Protokollreferenz](./protocol-reference.md) -- Vollstaendige Methoden- und Ereignisdokumentation
- [HTTP+SSE-Transport](./http-transport.md) -- Entfernte Bereitstellung und Token-Authentifizierung
