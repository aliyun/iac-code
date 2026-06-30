---
sidebar_position: 1
title: Integração MCP
description: Use servidores Model Context Protocol para estender o IaC Code com ferramentas, recursos, prompts e skills externos.
---

# Integração MCP

O IaC Code pode atuar como host do Model Context Protocol (MCP). Servidores MCP estendem o agente com ferramentas externas, recursos, prompts e skills reutilizáveis, mantendo o fluxo de permissões, sessão, logs e tratamento de saída do IaC Code.

Use MCP quando quiser que o IaC Code chame uma capacidade local ou remota que não é nativa do produto, como um catálogo privado de templates, um revisor interno de implantação, um serviço de inventário ou uma ferramenta especializada de operações em nuvem.

## Superfícies compatíveis

| Superfície | Suporte MCP |
|---|---|
| REPL interativo | Carrega servidores de usuário, locais e de projeto aprovados. Pergunta antes de confiar em novos servidores de projeto `.mcp.json`. |
| Modo não interativo | Carrega servidores de usuário, locais e de projeto aprovados. Nunca pergunta; servidores de projeto pendentes são ignorados com avisos. |
| Servidor ACP | Aceita configurações MCP de clientes ACP na sessão e expõe as capacidades MCP descobertas dentro dessa sessão. |
| Servidor A2A | Carrega MCP pelo runtime normal e pode publicar avisos MCP e progresso de ferramentas nos metadados de tarefas A2A. |
| Modo pipeline | Usa as mesmas integrações de runtime do modo normal, incluindo progresso de ferramentas MCP e propagação de avisos. |

## Capacidades compatíveis

| Capacidade | Status |
|---|---|
| Transporte `stdio` | Compatível com processos MCP locais. |
| Transporte Streamable HTTP | Compatível com servidores MCP remotos. |
| Transporte SSE | Compatível com servidores MCP remotos. |
| Ferramentas MCP | Expostas como ferramentas de agente chamadas `mcp__<server>__<tool>`. |
| Recursos MCP | Expostos por `list_mcp_resources` e `read_mcp_resource`. |
| Prompts MCP | Expostos como comandos slash chamados `mcp__<server>__<prompt>`. |
| Recursos MCP `skill://` | Expostos como comandos de skill chamados `mcp__<server>__<skill>`. |
| Autenticação OAuth loopback | Compatível com servidores remotos que têm metadados OAuth. |
| `roots/list` | Compatível. O IaC Code retorna a raiz ativa do workspace como URI file. |
| Notificações `list_changed` | Compatíveis para ferramentas, recursos e prompts. Os registros são atualizados dinamicamente. |
| MCP elicitation | Ainda não compatível. Servidores que solicitam elicitation recebem um erro claro de não suporte. |
| Transportes WebSocket, SDK e IDE | Não compatíveis. |
| Comandos dinâmicos `headersHelper` | Não compatíveis. Use headers estáticos ou referências a variáveis de ambiente. |
| IaC Code como servidor MCP | Não compatível. Atualmente o IaC Code atua apenas como host MCP. |

## Como funciona

Em tempo de execução, o IaC Code:

1. Carrega configuração MCP de fontes de usuário, locais, de projeto e de sessão.
2. Expande referências `${VAR}` e `${VAR:-default}`.
3. Ignora servidores inseguros ou inválidos com avisos visíveis ao usuário.
4. Conecta servidores aprovados com concorrência limitada.
5. Descobre ferramentas, recursos, prompts e recursos `skill://`.
6. Registra essas capacidades nos registros existentes de ferramentas e comandos.
7. Converte resultados de ferramentas MCP em resultados normais do IaC Code e armazena artefatos binários no diretório de configuração runtime.
8. Desconecta clientes MCP quando o REPL, a execução headless, a sessão ACP ou o runtime A2A é fechado.

Um servidor MCP com falha não bloqueia outros servidores configurados. Falhas de conexão e descoberta continuam visíveis como avisos MCP.

## Nomes

Ferramentas e comandos MCP são normalizados como nomes públicos:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Caracteres fora de letras, números e sublinhados viram sublinhados. Se capacidades descobertas colidirem depois da normalização, o IaC Code adiciona um digest curto para manter nomes únicos.

## Páginas relacionadas

- [Configuração MCP](./configuration.md)
- [Ferramentas, recursos, prompts e skills](./capabilities.md)
- [OAuth e segurança](./oauth-and-security.md)
- [Solução de problemas](./troubleshooting.md)
