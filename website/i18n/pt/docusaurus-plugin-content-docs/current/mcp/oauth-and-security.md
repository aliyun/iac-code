---
sidebar_position: 4
title: OAuth e segurança
description: Autentique servidores MCP remotos e entenda o modelo de segurança MCP no IaC Code.
---

# OAuth e segurança

O MCP pode iniciar processos locais e chamar serviços remotos, portanto, o Código IaC trata a configuração e a autenticação do MCP como sensíveis à segurança.

## OAuth

Servers remotos `http` e `sse` podem usar OAuth. Servers compatíveis com o padrao que publicam OAuth metadata e suportam Dynamic Client Registration nao exigem que voce informe um client id. Adicione o server e depois execute auth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Se um servidor exigir um cliente pré-provisionado, configure os metadados OAuth na configuração do servidor:

```json
{
  "mcpServers": {
    "secure-reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "iac-code",
        "clientSecretEnv": "MCP_CLIENT_SECRET",
        "callbackPort": 38487,
        "authServerMetadataUrl": "https://auth.example.com/.well-known/oauth-authorization-server"
      }
    }
  }
}
```

Supported OAuth fields:

| Field | Purpose |
|---|---|
| `clientId` | OAuth client id. |
| `clientSecretEnv` | Variável de ambiente que contém o segredo do cliente. |
| `callbackPort` | Porta de retorno de chamada de loopback opcional. Use `0` ou omita-o para escolher uma porta livre. |
| `authServerMetadataUrl` | URL opcional de metadados do servidor de autorização explícita. |
| `clientMetadataUrl` | URL opcional do documento de metadados do cliente HTTPS para servidores de autorização que suportam documentos de metadados de ID do cliente. |

O texto simples `oauth.clientSecret` foi rejeitado. Use `clientSecretEnv` ou o prompt CLI seguro.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

O código IaC abre ou imprime um URL de autorização e inicia um servidor de retorno de chamada em `127.0.0.1`. Se o navegador não puder abrir ou o retorno de chamada não puder ser concluído automaticamente, cole o URL de retorno de chamada ou o código de autorização no prompt da CLI. Após a autorização, o IaC Code troca o código por tokens e os armazena de forma segura.

Para servidores compatíveis com DCR, o Código IaC registra um cliente OAuth no servidor e armazena o ID do cliente retornado e o segredo do cliente opcional por meio do armazenamento secreto do MCP. A troca e atualização de token incluem o parâmetro de recurso selecionado pela semântica do SDK do MCP quando os metadados de recursos protegidos exigem isso.

Se um servidor precisar de autenticação durante uma sessão normal, o Código IaC registra uma ferramenta de autenticação:

```text
mcp__<server>__authenticate
```

O modelo pode chamar essa ferramenta para fornecer ao usuário a URL do OAuth. Após a conclusão do fluxo, o Código IaC reconecta o servidor MCP e atualiza os recursos descobertos.

## Token Storage

O Código IaC armazena tokens OAuth e segredos do cliente MCP por meio de `MCPSecretStorage`:

1. Os dados criptografados são armazenados em `<config-dir>/mcp/secrets.json.enc`.
2. A chave de criptografia é armazenada em `<config-dir>/mcp/secrets.key`.
3. As permissões de ambos os arquivos são restritas.

O armazenamento de segredos MCP não acessa o chaveiro do sistema operacional. Assim, verificações de estado em
segundo plano não exibem pedidos de autorização do sistema. O estado que existia apenas no chaveiro não é
migrado automaticamente; autorize o servidor MCP uma vez para criar a entrada local criptografada.

Use este comando para limpar o estado de autenticação armazenado:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` limpa, no scope persistente selecionado, OAuth token state, dynamic client registration state,
o `client_id` armazenado, o `client_secret` opcional e OAuth signature index, mas preserva o server config.
Ao remover um server persistente, o mesmo auth-state cleanup ocorre antes de apagar a configuracao:

```bash
iac-code mcp remove secure-reviewer --scope user
```

Use `reset-auth` para reautorizar um server existente. Use `mcp remove` quando o server config tambem deve sumir;
ambos os caminhos removem as entradas criptografadas gerenciadas por `MCPSecretStorage`.

## Project Trust

Os arquivos `.mcp.json` do projeto não são confiáveis automaticamente porque um repositório pode adicionar um servidor `stdio` que executa código local arbitrário. A aprovação interativa é feita por assinatura de configuração do servidor. Alterar comando, argumentos, env, URL, cabeçalhos ou configuração OAuth invalida a aprovação anterior.

Os modos de servidor headless e de protocolo ignoram servidores de projeto não aprovados em vez de solicitar.

## Secret Handling

O Código IaC protege segredos de várias maneiras:

- A saída de configuração de `iac-code mcp get` e `iac-code mcp get --config-only` mascara chaves que parecem tokens, secrets, passwords, API keys ou authorization headers.
- Valores sensíveis de headers ou env em texto claro são rejeitados ao adicionar servidores via `iac-code mcp add` ou `mcp add-json`, a menos que usem referência a variável de ambiente. Arquivos de configuração editados manualmente não são revalidados no carregamento; evite armazenar segredos em texto claro diretamente.
- Servidores MCP stdio herdam apenas uma allowlist de variáveis de ambiente seguras mais o env explícito do servidor.
- Variáveis de proxy com usernames ou passwords embutidos não são herdadas por servidores MCP stdio.
- Comandos `headersHelper` executam sem shell, sem stdin, com ambiente mínimo, captura limitada de stdout/stderr e diagnósticos privados de stderr mascarados.
- Arquivos de artefato MCP são escritos no diretório privado de configuração runtime do IaC Code.

## Permissions

As ferramentas MCP usam a mesma estrutura de permissão que as ferramentas integradas. Um servidor MCP remoto não pode ignorar as verificações de permissão do Código IaC simplesmente anunciando uma ferramenta. Tenha estas regras em mente:

- As ferramentas MCP somente leitura podem ser permitidas automaticamente dependendo da política de permissão ativa.
- As ferramentas destrutivas do MCP devem exigir aprovação, a menos que seja explicitamente permitido.
- Na automação headless, combine `--permission-mode`, `--allowed-tools` e `--disallowed-tools` para restringir o que as ferramentas MCP podem fazer.
- Habilidades remotas de MCP não concedem suas próprias `allowed_tools`.

## Recursos sensíveis à segurança não suportados

O Código IaC rejeita ou omite intencionalmente estes recursos do MCP por enquanto:

- Enterprise managed MCP policy.
- IDE and SDK transports.
- Headers de WebSocket, `headersHelper` de WebSocket e OAuth de WebSocket.
- IaC Code acting as an MCP server.
