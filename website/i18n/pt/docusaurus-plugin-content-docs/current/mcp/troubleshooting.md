---
sidebar_position: 5
title: Solução de problemas MCP
description: Diagnostique problemas de configuração, conexão, autenticação e descoberta de capacidades MCP.
---

# Solução de problemas MCP

MCP warnings nao sao fatais, a menos que todas as capabilities necessarias estejam indisponiveis. Um server com falha nao deve impedir outros MCP servers ou tools integradas do IaC Code de funcionar.

## Inspect Configuration

Inspecione os servers configurados sem conectar:

```bash
iac-code mcp list
```

Execute bounded health diagnostics para os servers configurados:

```bash
iac-code mcp list --check
```

Inspecione uma configuração de servidor editada sem conectar:

```bash
iac-code mcp get my-server --scope local
```

Execute diagnósticos de integridade limitados para um servidor:

```bash
iac-code mcp get my-server --scope local --check
```

Inspecione a configuração explicitamente, sem conectar:

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

Reconecte um servidor ou todos os servidores persistentes:

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Sintoma:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Correcao:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

Use o `--scope` exato mostrado pela listagem de configuracao. Para arquivos persistentes fora do padrao, informe
tambem o `--source-path` correspondente. Se o server foi removido, adicione-o novamente em vez de autenticar uma configuracao ausente.

## Pending Project Server

Estado ou warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

ou inicie o REPL interativo nesse projeto e responda `y` quando solicitado. Pressionar Enter significa `N` e rejeita o servidor.

Se a aprovação funcionava, mas parou, verifique se `.mcp.json` foi alterado. A aprovação está vinculada à assinatura de configuração.

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

Os servidores com variáveis de ambiente obrigatórias ausentes são ignorados.

## Connection Failed

Estado ou warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
- Use caminhos absolutos para scripts ao iniciar em diretórios diferentes.
- No Windows, execute servidores baseados em Node através de `cmd /c npx`.
- Verifique se todas as variáveis de ambiente necessárias estão configuradas.

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- Confirme se os cabeçalhos estáticos estão presentes e não contêm segredos de texto simples.
- Execute `iac-code mcp auth <server>` se o servidor exigir OAuth.

## Needs Authentication

Estado: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

Se o servidor usar tokens de atualização OAuth e a reautenticação for necessária, o Código IaC limpará os tokens obsoletos e solicitará um novo fluxo.

## OAuth Auth Failed

Sintoma (`auth-failed`):

```text
MCP auth failed for 'name':
```

O OAuth flow iniciou, mas nao terminou corretamente: o callback URL pode estar incompleto, o authorization code pode
ter expirado ou o authorization server pode ter retornado um erro. Se um novo flow falhar antes de concluir,
o IaC Code restaura o auth state anterior.

Correcao:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

Tente `auth` novamente primeiro. Use `reset-auth` antes de tentar outra vez somente quando o token salvo ou dynamic client state estiver obsoleto.

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

O código IaC limpa o cliente OAuth armazenado e o estado do token para esse servidor. Execute a autenticação novamente:

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

O servidor solicitou escopos OAuth adicionais. Na sessão atual, abra `/mcp` e escolha `Autenticar` ou
`Autenticar novamente` para esse servidor; O Código IaC inclui os escopos relatados pelo desafio do servidor nesse fluxo. O
O comando autônomo `iac-code mcp auth name` inicia um fluxo de autenticação normal e não carrega escopos somente de desafio de um
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

Execute novamente com o `--scope` command exato impresso no erro. Isso e scope ambiguity: server name e valido, mas o comando precisa de um scope persistente.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

O servidor foi conectado, mas uma lista de recursos falhou. Outros recursos do mesmo servidor ainda poderão funcionar. Corrija o erro do lado do servidor e reinicie o código IaC ou acione uma atualização de reconexão/autenticação.

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

Para falhas repetidas, verifique se o servidor remoto interrompeu a sessão ou reiniciou.

## Headers Helper Failed

Os sintomas podem incluir erros de análise auxiliar, tempo limite, status de saída diferente de zero, JSON inválido ou valores de cabeçalho sem string. Verifique se o comando auxiliar é válido no diretório de origem de configuração e imprime um objeto JSON como:

```json
{"X-Org": "platform"}
```

O stderr semelhante a um segredo é redigido no diagnóstico.

## WebSocket Config Rejected

Os servidores WebSocket MCP suportam configuração somente de URL. Remova `headers`, `headersHelper` e `oauth` dos servidores `type: "ws"`.

## Resources Are Missing

`list_mcp_resources` é registrado apenas quando pelo menos um servidor conectado expõe recursos. Se a ferramenta estiver faltando:

- Confirm the server connected.
- Confirme se o servidor suporta `resources/list`.
- Verifique os avisos de inicialização em busca de erros de descoberta de recursos.

## Prompt or Skill Command Missing

Os comandos de prompt e habilidade aparecem somente após uma descoberta bem-sucedida. Verifique:

- O prompt ou recurso `skill://` existe no servidor MCP.
- O nome do comando normalizado não entra em conflito com um comando integrado.
- O recurso de habilidade remota pode ser lido dentro do tempo limite de inicialização.
- A descrição da habilidade e o corpo se enquadram nos limites de segurança do Código IaC.

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

Os artefatos binários MCP dos resultados da ferramenta são armazenados no diretório de propriedade da sessão para sessões v2:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

Sessões legadas sem uso de marcador de layout compatível:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Evite compartilhar diretórios de configuração, log ou artefatos sem revisá-los em busca de segredos.
