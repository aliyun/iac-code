---
sidebar_position: 1
title: Integração MCP
description: Use servidores Model Context Protocol para ampliar o IaC Code com ferramentas, recursos, prompts e habilidades externas.
---

# Integração MCP

IaC Code pode atuar como host Model Context Protocol (MCP). Os MCP servers ampliam o agent com tools, resources, prompts e reusable skills externos enquanto continuam passando pelos fluxos de permission, session, logging e output handling do IaC Code.

Use o MCP quando quiser que o Código IaC chame um recurso local ou remoto que não esteja integrado ao produto, como um catálogo de modelos privado, um revisor de implantação interno, um serviço de consulta de inventário ou uma ferramenta especializada de operação em nuvem.

## Supported Surfaces

| Surface | MCP support |
|---|---|
| REPL interativo | Carrega servidores de projetos de usuários, locais e aprovados. Avisa antes de confiar nos servidores `.mcp.json` do novo projeto. |
| Modo não interativo | Carrega servidores de projetos de usuários, locais e aprovados. Nunca solicita; servidores de projetos pendentes são ignorados com avisos. |
| Servidor ACP | Aceita configurações de servidor MCP de sessão de clientes ACP e expõe recursos MCP descobertos dentro dessa sessão. |
| Servidor A2A | Carrega o MCP durante o tempo de execução normal e pode publicar avisos do MCP e o progresso da ferramenta em metadados de tarefas A2A. |
| Modo pipeline | Usa as mesmas integrações de tempo de execução do modo normal, incluindo o progresso da ferramenta MCP e a propagação de avisos. |

## Supported Capabilities

| Capability | Status |
|---|---|
| transporte `stdio` | Compatível com processos do servidor MCP local. |
| Transporte HTTP streamável | Compatível com servidores MCP remotos. |
| Transporte SSE | Compatível com servidores MCP remotos. |
| Ferramentas MCP | Expostas como ferramentas de agente denominadas `mcp__<server>__<tool>`. |
| Recursos do MCP | Exposto através de `list_mcp_resources` e `read_mcp_resource`. |
| Solicitações do MCP | Exposto como comandos de barra denominados `mcp__<server>__<prompt>`. |
| Recursos `skill://` do MCP | Exposto como comandos de habilidade denominados `mcp__<server>__<skill>`. |
| Autenticação de loopback OAuth | Compatível com servidores remotos com metadados OAuth. |
| `roots/list` | Suportado. O Código IaC retorna a raiz do espaço de trabalho ativo como um URI de arquivo. |
| notificações `list_changed` | Com suporte para ferramentas, recursos e prompts. Os registros são atualizados dinamicamente. |
| MCP elicitation | Suportado em sessões interativas. Execuções não interativas cancelam com segurança. URL elicitation pode repetir o tool call original após confirmação do usuário. |
| WebSocket transport | Suportado para servers `ws://` e `wss://` somente com URL. WebSocket rejeita headers, `headersHelper` e OAuth porque o SDK transport instalado aceita apenas uma URL. |
| Comandos dinâmicos `headersHelper` | Suportados para servers `http` e `sse` confiáveis. Helpers executam sem shell, com timeout limitado, ambiente mínimo e diagnostics redigidos. |
| Transportes SDK e IDE | Não suportado. |
| Código IaC como servidor MCP | Não suportado. O Código IaC atualmente atua apenas como um host MCP. |

## How It Works

At runtime IaC Code:

1. Carrega a configuração do MCP de fontes de usuário, local, projeto e sessão.
2. Expande as referências `${VAR}` e `${VAR:-default}`.
3. Ignora servidores inseguros ou inválidos com avisos visíveis ao usuário.
4. Conecta servidores aprovados com simultaneidade limitada.
5. Descobre ferramentas, recursos, prompts e recursos `skill://`.
6. Registra esses recursos nos registros de ferramentas e comandos existentes.
7. Injeta instruções do servidor conectado no prompt do agente como orientação no escopo do servidor.
8. Converte os resultados da ferramenta MCP em resultados normais da ferramenta IaC Code, armazenando artefatos binários e artefatos de texto grandes no diretório de configuração de tempo de execução.
9. Desconecta clientes MCP quando o tempo de execução REPL, headless run, sessão ACP ou A2A é fechado.

Um servidor MCP com falha não bloqueia outros servidores configurados. As falhas de conexão e descoberta permanecem visíveis como avisos do MCP.

## Naming

As ferramentas e comandos do MCP são normalizados em nomes públicos:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Caracteres fora de letras, números e sublinhados tornam-se sublinhados. Se dois recursos descobertos colidirem após a normalização, o Código IaC anexa um breve resumo para manter os nomes exclusivos.

Para habilidades MCP, o Código IaC também registra um alias de compatibilidade como `<server>:<skill>` quando esse alias não entra em conflito com um comando existente. Os diagnósticos preservam os nomes originais do servidor, da ferramenta, do prompt ou da habilidade, mesmo quando os nomes públicos são normalizados.

## Related Pages

- [Inicio rapido MCP](./quick-start.md)
- [Configuração MCP](./configuration.md)
- [Ferramentas, recursos, prompts e habilidades](./capabilities.md)
- [OAuth e segurança](./oauth-and-security.md)
- [Solução de problemas](./troubleshooting.md)
