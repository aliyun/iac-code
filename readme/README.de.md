# iac-code

**Language**: [English](../README.md) | [中文](README.zh.md) | [Español](README.es.md) | [Français](README.fr.md) | Deutsch | [日本語](README.ja.md) | [Português](README.pt.md)

KI-gestützter Infrastructure-as-Code-Assistent (IaC), der Alibaba Cloud Ressourcen-Orchestrierungsvorlagen (ROS / Terraform) durch natürlichsprachliche Interaktion generiert und verwaltet.

## Installation

```bash
pip install iac-code
```

## Verwendung

Bei der ersten Nutzung konfigurieren Sie den LLM-Anbieter und den IaC-Cloud-Dienst, indem Sie `/auth` im interaktiven Modus eingeben.

### Interaktiver Modus

Direkt ausführen, um die interaktive REPL zu starten:

```bash
iac-code
```

### Nicht-interaktiver Modus

Übergeben Sie einen einmaligen Prompt über `--prompt`:

```bash
iac-code --prompt "Erstelle ein VPC und zwei ECS-Instanzen"
```

Das Lesen von stdin wird ebenfalls unterstützt:

```bash
echo "Erstelle einen OSS-Bucket" | iac-code --prompt -
```
