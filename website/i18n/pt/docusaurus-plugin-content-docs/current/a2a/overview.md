---
sidebar_position: 1
title: Protocolo A2A
description: Visão geral do suporte ao Agent2Agent no iac-code.
---

# Protocolo A2A

## O que é A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) é um protocolo para descobrir e chamar agentes remotos. Ele permite que um agente publique um Agent Card, aceite mensagens estruturadas, transmita atualizações de tarefas em streaming e exponha operações de cancelamento e consulta de tarefas por meio de transports padrão.

## iac-code como servidor A2A

O iac-code pode executar como um servidor / agente A2A 1.0. Outros clientes compatíveis com A2A podem descobri-lo, enviar solicitações de Infrastructure as Code, receber atualizações de execução em streaming e cancelar tarefas ativas.

Use A2A quando outro agente, mecanismo de workflow ou serviço precisar chamar o iac-code como um especialista em IaC interoperável. Use ACP quando um cliente no estilo editor precisar de gerenciamento de sessão, prompts de permissão e integração com desenvolvimento local.

## Casos de uso

- **Orquestração de agentes** — Um agente planejador pode delegar trabalho de Alibaba Cloud ROS ou Terraform ao iac-code.
- **Automação de workflow** — Ferramentas internas podem enviar tarefas de geração, revisão ou conversão de IaC via HTTP.
- **Descoberta de serviço** — Clientes podem buscar o Agent Card e escolher capacidades como geração de IaC ou revisão de templates.
- **Integrações de streaming** — Um cliente de chatops ou dashboard pode mostrar texto do modelo, atividade de ferramentas, metadados de uso e o estado final da tarefa enquanto o turno executa.

## Comparação dos modos de interação

| Modo | Comando | Melhor para |
|------|---------|-------------|
| **REPL interativo** | `iac-code` | Exploração prática e autoria iterativa de templates |
| **CLI não interativa** | `iac-code --prompt "..."` ou `--headless` | Scripts de uma única execução e jobs de CI |
| **Servidor ACP** | `iac-code acp` | Integração com IDE/editor e controle de cliente multi-sessão |
| **Servidor A2A** | `iac-code a2a` | Interoperabilidade agente-a-agente sobre transports A2A |
| **Cliente A2A** | `iac-code a2a-client call` | Chamar agentes A2A remotos a partir do iac-code |

## Capacidades principais

- **Descoberta de Agent Card** — Publica `/.well-known/agent-card.json` com binding de protocolo, versão, skills, modos de entrada/saída e metadados opcionais de autenticação.
- **HTTP JSON-RPC e REST** — Serve requisições A2A JSON-RPC em `/` e registra as rotas REST do SDK.
- **Respostas em streaming** — Suporta `SendStreamingMessage` para atualizações incrementais de tarefas.
- **Gerenciamento de tarefas** — Suporta consulta de tarefas, listagem autenticada de tarefas com paginação por cursor, cancelamento de tarefas ativas e assinatura de tarefas ativas.
- **Reuso de contexto** — Reutiliza um runtime do iac-code para mensagens de acompanhamento no mesmo `contextId` A2A.
- **Escopo de workspace** — Lê o diretório do projeto a partir dos metadados da mensagem em `iac_code.cwd`.
- **Metadados de ferramentas** — Emite metadados específicos do iac-code para inícios de ferramentas, deltas de entrada, resultados de ferramentas concluídos, decisões de permissão e uso de tokens.
- **Partes de entrada** — Aceita partes semelhantes a texto, partes de dados JSON, texto UTF-8 bruto, arquivos de texto locais `file://` do workspace e anexos multimodais limitados representados como manifestos de prompt.
- **Chamadas de cliente** — Descobre Agent Cards remotos, verifica assinaturas quando configurado e envia prompts de texto para agentes remotos.
- **Roteamento** — Seleciona agentes remotos configurados por nome explícito, skill ou correspondência de prompt/tag.
- **Metadados de persistência** — Espelha snapshots locais de tarefas/contextos A2A em arquivos JSON para metadados de restauração entre processos.
- **Artefatos** — Armazena payloads de artefatos de texto locais suportados fora do corpo do evento em streaming, emite eventos padrão `TaskArtifactUpdateEvent` e registra `artifacts` da tarefa.
- **Extensões e cache** — Anuncia a extensão opcional de metadados de artefato do iac-code, valida `A2A-Extensions` exigidas e serve Agent Cards com cabeçalhos de cache.
- **Notificações push** — Suporta métodos de configuração de notificação push de tarefas A2A quando `push-notifications: true` está configurado, com filas de entrega baseadas em arquivos locais ou Redis.
- **Assinatura de Agent Card** — Adiciona assinaturas JWS opcionais do SDK A2A para Agent Cards e suporta verificação baseada em `kid` com chaves configuradas, dados JWKS octet locais ou uma URL JWKS remota.
- **Múltiplos transports** — Executa sobre HTTP, stdio, Unix sockets, WebSocket, gRPC oficial, JSON-RPC gRPC customizado e transports Redis Streams.
- **Operações de CLI** — Fornece comandos para descoberta, envio de mensagens, consulta/listagem/cancelamento/assinatura de tarefas, CRUD de configuração push, cards estendidos e pré-visualizações de rotas.

