---
title: Slash-Befehle
description: Vollstaendige Referenz fuer eingebaute interaktive Befehle.
---

# Slash-Befehle

Slash-Befehle steuern IaC Code innerhalb einer interaktiven Sitzung. Tippen Sie `/`, um verfuegbare Befehle anzuzeigen, und tippen Sie weiter, um die Liste zu filtern. Ein Befehl wird nur erkannt, wenn er am Anfang Ihrer Nachricht steht.

Die `/`-Liste enthaelt sowohl integrierte Befehle als auch alle konfigurierten Skills. Um die Vorschlaege nur auf Skills zu beschraenken, verwenden Sie stattdessen `$` — `$<name>` listet und ruft ausschliesslich Skills auf, und wenn Sie `$` gefolgt vom Namen eines integrierten Befehls (zum Beispiel `$help`) eingeben, wird ein Fehler ausgegeben, der auf das `/`-Aequivalent verweist.

Text nach dem Befehlsnamen wird als Argumente uebergeben. In der folgenden Tabelle kennzeichnet `<arg>` ein erforderliches Argument und `[arg]` ein optionales Argument.

| Befehl | Zweck |
|---|---|
| `/auth` | Konfigurieren Sie den Zugang zum Modellanbieter und die Alibaba Cloud-Anmeldedaten ueber den interaktiven Authentifizierungsablauf. Verwenden Sie dies beim erstmaligen Einrichten von IaC Code, beim Aendern von API-Schluesseln, beim Wechseln des Anbieters oder beim Aktualisieren des Cloud-Zugangs. Alias: `/login`. |
| `/clear` | Loeschen Sie den aktuellen Gespraechsverlauf und setzen Sie den aktiven Kontextmanager zurueck. Im interaktiven Modus wird auch der Terminalbildschirm geloescht und das Willkommensbanner erneut angezeigt. Verwenden Sie dies, wenn Sie eine neue Anfrage starten moechten, ohne das REPL zu verlassen. |
| `/compact` | Fassen Sie das aktuelle Gespraech zusammen, um die Kontextnutzung zu reduzieren und gleichzeitig die letzten Durchgaenge beizubehalten. Verwenden Sie dies nach einer langen Sitzung, wenn Sie mit weniger angesammeltem Kontext weiterarbeiten moechten. Wenn das Gespraech leer oder zu kurz ist, meldet der Befehl, dass nichts zu komprimieren ist. |
| `/debug [on\|off\|status]` | Pruefen oder aendern Sie die Laufzeit-Debug-Protokollierung fuer die aktive Sitzung. `/debug` und `/debug status` zeigen an, ob die Protokollierung aktiviert ist und, wenn aktiviert, den Pfad der Protokolldatei. `/debug on` aktiviert die Protokollierung fuer die aktuelle Sitzung. `/debug off` deaktiviert sie. |
| `/effort [level]` | Zeigen oder aendern Sie den Denkaufwand fuer das aktive Modell, wenn das ausgewaehlte Modell Aufwandsteuerung unterstuetzt. Mit einem Level wird der angeforderte Wert angewendet, wenn er fuer das Modell gueltig ist. Ohne Level wird im REPL eine interaktive Auswahl geoeffnet, oder der aktuelle Aufwand wird in nicht-interaktiven Kontexten ausgegeben. |
| `/exit` | Beenden Sie das interaktive REPL. Aliase: `/quit`, `/q`. |
| `/help` | Zeigen Sie verfuegbare Befehle und gaengige Tastenkuerzel im REPL an. Alias: `/?`. |
| `/memory` | Öffnet die Speicherauswahl. Bearbeiten Sie Projekt- oder Benutzerdateien `AGENTS.md`, schalten Sie auto-memory ein oder aus und öffnen Sie den auto-memory-Ordner des Projekts, wenn auto-memory aktiviert ist. |
| `/model [model_name]` | Zeigen oder wechseln Sie das aktive Modell. Mit `model_name` wird direkt zu diesem Modell fuer den aktiven Anbieter gewechselt. Ohne Argument wird eine interaktive Modellauswahl geoeffnet, wenn ein Anbieter konfiguriert ist, oder das aktuelle Modell wird ausgegeben, wenn keine Konsolen-UI verfuegbar ist. |
| `/rename <name>` | Die aktuelle Sitzung benennen. Namen erscheinen im Willkommensbanner, im Exit-Hinweis und in der `/resume`-Auswahl und können mit `/resume` oder `--resume` verwendet werden, wenn sie eine Sitzung eindeutig identifizieren. |
| `/resume [sitzungs-id\|eindeutiges-id-präfix\|eindeutiger-sitzungsname]` | Eine frühere Sitzung fortsetzen. Mit einem Argument löst IaC Code es als exakte Sitzungs-ID, eindeutiges ID-Präfix oder eindeutigen Sitzungsnamen auf. Ohne Argument wird die interaktive Sitzungsauswahl geöffnet. Projektübergreifende Sitzungen geben einen `cd ... && iac-code --resume <id>`-Befehl aus, anstatt das aktuelle Projekt direkt zu wechseln. |
| `/skills` | Die Skill-Verwaltungsauswahl öffnen. Skills nach Name oder Beschreibung suchen, nach Name/Quelle/Größe sortieren und Benutzer- oder Projekt-Skills aktivieren oder deaktivieren. Gebündelte Skills bleiben gesperrt aktiviert. |
| `/status` | Aktuelle Sitzungs-ID, Anbieter, Modell, Alibaba Cloud-Region, Arbeitsverzeichnis, aufgezeichnete API-Token-Nutzung, Rundenzahl und Kontextauslastung anzeigen. Im Debug-Modus werden außerdem Speicherabruf-Side-Call-Zähler und deren Token-Nutzung angezeigt. |

Die genaue Befehlsliste kann sich zwischen Versionen aendern. Verwenden Sie `/help` oder tippen Sie `/` im REPL, um die in Ihrer installierten Version verfuegbaren Befehle anzuzeigen.

## Speicher

Verwenden Sie `/memory`, um die Speicherdateien zu bearbeiten, die IaC Code in die Unterhaltung lädt:

- Projektspeicher wird standardmäßig in `AGENTS.md` im Projektstamm gespeichert.
- Benutzerspeicher wird in `AGENTS.md` im Laufzeit-Konfigurationsverzeichnis gespeichert, standardmäßig `~/.iac-code/`.
- Setzen Sie `IAC_CODE_INSTRUCTION_MEMORY_FILE`, um einen anderen Dateinamen zu verwenden, zum Beispiel `IAC-CODE.md`.
- Der Editor ist ein kompakter Vollbild-Editor im Vim-Stil. Verwenden Sie `i`, `a` oder `o`, um in den Einfügemodus zu wechseln, `Esc`, um zum Normalmodus zurückzukehren, `:wq` zum Speichern und `:q!` zum Verwerfen.
- Die Zeile `Auto-memory` kann mit `Enter` umgeschaltet werden. Wenn auto-memory aktiviert ist, kann IaC Code relevante Projektthemen-Speicher als versteckten Unterhaltungskontext abrufen.
- Die Option für den auto-memory-Ordner erscheint nur, wenn auto-memory aktiviert ist.
