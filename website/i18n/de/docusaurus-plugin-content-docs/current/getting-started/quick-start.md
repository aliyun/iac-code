---
title: Schnellstart
description: Konfigurieren Sie IaC Code und fuehren Sie Ihre erste Eingabe aus.
---

# Schnellstart

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Konfigurieren Sie bei der ersten Verwendung den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen.

```bash
iac-code
```

Dann fragen Sie nach Infrastruktur:

```text
Create a VPC and two ECS instances
```

Fuer eine einmalige Eingabe verwenden Sie den nicht-interaktiven Modus:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Sie koennen die Eingabe auch von der Standardeingabe lesen:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
