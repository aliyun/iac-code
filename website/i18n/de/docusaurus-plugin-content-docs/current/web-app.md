---
title: Web-App
description: IaC Code als lokale Web-App mit derselben Engine wie die CLI ausführen.
---

# Web-App

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Konfigurieren Sie bei der ersten Verwendung den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen.

Die Web-App liest und schreibt denselben Sitzungsspeicher wie die CLI, sodass eine an einer Stelle begonnene Konversation an der anderen fortgesetzt werden kann.

## Installation

Die Standardinstallation enthält die Abhängigkeiten der Web-App und der HTTP-Laufzeit. Das Extra `[http]` bleibt als Kompatibilitätsalias erhalten und muss nicht mehr separat installiert werden. Im ausgecheckten Repository genügt `uv sync`.

```bash
pip install iac-code
```

## Web-App starten

Starten Sie den Server im Terminal:

```bash
iac-code web
```

Standardmäßig bindet er an `127.0.0.1:8766` und öffnet Ihren Standardbrowser unter `http://127.0.0.1:8766`.

| Option | Standard | Beschreibung |
|---|---|---|
| `--host` | `127.0.0.1` | HTTP-Serverhost. Der lokale Modus akzeptiert Loopback-Adressen; andere IP-Adressen erfordern `--access-token-file`. |
| `--port` | `8766` | Port des HTTP-Servers. |
| `--open` / `--no-open` | `--open` | Beim Start den Browser öffnen. Mit `--no-open` deaktivieren. |
| `--debug`, `-d` | `false` | Server-Debugprotokolle für diesen Lauf aktivieren. |
| `--access-token-file <path>` | — | Token-Datei für expliziten Fernzugriff mit verschlüsseltem API-Transport. |

```bash
iac-code web --port 9000 --no-open
```

### Sicherheit

Ohne `--access-token-file` akzeptiert der Server nur Loopback-Hosts (`127.0.0.1`, `localhost` oder `::1`) und prüft lokale Anfragen. Der explizite Token-Modus akzeptiert auch andere IP-Adressen und schützt API-Anfragen über den verschlüsselten Token-Transport.

## Überblick über die Oberfläche

### Sitzungs-Seitenleiste

Die Seitenleiste listet die Konversationen des ausgewählten Projekts auf. Von hier aus können Sie:

- Einen **neuen Chat** starten oder mit der Projektauswahl das Projekt wechseln.
- Konversationen **durchsuchen** oder die Befehlspalette öffnen, um einen Befehl auszuführen.
- Eine Konversation **anheften**, **umbenennen** oder **archivieren** und archivierte Konversationen durchsehen.

Da Sitzungen mit der CLI geteilt werden, erscheint eine mit `iac-code tui --resume` fortgesetzte Konversation auch hier. Wie der Sitzungsspeicher funktioniert, erfahren Sie unter [Sitzungen](./cli/sessions.md).

### Eingabebereich (Composer)

Im Eingabebereich formulieren Sie Ihre Anfragen. Er bietet dieselben Steuerelemente, die die CLI über Slash-Befehle und Flags bereitstellt:

- Auswahl von **Modell und Anbieter** für die aktive Sitzung.
- Einen **Denken**-Schalter, um erweitertes Schlussfolgern für unterstützte Modelle ein- oder auszuschalten.
- Ein Steuerelement für den **Berechtigungsmodus**, das festlegt, wie Werkzeugaktionen genehmigt werden.
- **Bildanhänge** für multimodale Modelle.
- **Slash-Befehle** (mit `/` eingegeben) und **`@`-Dateiverweise**, um auf Dateien in Ihrem Arbeitsbereich zu verweisen.

### Normaler Chat und Pipeline-Modus

Wählen Sie beim Erstellen einer Unterhaltung den Modus und bei Pipeline-Modus auch die Pipeline. Unterhaltungen in derselben App können unterschiedliche Modi nutzen, ohne den Server neu zu starten.

Eine Sitzung läuft entweder als normaler Chat oder im **Pipeline**-Modus. Der normale Chat streamt die Antworten des Assistenten, Werkzeugaufrufe und Ergebnisse inline. Der Pipeline-Modus ergänzt einen Arbeitsbereich, der während der Ausführung Schritt-Zeitleisten, Diagnosen, Diagramme, Bereitstellungsfortschritt, Aufräumarbeiten und Übergabedetails anzeigt. Was Pipelines leisten, erfahren Sie unter [Pipeline-Modus](./automation/pipeline-mode.md).

Die [`selling_solution_first`-Pipeline](./automation/solution-first-pipeline.md) nutzt diesen Arbeitsbereich für einen dreistufigen Kaufablauf: Architekturvarianten vergleichen, die ausgewählte Lösung umsetzen und sie nach Bestätigung bereitstellen. Tool-Genehmigungen erscheinen als lokalisierte Berechtigungskarten unter dem auslösenden Schritt; offene Genehmigungen werden beim Wiederherstellen der Sitzung demselben Schritt zugeordnet.

### Werkzeuge und Genehmigungen

Werkzeugaufrufe werden im Transkript als Karten dargestellt. Wenn ein Werkzeug Ihre Genehmigung benötigt, erscheint inline eine Genehmigungsanfrage; der im Eingabebereich eingestellte Berechtigungsmodus bestimmt, wann Sie gefragt werden.

### Einstellungen

Der Einstellungsbereich bündelt dieselbe Konfiguration, die die CLI verwaltet:

- **Cloud-Anmeldedaten** für Alibaba Cloud (siehe [Alibaba-Cloud-Anmeldedaten](./configuration/alibaba-cloud-credentials.md)).
- **Modelle** und Anbieterkonfiguration (siehe [LLM-Anbieter](./configuration/llm-providers.md)).
- **MCP-Plugins** (siehe [MCP-Integration](./mcp/overview.md)).
- Einsehen und Verwalten des **Gedächtnisses**.

### Oberflächensprache

Die Web-App ist in sieben Sprachen verfügbar — English, 简体中文, 日本語, Français, Deutsch, Español und Português — auswählbar in den Einstellungen. Ihre Auswahl wird für künftige Sitzungen gespeichert.

## Verhältnis zur CLI

Web, Desktop und Headless verwenden dieselben Anbieter, Zugangsdaten, Skills, Werkzeuge und Sitzungen. Richten Sie Modell und Cloud-Zugriff einmal in den Einstellungen ein, um sie über diese Einstiege hinweg zu nutzen.
