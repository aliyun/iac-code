---
sidebar_position: 4
title: OAuth und Sicherheit
description: Remote-MCP-Server authentifizieren und das MCP-Sicherheitsmodell in IaC Code verstehen.
---

# OAuth und Sicherheit

MCP kann lokale Prozesse starten und Remote-Dienste aufrufen, daher behandelt IaC-Code die MCP-Konfiguration und -Authentifizierung als sicherheitsrelevant.

## OAuth

Remote `http`- und `sse` servers koennen OAuth verwenden. Standardkonforme servers, die OAuth metadata veroeffentlichen und Dynamic Client Registration unterstuetzen, benoetigen keine vorab angegebene client id. Fuege den server hinzu und fuehre dann auth aus:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Wenn ein Server einen vorab bereitgestellten Client erfordert, konfigurieren Sie OAuth-Metadaten in der Serverkonfiguration:

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
| `clientSecretEnv` | Umgebungsvariable, die das Client-Geheimnis enthält. |
| `callbackPort` | Optionaler Loopback-Callback-Port. Verwenden Sie „0“ oder lassen Sie es weg, um einen freien Port auszuwählen. |
| `authServerMetadataUrl` | Optionale explizite Metadaten-URL des Autorisierungsservers. |
| `clientMetadataUrl` | Optionale HTTPS-Client-Metadatendokument-URL für Autorisierungsserver, die Client-ID-Metadatendokumente unterstützen. |

Klartext `oauth.clientSecret` wird abgelehnt. Verwenden Sie `clientSecretEnv` oder die sichere CLI-Eingabeaufforderung.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC-Code öffnet oder druckt eine Autorisierungs-URL und startet einen Loopback-Callback-Server auf `127.0.0.1`. Wenn der Browser nicht geöffnet werden kann oder der Rückruf nicht automatisch abgeschlossen werden kann, fügen Sie die Rückruf-URL oder den Autorisierungscode in die CLI-Eingabeaufforderung ein. Nach der Autorisierung tauscht IaC Code den Code gegen Tokens aus und speichert diese sicher.

Bei DCR-fähigen Servern registriert IaC-Code einen OAuth-Client beim Server und speichert die zurückgegebene Client-ID und das optionale Client-Geheimnis über den MCP-Geheimnisspeicher. Token-Austausch und -Aktualisierung umfassen den von der MCP SDK-Semantik ausgewählten Ressourcenparameter, wenn die Metadaten geschützter Ressourcen dies erfordern.

Wenn ein Server während einer normalen Sitzung eine Authentifizierung benötigt, registriert IaC Code ein Authentifizierungstool:

```text
mcp__<server>__authenticate
```

Das Modell kann dieses Tool aufrufen, um dem Benutzer die OAuth-URL bereitzustellen. Nachdem der Fluss abgeschlossen ist, verbindet IaC-Code den MCP-Server erneut und aktualisiert die erkannten Funktionen.

## Token Storage

IaC-Code speichert OAuth-Tokens und MCP-Client-Geheimnisse über `MCPSecretStorage`:

1. Es versucht den Betriebssystemschlüsselbund, sofern verfügbar.
2. Wenn der Schlüsselring deaktiviert oder nicht verfügbar ist, speichert er verschlüsselte Fallback-Daten unter `<config-dir>/mcp/`.
3. Die Dateiberechtigungen sind für den Fallback-Schlüssel und den verschlüsselten Geheimspeicher eingeschränkt.

Legen Sie `IAC_CODE_MCP_DISABLE_KEYRING=1` fest, um einen verschlüsselten Fallback-Speicher zu erzwingen, was für isolierte Tests nützlich ist.

Verwenden Sie diesen Befehl, um den gespeicherten Authentifizierungsstatus zu löschen:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` loescht fuer den ausgewaehlten persistierten scope OAuth token state, dynamic client registration state,
gespeicherte `client_id`, optionales `client_secret` und den OAuth signature index, behaelt aber den server config.
Beim Entfernen eines persistierten servers wird dieselbe auth-state cleanup vor dem Loeschen der Konfiguration ausgefuehrt:

```bash
iac-code mcp remove secure-reviewer --scope user
```

Verwenden Sie `reset-auth`, wenn Sie einen bestehenden server neu autorisieren moechten. Verwenden Sie `mcp remove`,
wenn auch der server config verschwinden soll; beide Pfade loeschen keyring und encrypted fallback entries, die
`MCPSecretStorage` verwaltet.

## Project Trust

Projektdateien `.mcp.json` werden nicht automatisch vertrauenswürdig, da ein Repository einen `stdio`-Server hinzufügen kann, der beliebigen lokalen Code ausführt. Die interaktive Genehmigung erfolgt pro Serverkonfigurationssignatur. Durch das Ändern von Befehl, Argumenten, Umgebung, URL, Headern oder OAuth-Konfiguration wird die vorherige Genehmigung ungültig.

Die Modi „Headless“ und „Protokollserver“ überspringen nicht genehmigte Projektserver, anstatt eine Eingabeaufforderung vorzunehmen.

## Secret Handling

IaC-Code schützt Geheimnisse auf verschiedene Weise:

- Die Konfigurationsausgabe von `iac-code mcp get` und `iac-code mcp get --config-only` schwärzt Schlüssel, die wie Tokens, Secrets, Passwörter, API-Keys oder Authorization-Header aussehen.
- Klartextwerte in sensiblen Headern oder Umgebungsvariablen werden beim Hinzufügen von Servern über `iac-code mcp add` oder `mcp add-json` abgelehnt, sofern sie keine Umgebungsvariablen-Referenz nutzen. Von Hand bearbeitete Konfigurationsdateien werden beim Laden nicht erneut validiert; speichern Sie dort keine Klartext-Secrets.
- MCP-stdio-Server erben nur eine Allowlist sicherer Umgebungsvariablen plus explizites Server-env.
- Proxy-Umgebungsvariablen mit eingebettetem Benutzernamen oder Passwort werden nicht an stdio-MCP-Server vererbt.
- `headersHelper`-Befehle laufen ohne Shell, ohne stdin, mit minimaler Umgebung, begrenzter stdout/stderr-Erfassung und geschwärzten privaten stderr-Diagnosen.
- MCP-Artefaktdateien werden im privaten Runtime-Konfigurationsverzeichnis von IaC Code geschrieben.

## Permissions

MCP-Tools verwenden dasselbe Berechtigungsframework wie integrierte Tools. Ein Remote-MCP-Server kann die IaC-Code-Berechtigungsprüfungen nicht einfach dadurch umgehen, dass er ein Tool ankündigt. Beachten Sie diese Regeln:

– Je nach aktiver Berechtigungsrichtlinie können schreibgeschützte MCP-Tools automatisch zugelassen werden.
– Zerstörerische MCP-Tools sollten einer Genehmigung bedürfen, sofern dies nicht ausdrücklich erlaubt ist.
- Kombinieren Sie in der Headless-Automatisierung `--permission-mode`, `--allowed-tools` und `--disallowed-tools`, um die Möglichkeiten von MCP-Tools einzuschränken.
- Remote-MCP-Fähigkeiten gewähren keine eigenen `allowed_tools`.

## Nicht unterstützte sicherheitsrelevante Funktionen

Der IaC-Code lehnt diese MCP-Funktionen vorerst absichtlich ab oder lässt sie weg:

- Enterprise managed MCP policy.
- IDE and SDK transports.
- WebSocket headers, WebSocket `headersHelper` und WebSocket OAuth.
- IaC Code acting as an MCP server.
