---
sidebar_position: 1
title: A2A-Protokoll
description: Uebersicht ueber die Agent2Agent-Unterstuetzung in iac-code.
---

# A2A-Protokoll

## Was ist A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) ist ein Protokoll zum Entdecken und Aufrufen entfernter Agents. Es ermoeglicht einem Agent, eine Agent Card zu veroeffentlichen, strukturierte Nachrichten anzunehmen, Task-Aktualisierungen zu streamen und Abbruch- sowie Task-Abfrageoperationen ueber Standard-Transports bereitzustellen.

## iac-code als A2A-Server

iac-code kann als A2A 1.0 Server / Agent ausgefuehrt werden. Andere A2A-kompatible Clients koennen ihn entdecken, Infrastructure-as-Code-Anfragen senden, Ausfuehrungsaktualisierungen streamen und aktive Tasks abbrechen.

Verwenden Sie A2A, wenn ein anderer Agent, eine Workflow-Engine oder ein Service iac-code als interoperablen IaC-Spezialisten aufrufen muss. Verwenden Sie ACP, wenn ein editorartiger Client Sitzungsverwaltung, Berechtigungsabfragen und lokale Entwicklungsintegration benoetigt.

## Anwendungsfaelle

- **Agent-Orchestrierung** - Ein Planner-Agent kann Alibaba Cloud ROS- oder Terraform-Arbeit an iac-code delegieren.
- **Workflow-Automatisierung** - Interne Tools koennen IaC-Generierungs-, Review- oder Konvertierungs-Tasks ueber HTTP einreichen.
- **Service Discovery** - Clients koennen die Agent Card abrufen und Faehigkeiten wie IaC-Generierung oder Template-Review auswaehlen.
- **Streaming-Integrationen** - Ein ChatOps- oder Dashboard-Client kann Modelltext, Tool-Aktivitaet, Nutzungsmetadaten und den finalen Task-Zustand anzeigen, waehrend der Turn laeuft.

## Vergleich der Interaktionsmodi

| Modus | Befehl | Am besten fuer |
|------|---------|----------|
| **Interaktive REPL** | `iac-code` | Praktische Erkundung und iterative Template-Erstellung |
| **Nicht interaktive CLI** | `iac-code --prompt "..."` oder `--headless` | Einmalige Skripte und CI-Jobs |
| **ACP-Server** | `iac-code acp` | IDE-/Editor-Integration und Multi-Session-Clientsteuerung |
| **A2A-Server** | `iac-code a2a` | Agent-zu-Agent-Interoperabilitaet ueber A2A-Transports |
| **A2A-Client** | `iac-code a2a-client call` | Aufrufen entfernter A2A-Agents aus iac-code |

## Kernfaehigkeiten

- **Agent-Card-Erkennung** - Veroeffentlicht `/.well-known/agent-card.json` mit Protocol Binding, Version, Skills, Eingabe-/Ausgabemodi und optionalen Auth-Metadaten.
- **HTTP JSON-RPC und REST** - Bedient A2A JSON-RPC-Anfragen unter `/` und registriert die SDK-REST-Routen.
- **Streaming-Antworten** - Unterstuetzt `SendStreamingMessage` fuer inkrementelle Task-Aktualisierungen.
- **Task-Verwaltung** - Unterstuetzt Task-Abfrage, authentifizierte Task-Auflistung mit Cursor-Paginierung, Abbruch aktiver Tasks und Abonnement aktiver Tasks.
- **Kontextwiederverwendung** - Verwendet eine iac-code-Laufzeit fuer Follow-up-Nachrichten im selben A2A `contextId` wieder.
- **Workspace-Eingrenzung** - Liest das Projektverzeichnis aus Nachrichtenmetadaten unter `iac_code.cwd`.
- **Tool-Metadaten** - Gibt iac-code-spezifische Metadaten fuer Tool-Starts, Eingabedeltas, abgeschlossene Tool-Ergebnisse, Berechtigungsentscheidungen und Token-Nutzung aus.
- **Eingabeteile** - Akzeptiert textartige Teile, JSON-Datenteile, rohen UTF-8-Text, lokale Workspace-Textdateien mit `file://` und begrenzte multimodale Anhaenge, die als Prompt-Manifeste dargestellt werden.
- **Client-Aufrufe** - Entdeckt entfernte Agent Cards, prueft Signaturen bei entsprechender Konfiguration und sendet Text-Prompts an entfernte Agents.
- **Routing** - Waehlt konfigurierte entfernte Agents nach explizitem Namen, Skill oder Prompt-/Tag-Abgleich aus.
- **Persistenzmetadaten** - Spiegelt lokale A2A-Task-/Kontext-Snapshots in JSON-Dateien fuer prozessuebergreifende Wiederherstellungsmetadaten.
- **Artifacts** - Speichert unterstuetzte lokale Text-Artifact-Payloads ausserhalb des gestreamten Event-Bodys, gibt standardmaessige `TaskArtifactUpdateEvent`-Events aus und zeichnet Task-`artifacts` auf.
- **Extensions und Caching** - Bewirbt die optionale iac-code-Artifact-Metadaten-Extension, validiert erforderliche `A2A-Extensions` und liefert Agent Cards mit Cache-Headern aus.
- **Push-Benachrichtigungen** - Unterstuetzt A2A-Task-Push-Notification-Config-Methoden, wenn `push-notifications: true` konfiguriert ist, mit lokalen dateibasierten oder Redis-gestuetzten Zustellqueues.
- **Agent-Card-Signierung** - Fuegt optionale A2A-SDK-JWS-Signaturen fuer Agent Cards hinzu und unterstuetzt `kid`-basierte Verifikation mit konfigurierten Schluesseln, lokalen Octet-JWKS-Daten oder einer entfernten JWKS-URL.
- **Mehrere Transports** - Laeuft ueber HTTP, stdio, Unix-Sockets, WebSocket, offizielles gRPC, benutzerdefiniertes gRPC JSON-RPC und Redis Streams-Transports.
- **CLI-Operationen** - Bietet Befehle fuer Discovery, Nachrichtensenden, Task-Abfrage/-Liste/-Abbruch/-Abo, Push-Config-CRUD, erweiterte Karten und Routenvorschauen.

