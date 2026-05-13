# iac-code

**Language**: [English](../README.md) | [中文](README.zh.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [日本語](README.ja.md) | Português

Assistente de Infraestrutura como Código (IaC) impulsionado por IA que gera e gerencia modelos de orquestração de recursos do Alibaba Cloud (ROS / Terraform) por meio de interação em linguagem natural.

## Instalação

```bash
pip install iac-code
```

## Uso

No primeiro uso, configure o provedor de LLM e o serviço de nuvem IaC digitando `/auth` no modo interativo.

### Modo Interativo

Execute diretamente para entrar no REPL interativo:

```bash
iac-code
```

### Modo Não Interativo

Passe um prompt único via `--prompt`:

```bash
iac-code --prompt "Criar um VPC e duas instâncias ECS"
```

A leitura a partir do stdin também é suportada:

```bash
echo "Criar um bucket OSS" | iac-code --prompt -
```