## Suporte da Fase 1

O iac-code suporta modo servidor A2A sobre HTTP JSON-RPC/REST e vários transports opcionais, além do modo cliente Fase 1 para chamar agentes A2A remotos. Ele pode descobrir Agent Cards remotos, selecionar endpoints anunciados, enviar prompts A2A 1.0, consultar/listar/cancelar/assinar tarefas, rotear para agentes configurados, persistir metadados locais de restauração de tarefas/contextos, armazenar payloads de artefatos locais como artefatos de tarefa padrão, validar extensões obrigatórias, gerenciar configurações de notificação push e assinar ou verificar Agent Cards com metadados HMAC ou JWKS.

## Sem suporte na Fase 1 {#phase-1-unsupported}

- stdio, Unix sockets, WebSocket, envelope JSON-RPC gRPC e Redis Streams são transports JSON-RPC customizados experimentais.
- gRPC oficial exige dependências opcionais e usa por padrão um binding de servidor local inseguro.
- Não há armazenamento de tarefas distribuído ou compartilhado. A persistência é armazenamento local de arquivos na área de configuração de runtime do iac-code.
- Não há restauração de uma tarefa asyncio em execução após reinício do processo.
- Não há continuação automática em segundo plano de tarefas remotas interrompidas.
- Não há backend de artefatos OSS, S3, banco de dados ou object-store externo.
- Não há ingestão de URL HTTP remota, divisão de binários grandes em chunks ou protocolo de upload retomável. Partes de URL de arquivo local devem permanecer dentro das raízes de workspace permitidas.
- Não há falha rígida padrão para Agent Cards não assinados.
- Não há assinatura assimétrica de Agent Card pelo servidor nem rotação automática de chaves de assinatura.
- Não há DAG de planejador autônomo nem orquestração multiagente complexa.
- A entrega push é at-least-once para filas baseadas em Redis; receptores de callback devem lidar com duplicatas e aplicar sua própria política de autorização no lado do endpoint.

Solicitações de permissão de ferramentas são rejeitadas automaticamente no modo servidor A2A, a menos que `auto-approve-permissions` ou uma regra de permissão explícita as permita. Decisões de permissão são auditadas localmente; toda decisão allow que exige um registro de auditoria falha de forma fechada se esse registro não puder ser persistido. APIs protegidas de escrita Alibaba Cloud ainda exigem autorização exata por API fora de modos de bypass global. Execute o modo A2A não autenticado apenas em ambientes locais confiáveis ou proteja-o com autenticação por Bearer token, Basic auth ou API key.
