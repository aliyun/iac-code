---
sidebar_position: 4
title: OAuth e segurança
description: Autentique servidores MCP remotos e entenda o modelo de segurança MCP no IaC Code.
---

# OAuth e segurança

MCP pode iniciar processos locais e chamar serviços remotos, então o IaC Code trata configuração e autenticação MCP como sensíveis para segurança.

## OAuth

Servidores remotos `http` e `sse` podem usar OAuth. Configure metadados OAuth na configuração do servidor:

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

Campos OAuth suportados:

| Campo | Finalidade |
|---|---|
| `clientId` | Id do cliente OAuth. |
| `clientSecretEnv` | Variável de ambiente que contém o client secret. |
| `callbackPort` | Porta de callback loopback opcional. Use `0` ou omita para escolher uma porta livre. |
| `authServerMetadataUrl` | URL explícita opcional de metadados do servidor de autorização. |

`oauth.clientSecret` em texto claro é rejeitado. Use `clientSecretEnv` ou o prompt CLI seguro.

## Autenticação

Execute:

```bash
iac-code mcp auth secure-reviewer --scope user
```

O IaC Code abre ou imprime uma URL de autorização e inicia um servidor de callback loopback em `127.0.0.1`. Depois que o provedor redireciona com um código de autorização, o IaC Code troca o código por tokens e os armazena com segurança.

Se um servidor precisar de autenticação durante uma sessão normal, o IaC Code registra uma ferramenta de autenticação:

```text
mcp__<server>__authenticate
```

O modelo pode chamar essa ferramenta para fornecer a URL OAuth ao usuário. Depois que o fluxo termina, o IaC Code reconecta o servidor MCP e atualiza as capacidades descobertas.

## Armazenamento de tokens

O IaC Code armazena tokens OAuth e secrets de cliente MCP por meio de `MCPSecretStorage`:

1. Tenta usar o keyring do sistema operacional quando disponível.
2. Se o keyring estiver desativado ou indisponível, armazena dados fallback criptografados em `<config-dir>/mcp/`.
3. Permissões de arquivo são restringidas para a chave fallback e o armazenamento criptografado.

Defina `IAC_CODE_MCP_DISABLE_KEYRING=1` para forçar o armazenamento fallback criptografado, útil para testes isolados.

Use este comando para limpar o estado de autenticação armazenado:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## Confiança de projeto

Arquivos `.mcp.json` de projeto não são confiados automaticamente, porque um repositório pode adicionar um servidor `stdio` que executa código local arbitrário. A aprovação interativa é vinculada à assinatura da configuração do servidor. Alterar command, args, env, URL, headers ou OAuth config invalida a aprovação anterior.

Modos headless e servidor de protocolo ignoram servidores de projeto não aprovados em vez de pedir confirmação.

## Tratamento de segredos

O IaC Code protege segredos de várias formas:

- A saída de `iac-code mcp get` mascara chaves que parecem tokens, secrets, passwords, API keys e authorization headers.
- Valores sensíveis de headers ou env em texto claro são rejeitados, a menos que usem referência a variável de ambiente.
- Servidores MCP stdio herdam apenas uma allowlist de variáveis de ambiente seguras mais o env explícito do servidor.
- Variáveis de proxy com usernames ou passwords não são herdadas por servidores MCP stdio.
- Arquivos de artefato MCP são escritos no diretório privado de configuração runtime do IaC Code.

## Permissões

Ferramentas MCP usam o mesmo framework de permissões das ferramentas nativas. Um servidor MCP remoto não consegue contornar verificações de permissão do IaC Code apenas anunciando uma ferramenta. Lembre-se:

- Ferramentas MCP somente leitura podem ser autoaprovadas dependendo da política ativa.
- Ferramentas MCP destrutivas devem exigir aprovação, salvo se explicitamente permitidas.
- Em automação headless, combine `--permission-mode`, `--allowed-tools` e `--disallowed-tools` para restringir o que ferramentas MCP podem fazer.
- Skills MCP remotos não concedem seus próprios `allowed_tools`.

## Recursos sensíveis não suportados

O IaC Code rejeita ou omite intencionalmente estes recursos MCP por enquanto:

- Comandos dinâmicos `headersHelper`.
- Interface de elicitation MCP.
- Transportes WebSocket, IDE e SDK.
- Política MCP empresarial gerenciada.
- IaC Code como servidor MCP.
