---
title: Schnellstart
description: Konfigurieren Sie IaC Code und fuehren Sie Ihre erste Eingabe aus.
---

# Schnellstart

Starten Sie die interaktive CLI:

```bash
iac-code
```

Bei der ersten Verwendung konfigurieren Sie den LLM-Anbieter und die Alibaba Cloud-Anmeldedaten:

```text
/auth
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