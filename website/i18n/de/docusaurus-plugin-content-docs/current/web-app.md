---
title: Web-App
description: IaC Code als lokale Web-App mit derselben Engine wie die CLI ausführen.
---

# Web-App

IaC Code bringt eine lokale Web-App mit, die dieselbe Agenten-Engine wie das Terminal ausführt, jedoch im Browser statt in einer REPL dargestellt wird. Sie ist nützlich, wenn Sie eine grafische Chat-Oberfläche bevorzugen, mehrere Konversationen nebeneinander verwalten möchten oder den Pipeline-Fortschritt und die Werkzeugaktivität in einem übersichtlicheren Layout verfolgen wollen.

Die Web-App liest und schreibt denselben Sitzungsspeicher wie die CLI, sodass eine an einer Stelle begonnene Konversation an der anderen fortgesetzt werden kann.

## Installation

Die Web-App ist eine optionale Funktion, die vom Extra `http` (Starlette und Uvicorn) abhängt. Installieren Sie es zusammen mit dem Basispaket:

```bash
pip install 'iac-code[http]'
```

Wenn Sie `iac-code web` ohne das Extra ausführen, schlägt der Befehl mit einer Meldung fehl, die Sie zur Installation von `iac-code[http]` auffordert. Bei der Arbeit mit einem Checkout des Repositorys installiert `uv sync --extra http` dieselben Abhängigkeiten.

## Web-App starten

Starten Sie den Server im Terminal:

```bash
iac-code web
```

Standardmäßig bindet er an `127.0.0.1:8766` und öffnet Ihren Standardbrowser unter `http://127.0.0.1:8766`.

| Option | Standard | Beschreibung |
|---|---|---|
| `--host` | `127.0.0.1` | Host des HTTP-Servers. Es werden nur Loopback-Adressen akzeptiert. |
| `--port` | `8766` | Port des HTTP-Servers. |
| `--open` / `--no-open` | `--open` | Beim Start den Browser öffnen. Mit `--no-open` deaktivieren. |

```bash
iac-code web --port 9000 --no-open
```

### Sicherheit

Der Web-Server bindet ausschließlich an Loopback-Schnittstellen (`127.0.0.1`, `localhost` oder `::1`). Er ist für die Nutzung auf Ihrem eigenen Rechner gedacht und weist öffentliche Bind-Adressen ab. Machen Sie ihn nicht direkt in einem Netzwerk zugänglich; stellen Sie ihn hinter Ihren eigenen authentifizierten Proxy, falls Fernzugriff erforderlich ist.

## Überblick über die Oberfläche

### Sitzungs-Seitenleiste

Die Seitenleiste listet die Konversationen des ausgewählten Projekts auf. Von hier aus können Sie:

- Einen **neuen Chat** starten oder mit der Projektauswahl das Projekt wechseln.
- Konversationen **durchsuchen** oder die Befehlspalette öffnen, um einen Befehl auszuführen.
- Eine Konversation **anheften**, **umbenennen** oder **archivieren** und archivierte Konversationen durchsehen.

Da Sitzungen mit der CLI geteilt werden, erscheint eine mit `iac-code --resume` fortgesetzte Konversation auch hier. Wie der Sitzungsspeicher funktioniert, erfahren Sie unter [Sitzungen](./cli/sessions.md).

### Eingabebereich (Composer)

Im Eingabebereich formulieren Sie Ihre Anfragen. Er bietet dieselben Steuerelemente, die die CLI über Slash-Befehle und Flags bereitstellt:

- Auswahl von **Modell und Anbieter** für die aktive Sitzung.
- Einen **Denken**-Schalter, um erweitertes Schlussfolgern für unterstützte Modelle ein- oder auszuschalten.
- Ein Steuerelement für den **Berechtigungsmodus**, das festlegt, wie Werkzeugaktionen genehmigt werden.
- **Bildanhänge** für multimodale Modelle.
- **Slash-Befehle** (mit `/` eingegeben) und **`@`-Dateiverweise**, um auf Dateien in Ihrem Arbeitsbereich zu verweisen.

### Normaler Chat und Pipeline-Modus

Eine Sitzung läuft entweder als normaler Chat oder im **Pipeline**-Modus. Der normale Chat streamt die Antworten des Assistenten, Werkzeugaufrufe und Ergebnisse inline. Der Pipeline-Modus ergänzt einen Arbeitsbereich, der während der Ausführung Schritt-Zeitleisten, Diagnosen, Diagramme, Bereitstellungsfortschritt, Aufräumarbeiten und Übergabedetails anzeigt. Was Pipelines leisten, erfahren Sie unter [Pipeline-Modus](./automation/pipeline-mode.md).

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

Die Web-App ist eine alternative Oberfläche, kein eigenständiges Produkt. Sie nutzt dieselben Anbieter, Anmeldedaten, Skills, Werkzeuge und den Sitzungsspeicher wie das Terminal. Konfigurieren Sie Anbieter und Anmeldedaten einmal mit `/auth` in der CLI oder über die Einstellungen der Web-App, und beide Oberflächen teilen sie.
