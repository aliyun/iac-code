---
sidebar_position: 1
title: Protocolo ACP
description: Visao geral do suporte ao Agent Client Protocol no iac-code.
---

# Protocolo ACP

## O que e ACP

O [Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction) e um protocolo de comunicacao padronizado entre agentes de IA e seus clientes. Ele define como os clientes (IDEs, editores, ferramentas de automacao) iniciam, interagem e gerenciam sessoes de agentes atraves de mensagens JSON-RPC estruturadas.

## iac-code como um servidor ACP

O iac-code expoe suas capacidades de Infrastructure as Code atraves de um servidor ACP. Qualquer cliente compativel com ACP pode lancar `iac-code acp` como um subprocesso (ou conectar-se via HTTP+SSE) e programaticamente:

- Criar sessoes vinculadas a um diretorio de projeto
- Enviar prompts em linguagem natural e receber respostas em streaming
- Aprovar ou rejeitar operacoes de escrita de arquivos e operacoes destrutivas
- Gerenciar multiplas sessoes simultaneas

Assim, o iac-code funciona como um **backend combinável** para ambientes de desenvolvimento.

## Casos de uso

- **Integracao com IDE / Editor** — Zed, VS Code ou editores personalizados podem incorporar o iac-code como um servidor de contexto para fornecer geracao de IaC inline.
- **Orquestracao agente-a-agente** — Outros agentes de IA podem chamar as capacidades de IaC do iac-code atraves do protocolo, habilitando fluxos de trabalho multi-agente.
- **Pipelines de automacao** — Scripts de CI/CD ou bots de chatops podem invocar o iac-code de forma headless para gerar e validar templates.

## Comparacao de modos de interacao

| Modo | Comando | Melhor para |
|------|---------|-------------|
| **Web** | `iac-code` | Exploracao pratica, autoria iterativa de templates |
| **CLI nao interativo** | `iac-code --prompt "..."` | Scripting, geracao unica, pipelines de CI |
| **Servidor ACP** | `iac-code acp` | Integracao com IDE, gerenciamento multi-sessao, acesso programatico |

ACP oferece integração de clientes orientada a sessões com eventos estruturados em streaming. Web, A2A e o modo de processo SDK possuem suas próprias interfaces de sessão e streaming.

## Capacidades principais

- **Gerenciamento multi-sessao** — Crie, liste, bifurque, retome e feche sessoes independentes, cada uma com seu proprio historico de conversa e diretorio de trabalho.
- **Respostas em streaming** — Eventos em tempo real para texto do agente, raciocinio, chamadas de ferramentas, progresso de ferramentas e conclusao.
- **Framework de permissoes** — Ferramentas somente leitura sao auto-aprovadas; ferramentas de escrita e destrutivas requerem aprovacao explicita do cliente antes da execucao.
- **Transporte duplo** — Stdio para uso local/subprocesso, HTTP+SSE para cenarios remotos e de rede.
- **Passagem de configuracao de servidor MCP** — Os clientes podem declarar servidores MCP na criacao da sessao para aumento de ferramentas.
- **Suporte a comandos slash** — Encaminhe comandos slash (`/compact`, `/clear`, `/debug`, etc.) atraves do protocolo.
- **Metricas em tempo de execucao** — Uso de tokens, latencia e estatisticas de chamadas de ferramentas por sessao.
