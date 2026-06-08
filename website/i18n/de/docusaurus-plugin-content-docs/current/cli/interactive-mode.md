---
title: Interaktiver Modus
description: Verwenden Sie das REPL fuer iterative Infrastrukturarbeit.
---

# Interaktiver Modus

Starten Sie ohne Argumente, um das interaktive REPL zu oeffnen:

```bash
iac-code
```

Der interaktive Modus ist nuetzlich, wenn Sie Infrastrukturanforderungen ueber mehrere Durchgaenge verfeinern moechten.

Beginnen Sie mit der Authentifizierung:

```text
/auth
```

Beschreiben Sie dann, was Sie erstellen moechten:

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```

## Befehle

Tippen Sie `/`, um verfügbare Slash-Befehle zu entdecken. Zu den häufigen Betriebsbefehlen gehören `/status` für den aktuellen Sitzungszustand, `/skills` für die Skill-Verwaltung, `/memory` für Projekt- und Benutzerspeicherdateien, `/rename` zum Benennen der aktiven Sitzung und `/resume` zum Wechseln zwischen Sitzungen.

Tippen Sie `$`, um ausschließlich Skills zu entdecken und aufzurufen.

## Eingabe bearbeiten

Verwenden Sie `Shift+Enter`, um eine neue Zeile einzufuegen, ohne den Prompt zu senden. Druecken Sie normales `Enter`, um den vollstaendigen Prompt zu senden.

Wenn Ihr Terminal `Shift+Enter` nicht getrennt uebermittelt, druecken Sie `Esc` und dann `Enter`, um eine neue Zeile einzufuegen. Mehrzeilige Prompts werden als ein Verlaufseintrag gespeichert, sodass `Up` den vollstaendigen Prompt wiederherstellt.

## Shell-Escapes

Stellen Sie einer Zeile `!` voran, um im REPL einen lokalen Shell-Befehl ueber das integrierte `bash`-Tool auszufuehren:

```text
!pwd
!git status --short
```

IaC Code wendet die normalen Tool-Berechtigungspruefungen an, fuehrt den Befehl im aktuellen Projektkontext aus und zeigt die Ausgabe im Terminal an. Der Befehl wird nicht als Chat-Nachricht an das Modell gesendet.
