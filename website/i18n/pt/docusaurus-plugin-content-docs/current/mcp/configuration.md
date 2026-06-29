---
sidebar_position: 2
title: Configuração MCP
description: Configure servidores MCP por comandos CLI, arquivos settings, arquivos de projeto e sessões ACP.
---

# Configuração MCP

Servidores MCP são configurados no objeto `mcpServers`. O IaC Code suporta um schema central compatível com Claude Code para servidores `stdio`, `http` e `sse`.

## Fontes de configuração

O IaC Code lê servidores MCP destas fontes:

| Fonte | Scope | Arquivo ou ponto de entrada | Modelo de confiança |
|---|---|---|---|
| Settings de usuário | `user` | `~/.iac-code/settings.yml` ou `IAC_CODE_CONFIG_DIR/settings.yml` | Confiável para o usuário atual. |
| Settings locais do projeto | `local` | `<workspace>/.iac-code/settings.local.yml` | Privado do checkout local. |
| Arquivo MCP do projeto | `project` | `<workspace>/.mcp.json` | Compartilhado com o projeto e exige aprovação local. |
| Configuração de sessão ACP | `session` | `mcp_servers` enviados por um cliente ACP | Aplica-se apenas ao runtime dessa sessão ACP. |

A precedência é user, project, local e depois session. Fontes posteriores sobrescrevem fontes anteriores por nome de servidor. Configurações equivalentes também são deduplicadas por assinatura de conteúdo.

Arquivos `.mcp.json` de projeto são descobertos da raiz do workspace até o diretório atual. Arquivos filhos sobrescrevem arquivos pais por nome de servidor.

## Comandos CLI

Use `iac-code mcp` para gerenciar a configuração MCP persistida:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

Comandos disponíveis:

| Comando | Finalidade |
|---|---|
| `iac-code mcp add` | Adiciona um servidor usando flags CLI estruturados. |
| `iac-code mcp add-json` | Adiciona um servidor a partir de um objeto JSON. |
| `iac-code mcp list` | Lista servidores configurados, scopes, transportes e status de aprovação. |
| `iac-code mcp get` | Imprime uma configuração de servidor com segredos mascarados. |
| `iac-code mcp remove` | Remove um servidor de um scope persistido. |
| `iac-code mcp approve` | Aprova um servidor de projeto `.mcp.json`. |
| `iac-code mcp reject` | Rejeita um servidor de projeto `.mcp.json`. |
| `iac-code mcp reset-project-choices` | Limpa escolhas salvas de aprovação de projeto. |
| `iac-code mcp auth` | Inicia autenticação OAuth para um servidor. |
| `iac-code mcp reset-auth` | Exclui tokens OAuth e client secret armazenados para um servidor. |

Quando `--scope` é omitido, o IaC Code grava em `local` dentro de um projeto e em `user` fora de um projeto.

## Servidores Stdio

Servidores stdio iniciam um comando local:

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

O campo `type` pode ser omitido quando `command` está presente. O IaC Code passa um ambiente herdado seguro mais o `env` do servidor. No Windows, prefira `cmd /c npx` em vez de `npx` puro para servidores baseados em Node.

## Servidores HTTP e SSE

Servidores remotos exigem `type` e `url`:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

Use `type: "sse"` para servidores SSE. Headers estáticos são suportados. Comandos dinâmicos `headersHelper` são rejeitados porque exigem um design separado de execução confiável.

## Expansão de ambiente

Valores string suportam:

```text
${VAR}
${VAR:-default-value}
```

Variáveis ausentes sem padrão geram um aviso MCP e o servidor afetado é ignorado. A expansão de ambiente é aplicada recursivamente a strings dentro de listas e objetos.

Não armazene segredos em texto claro em headers ou valores env. Use referências a variáveis de ambiente ou armazenamento secreto OAuth.

## Aprovação de projeto

Um `.mcp.json` de projeto pode ser commitado no repositório, então o IaC Code não confia nele automaticamente.

Na inicialização do REPL interativo, ele pergunta:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Pressionar Enter mantém o padrão `N` e rejeita aquela configuração exata de servidor de projeto. Digite `y` ou `yes` para aprovar. A aprovação é armazenada localmente no diretório de configuração do IaC Code e inclui o caminho do workspace, o caminho do arquivo de projeto, o nome do servidor e a assinatura da configuração. Se a configuração do servidor em `.mcp.json` mudar, a aprovação é invalidada e o servidor volta a ficar pending.

Inicializações headless, ACP e A2A nunca fazem perguntas interativas de aprovação. Servidores de projeto pendentes são ignorados e relatados como avisos.
