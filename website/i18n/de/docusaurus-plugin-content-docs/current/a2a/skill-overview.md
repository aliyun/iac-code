---
sidebar_position: 1
title: Ueberblick ueber offizielle IaC Code Skills
description: Vergleichen Sie die offiziellen IaC Code Skills und waehlen Sie die passende Distribution.
---

# Ueberblick ueber offizielle IaC Code Skills

IaC Code ist in drei offiziellen Skill-Distributionen verfuegbar. Alle verwalten Alibaba-Cloud-Infrastruktur ueber
einen Agenten, unterscheiden sich aber bei Vertriebskanal und Ausfuehrungsort des IaC Code Agenten.

## Skill auswaehlen

| Skill | Ausfuehrungsort | Geeignet, wenn |
|---|---|---|
| `iac-code` | Verifizierte IaC Code Runtime auf Ihrem Rechner | Sie das eigenstaendige Paket des iac-code-Projekts und direkte Kontrolle ueber Installation und Updates wollen. |
| `alibabacloud-iac-code` | Dieselbe lokale Runtime, verpackt fuer das Alibaba Cloud Agent Skills Portal | Sie Alibaba Cloud Skills ueber das Portal oder `npx skills` verwalten. |
| `alibabacloud-ros-agent` | Gehosteter Alibaba Cloud ROS Agent ueber die ROS StartChat API | Sie einen Remote-Agenten ohne lokale IaC Code Runtime verwenden wollen. |

`iac-code` und `alibabacloud-iac-code` bieten dieselbe Runtime-Funktion. Waehlen Sie innerhalb eines Agentenbereichs
eine Distribution; beide zusammen erzeugen ueberlappende Ausloeser, aber keine zusaetzlichen Funktionen.

`alibabacloud-ros-agent` ist eine getrennte Remote-Integration. Sie kann neben einer lokalen Distribution installiert
werden, wenn Benutzer explizit zwischen lokaler und gehosteter Ausfuehrung waehlen sollen.

## Eigenstaendigen Skill beziehen

[Stabile iac-code-skill.zip herunterladen](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Diese Distribution eignet sich fuer eine manuell verwaltete Installation. Sie laedt beim ersten Einsatz die Runtime
und verwendet Modell- sowie Alibaba-Cloud-Einstellungen aus `~/.iac-code/`. Siehe
[IaC Code Skill installieren und verwenden](./skill-integration.md).

## Skills aus dem Alibaba Cloud Portal beziehen

Suchen Sie die exakten Namen im [Alibaba Cloud Agent Skills Portal](https://skills.aliyun.com/) oder installieren Sie
aus dem offiziellen Repository:

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

Direkte Downloads:

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) · [Quellcode](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) · [Quellcode](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` benoetigt Node.js 18 oder neuer und laesst Agent sowie Installationsbereich interaktiv auswaehlen. Bei einem
ZIP entpacken Sie das oberste Skill-Verzeichnis in das Benutzer- oder Projekt-Skill-Verzeichnis des Agenten.

## Unterschiede bei Funktionen und Konfiguration

Beide lokalen Distributionen unterstuetzen normale Gespraeche und Pipeline, Architekturplanung, ROS-/Terraform-
Vorlagen, Kostenschaetzung, Stack-Operationen, Bereitstellung und Bestaetigungen. Sie benoetigen ein konfiguriertes Modell
und fuer Abfragen oder Aenderungen von Cloud-Ressourcen Alibaba-Cloud-Zugangsdaten.

`alibabacloud-ros-agent` verbindet sich mit `ros:StartChat` zum Alibaba Cloud ROS Agent. Lokale Runtime und lokaler
Modellanbieter sind nicht erforderlich; verwendet wird die Alibaba-Cloud-Identitaet des Hosts. Gewaehren Sie nur die
noetigen RAM-Rechte. Ein expliziter Remote-Abbruch nutzt zusaetzlich `ros:StopChat`.

Pruefen Sie bei allen Varianten Ressourcen, Region, Auswirkungen, Preis und Rechte vor einer Freigabe. Speichern Sie
Zugangsdaten nicht in `SKILL.md`, Prompts oder Projektdateien.

## Weitere Dokumentation

- [IaC Code Skill installieren und verwenden](./skill-integration.md)
- [Host-Integrationsreferenz](./skill-host-integration.md)
- [Alibaba-Cloud-Zugangsdaten](../configuration/alibaba-cloud-credentials.md)
