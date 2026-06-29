---
sidebar_position: 5
title: Solução de problemas MCP
description: Diagnostique problemas de configuração, conexão, autenticação e descoberta de capacidades MCP.
---

# Solução de problemas MCP

Avisos MCP não são fatais a menos que toda capacidade necessária esteja indisponível. Um servidor com falha não deve impedir que outros servidores MCP ou ferramentas nativas do IaC Code funcionem.

## Inspecionar configuração

Liste servidores configurados:

```bash
iac-code mcp list
```

Inspecione uma configuração de servidor mascarada:

```bash
iac-code mcp get my-server --scope local
```

Remova um servidor incorreto:

```bash
iac-code mcp remove my-server --scope local
```

Limpe escolhas de aprovação de projeto:

```bash
iac-code mcp reset-project-choices
```

## Servidor de projeto pendente

Sintoma:

```text
Project MCP server 'name' is pending approval.
```

Correção:

```bash
iac-code mcp approve name
```

ou inicie o REPL interativo nesse projeto e responda `y` quando solicitado. Pressionar Enter significa `N` e rejeita o servidor.

Se a aprovação funcionava antes e parou, verifique se `.mcp.json` mudou. A aprovação é vinculada à assinatura da configuração.

## Variável de ambiente ausente

Sintoma:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Corrija com uma destas opções:

```bash
export TOKEN=...
```

ou use um padrão:

```json
"Authorization": "${TOKEN:-}"
```

Servidores com variáveis de ambiente obrigatórias ausentes são ignorados.

## Falha de conexão

Para servidores stdio:

- Verifique se `command` existe no `PATH`.
- Use caminhos absolutos para scripts ao iniciar de diretórios diferentes.
- No Windows, execute servidores Node por `cmd /c npx`.
- Verifique se variáveis de ambiente obrigatórias estão configuradas.

Para servidores HTTP ou SSE:

- Verifique a URL e o tipo de transporte.
- Verifique TLS e configurações de proxy.
- Confirme que headers estáticos existem e não contêm segredos em texto claro.
- Execute `iac-code mcp auth <server>` se o servidor exigir OAuth.

## Precisa de autenticação

Sintoma:

```text
MCP server 'name' requires authentication.
```

Correção:

```bash
iac-code mcp auth name --scope user
```

Se o servidor usa OAuth refresh tokens e exige reautenticação, o IaC Code limpa tokens obsoletos e solicita um novo fluxo.

## Falha na descoberta de capacidade

Os sintomas podem incluir:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

O servidor conectou, mas uma lista de capacidades falhou. Outras capacidades do mesmo servidor ainda podem funcionar. Corrija o erro do lado do servidor e reinicie o IaC Code ou acione reconnect/auth refresh.

## Recursos ausentes

`list_mcp_resources` é registrado apenas quando pelo menos um servidor conectado expõe recursos. Se a ferramenta estiver ausente:

- Confirme que o servidor conectou.
- Confirme que o servidor suporta `resources/list`.
- Verifique avisos de inicialização para erros de discovery de recursos.

## Comando prompt ou skill ausente

Comandos de prompt e skill aparecem apenas depois de discovery bem-sucedida. Verifique:

- O prompt ou recurso `skill://` existe no servidor MCP.
- O nome de comando normalizado não conflita com um comando nativo.
- O recurso de skill remoto pode ser lido dentro do timeout de inicialização.
- A descrição e o corpo do skill cabem nos limites de segurança do IaC Code.

## Logs e artefatos

Logs runtime usam por padrão:

```text
<config-dir>/logs/
```

ou `IAC_CODE_LOG_DIR` quando definido.

Artefatos binários de resultados de ferramentas MCP são armazenados em:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Evite compartilhar diretórios de config, log ou artefatos sem revisar se contêm segredos.
