# iac-code

**Language**: [English](../README.md) | [中文](README.zh.md) | [Español](README.es.md) | Français | [Deutsch](README.de.md) | [日本語](README.ja.md) | [Português](README.pt.md)

Assistant d'Infrastructure as Code (IaC) propulsé par l'IA qui génère et gère des modèles d'orchestration de ressources Alibaba Cloud (ROS / Terraform) via une interaction en langage naturel.

## Installation

```bash
pip install iac-code
```

## Utilisation

Lors de la première utilisation, configurez le fournisseur LLM et le service cloud IaC en saisissant `/auth` en mode interactif.

### Mode Interactif

Exécutez directement pour accéder au REPL interactif :

```bash
iac-code
```

### Mode Non Interactif

Passez un prompt unique via `--prompt` :

```bash
iac-code --prompt "Créer un VPC et deux instances ECS"
```

La lecture depuis stdin est également prise en charge :

```bash
echo "Créer un bucket OSS" | iac-code --prompt -
```
