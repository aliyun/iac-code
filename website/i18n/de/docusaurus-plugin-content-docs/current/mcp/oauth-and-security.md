---
sidebar_position: 4
title: OAuth und Sicherheit
description: Authentifizieren Sie entfernte MCP-Server und verstehen Sie das MCP-Sicherheitsmodell in IaC Code.
---

# OAuth und Sicherheit

MCP kann lokale Prozesse starten und entfernte Dienste aufrufen. Deshalb behandelt IaC Code MCP-Konfiguration und Authentifizierung als sicherheitsrelevant.

## OAuth

Entfernte `http`- und `sse`-Server können OAuth verwenden. Konfigurieren Sie OAuth-Metadaten in der Serverkonfiguration:

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

Unterstützte OAuth-Felder:

| Feld | Zweck |
|---|---|
| `clientId` | OAuth-Client-ID. |
| `clientSecretEnv` | Umgebungsvariable mit dem Client Secret. |
| `callbackPort` | Optionaler Loopback-Callback-Port. Verwenden Sie `0` oder lassen Sie ihn weg, um einen freien Port zu wählen. |
| `authServerMetadataUrl` | Optionale explizite URL für Authorization-Server-Metadaten. |

Klartext `oauth.clientSecret` wird abgelehnt. Verwenden Sie `clientSecretEnv` oder den sicheren CLI-Prompt.

## Authentifizierung

Führen Sie aus:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code öffnet oder druckt eine Authorization-URL und startet einen Loopback-Callback-Server auf `127.0.0.1`. Nachdem der Provider mit einem Authorization Code zurückleitet, tauscht IaC Code ihn gegen Tokens und speichert sie sicher.

Wenn ein Server während einer normalen Sitzung Authentifizierung benötigt, registriert IaC Code ein Authentifizierungstool:

```text
mcp__<server>__authenticate
```

Das Modell kann dieses Tool aufrufen, um dem Benutzer die OAuth-URL zu geben. Nach Abschluss des Flows verbindet IaC Code den MCP-Server erneut und aktualisiert entdeckte Funktionen.

## Token-Speicherung

IaC Code speichert OAuth-Tokens und MCP-Client-Secrets über `MCPSecretStorage`:

1. Es versucht zuerst den Betriebssystem-Keyring, wenn verfügbar.
2. Wenn Keyring deaktiviert oder nicht verfügbar ist, speichert es verschlüsselte Fallback-Daten unter `<config-dir>/mcp/`.
3. Die Dateiberechtigungen für Fallback-Schlüssel und verschlüsselten Secret-Store werden eingeschränkt.

Setzen Sie `IAC_CODE_MCP_DISABLE_KEYRING=1`, um verschlüsselten Fallback-Speicher zu erzwingen. Das ist für isolierte Tests nützlich.

Mit diesem Command löschen Sie den gespeicherten Auth-Status:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## Projektvertrauen

Projektdateien `.mcp.json` werden nicht automatisch vertraut, weil ein Repository einen `stdio`-Server hinzufügen kann, der beliebigen lokalen Code ausführt. Interaktive Genehmigung ist an die Server-Konfigurationssignatur gebunden. Änderungen an command, args, env, URL, headers oder OAuth config machen frühere Genehmigungen ungültig.

Headless- und Protokollservermodi überspringen nicht genehmigte Projektserver, statt nachzufragen.

## Secret-Behandlung

IaC Code schützt Secrets auf mehrere Arten:

- Die Ausgabe von `iac-code mcp get` schwärzt Keys, die wie Tokens, Secrets, Passwörter, API-Keys oder Authorization-Headers aussehen.
- Klartextwerte in sensiblen Headers oder env-Einträgen werden abgelehnt, sofern sie keine Umgebungsvariablen-Referenz nutzen.
- MCP-stdio-Server erben nur eine Allowlist sicherer Umgebungsvariablen plus explizites Server-env.
- Proxy-Umgebungsvariablen mit Benutzernamen oder Passwörtern werden nicht an stdio-MCP-Server vererbt.
- MCP-Artefaktdateien werden im privaten Runtime-Konfigurationsverzeichnis von IaC Code geschrieben.

## Berechtigungen

MCP-Tools nutzen dasselbe Berechtigungssystem wie eingebaute Tools. Ein entfernter MCP-Server kann IaC Code-Berechtigungsprüfungen nicht umgehen, nur weil er ein Tool anbietet. Beachten Sie:

- Read-only MCP-Tools können je nach aktiver Policy automatisch erlaubt werden.
- Destruktive MCP-Tools sollten Genehmigung erfordern, sofern sie nicht explizit erlaubt sind.
- Kombinieren Sie in Headless-Automation `--permission-mode`, `--allowed-tools` und `--disallowed-tools`, um MCP-Tools einzuschränken.
- Remote-MCP-Skills vergeben keine eigenen `allowed_tools`.

## Nicht unterstützte sicherheitsrelevante Funktionen

IaC Code lehnt oder lässt diese MCP-Funktionen derzeit bewusst aus:

- Dynamische `headersHelper`-Commands.
- MCP-Elicitation-Oberfläche.
- WebSocket-, IDE- und SDK-Transporte.
- Enterprise Managed MCP Policy.
- IaC Code als MCP-Server.