## Phase-1-Unterstuetzung

iac-code unterstuetzt A2A-Servermodus ueber HTTP JSON-RPC/REST und mehrere optionale Transports sowie Phase-1-Clientmodus fuer das Aufrufen entfernter A2A-Agents. Es kann entfernte Agent Cards entdecken, beworbene Endpunkte auswaehlen, A2A 1.0-Prompts senden, Tasks abfragen/auflisten/abbrechen/abonnieren, zu konfigurierten Agents routen, lokale Task-/Kontext-Wiederherstellungsmetadaten persistieren, lokale Artifact-Payloads als Standard-Task-Artifacts speichern, erforderliche Extensions validieren, Push-Notification-Configs verwalten und Agent Cards mit HMAC- oder JWKS-Metadaten signieren oder verifizieren.

## In Phase 1 nicht unterstuetzt {#phase-1-unsupported}

- stdio, Unix-Sockets, WebSocket, gRPC JSON-RPC Envelope und Redis Streams sind experimentelle benutzerdefinierte JSON-RPC-Transports.
- Offizielles gRPC erfordert optionale Abhaengigkeiten und verwendet standardmaessig ein unsicheres lokales Server-Binding.
- Kein verteilter oder gemeinsamer Task-Store. Persistenz ist lokale Dateispeicherung im Laufzeit-Konfigurationsbereich von iac-code.
- Keine Wiederherstellung eines laufenden asyncio-Tasks nach einem Prozessneustart.
- Keine automatische Hintergrundfortsetzung unterbrochener entfernter Tasks.
- Kein OSS-, S3-, Datenbank- oder externer Object-Store-Artifact-Backend.
- Keine Aufnahme entfernter HTTP-URLs, kein Chunking grosser Binaerdaten und kein fortsetzbares Upload-Protokoll. Lokale File-URL-Teile muessen innerhalb der erlaubten Workspace-Roots bleiben.
- Kein standardmaessiger harter Fehler fuer unsignierte Agent Cards.
- Keine asymmetrische Agent-Card-Signierung vom Server und keine automatische Rotation von Signierschluesseln.
- Kein autonomer Planner-DAG und keine komplexe Multi-Agent-Orchestrierung.
- Push-Zustellung ist fuer Redis-gestuetzte Queues mindestens einmal; Callback-Empfaenger muessen Duplikate behandeln und ihre eigene autorisierungsseitige Endpoint-Policy erzwingen.

Tool-Berechtigungsanfragen werden im A2A-Servermodus automatisch abgelehnt, sofern sie nicht durch `auto-approve-permissions` oder eine explizite Berechtigungsregel erlaubt werden. Berechtigungsentscheidungen werden lokal auditiert; jede Allow-Entscheidung, die einen Auditdatensatz erfordert, schlaegt fail-closed fehl, wenn dieser Datensatz nicht persistiert werden kann. Geschuetzte Alibaba-Cloud-Schreib-APIs erfordern ausserhalb pauschaler Bypass-Modi weiterhin eine exakte Autorisierung pro API. Fuehren Sie den unauthentifizierten A2A-Modus nur in vertrauenswuerdigen lokalen Umgebungen aus oder schuetzen Sie ihn mit Bearer-Token-, Basic-Auth- oder API-Key-Authentifizierung.
