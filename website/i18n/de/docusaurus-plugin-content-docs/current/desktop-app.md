---
title: Desktop-App
description: Installieren und verwenden Sie die native IaC-Code-App unter macOS, Windows und Linux.
---

# Desktop-App

Die IaC-Code-Desktop-App bietet denselben Agenten, dieselben Modellanbieter, Cloud-Integrationen, Projekte und Unterhaltungen wie CLI und Web-App, jedoch als installierte native Anwendung. Der Tauri-Host startet die mitgelieferte Python-Laufzeit und lädt die lokale IaC-Code-Oberfläche über eine Loopback-Verbindung; ein öffentlicher Webdienst wird nicht bereitgestellt.

## Unterstützte Pakete

Laden Sie das Paket für Ihre Plattform von den [GitHub-Releases](https://github.com/aliyun/iac-code/releases) herunter.

| Betriebssystem | Architektur | Paket | Aktualisierung |
|---|---|---|---|
| macOS | Apple-Chip | `.dmg` | Aktualisierung in der App |
| Windows | x64 | `.exe`-Installationsprogramm | Aktualisierung in der App |
| Linux | x64 | `.AppImage` | Aktualisierung in der App |
| Debian / Ubuntu | x64 | `.deb` | Installation eines neueren Pakets |

Zu jedem Release gehören außerdem `SHA256SUMS`, eine Software-Stückliste (SBOM) und Hinweise zu Drittanbieterkomponenten.

## Installation

### macOS

1. Laden Sie die `.dmg`-Datei herunter, öffnen Sie sie und ziehen Sie **IaC Code** in den Ordner **Programme**.
2. Öffnen Sie IaC Code aus dem Ordner „Programme“.
3. Stabile Pakete sind mit einer Apple Developer ID signiert und von Apple notarisiert. Falls macOS das Paket dennoch nicht verifizieren kann, prüfen Sie die Herausgeberangaben und die Prüfsumme.

### Windows

1. Laden Sie das `.exe`-Installationsprogramm herunter und führen Sie es aus. IaC Code wird für den aktuellen Benutzer installiert und legt Verknüpfungen an.
2. Stabile Pakete tragen eine Authenticode-Herausgebersignatur. Falls Microsoft Defender SmartScreen dennoch warnt, prüfen Sie die Herausgeberangaben und `SHA256SUMS`, bevor Sie fortfahren.
3. Das Paket enthält die von der Oberfläche benötigte WebView2-Bootstrap-Unterstützung. Beim ersten Start prüft IaC Code außerdem, ob Git Bash installiert ist, und bietet bei Bedarf eine Installationsanleitung an.

### Linux AppImage

Machen Sie die heruntergeladene Datei ausführbar und starten Sie sie:

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

Nach dem ersten Start bietet Ihre Desktop-Umgebung möglicherweise an, einen Starter anzulegen. Das AppImage kann sich selbst aktualisieren, sobald eine signierte Aktualisierung verfügbar ist.

### Debian oder Ubuntu

Installieren Sie das deb-Paket mit APT, damit die Systemabhängigkeiten aufgelöst werden:

```bash
sudo apt install ./iac-code_*_amd64.deb
```

Starten Sie **IaC Code** über das Anwendungsmenü. Eine deb-Installation verwendet die Aktualisierung in der App nicht; installieren Sie für ein Upgrade das neuere deb-Paket.

## Erster Start

Beim ersten Start fordert IaC Code Sie zur Auswahl eines Projektordners auf. Dieser Ordner dient als Arbeitsbereich für Dateizugriffe, generierte Vorlagen, Werkzeuge und Unterhaltungen. Später können Sie das Projekt über die Projektauswahl wechseln.

Wenn Sie bereits die CLI oder Web-App verwendet haben, übernimmt die Desktop-App die Konfiguration aus `~/.iac-code/` (oder `IAC_CODE_CONFIG_DIR`), darunter Modellanbieter, Alibaba-Cloud-Zugangsdaten, Einstellungen und gespeicherte Sitzungen. Andernfalls öffnen Sie die **Einstellungen**, um vor der ersten Aufgabe einen Modellanbieter und die Cloud-Zugangsdaten einzurichten.

Die Oberfläche ist auf Englisch, vereinfachtem Chinesisch, Japanisch, Französisch, Deutsch, Spanisch und Portugiesisch verfügbar. Sprache und Farbschema lassen sich unter **Einstellungen > Allgemein** ändern.

## Aktualisierungen und Paketsignaturen

Die macOS-, Windows- und AppImage-Versionen fragen regelmäßig die Informationen zum stabilen Release ab und können eine neue Version herunterladen und installieren. Vor der Installation wird jede Aktualisierung mit dem öffentlichen IaC-Code-Aktualisierungsschlüssel geprüft. Das deb-Paket folgt stattdessen dem üblichen Linux-Paketverfahren.

Eine Aktualisierungssignatur ist nicht dasselbe wie die vom Betriebssystem geprüfte Herausgebersignatur. Erstere bestätigt, dass die Aktualisierung von IaC Code erstellt wurde; Apple-Developer-ID-Signatur und -Notarisierung sowie Windows Authenticode weisen den Herausgeber gegenüber dem Betriebssystem aus. Stabile macOS- und Windows-Pakete bestehen beide Prüfebenen. Beziehen Sie Pakete immer von der offiziellen Release-Seite und prüfen Sie `SHA256SUMS`.

## Fehlerbehebung

- **Die App bleibt auf dem Startbildschirm:** verwenden Sie die Wiederherstellungsaktionen, um den Start erneut zu versuchen oder den Diagnoseordner zu öffnen. Das Protokoll weist auf fehlende Laufzeitdateien, einen belegten Loopback-Port oder einen fehlgeschlagenen Start des Hilfsprozesses hin.
- **Windows meldet, dass Git Bash fehlt:** folgen Sie der Installationsanleitung, starten Sie IaC Code neu und führen Sie die Prüfung erneut aus. Shell-basierte Agentenwerkzeuge benötigen unter Windows Git Bash.
- **Linux öffnet das deb-Paket als Archiv:** installieren Sie es mit dem oben genannten APT-Befehl, statt es in einer Archivverwaltung zu öffnen.
- **Ein Stack oder externer Link wird unter Linux nicht geöffnet:** legen Sie einen Standardbrowser für die Desktop-Sitzung fest und versuchen Sie es erneut.
- **Einstellungen oder Sitzungen werden nicht mit der CLI geteilt:** prüfen Sie, ob beide Anwendungen denselben Wert für `IAC_CODE_CONFIG_DIR` und dasselbe Betriebssystem-Benutzerkonto verwenden.

Hinweise zur CLI-Installation und zu den Befehlen finden Sie unter [Installation](./getting-started/installation.md) und [CLI verwenden](./cli/usage.md). Die Browser-Oberfläche wird unter [Web-App](./web-app.md) beschrieben.
