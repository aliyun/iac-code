---
sidebar_position: 2
title: Configuração MCP
description: Configure servidores MCP com comandos CLI, arquivos de configuração, arquivos de projeto e sessões ACP.
---

# Configuração MCP

Os MCP servers sao configurados no `mcpServers` object. IaC Code oferece um core schema compativel com Claude Code para `stdio`, `http`, `sse`, and URL-only `ws` servers.

## Inicio rapido

Para um servidor MCP HTTP remoto como o Yuque, adicione o servidor com a forma de URL posicional e depois inicie OAuth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Para wrappers stdio como `mcp-remote`, coloque o comando subprocess depois de `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

O Código IaC lê servidores MCP destas fontes:

| Fonte | Escopo | Arquivo ou ponto de entrada | Modelo de confiança |
|---|---|---|---|
| Configurações do usuário | `user` | `~/.iac-code/settings.yml` ou `IAC_CODE_CONFIG_DIR/settings.yml` | Confiável pelo usuário atual. |
| Configurações locais do projeto | `local` | `<workspace>/.iac-code/settings.local.yml` | Privado para o checkout local. |
| Arquivo MCP do projeto | `project` | `<workspace>/.mcp.json` | Compartilhado com o projeto e requer aprovação local. |
| Configuração da sessão ACP | `session` | `mcpServers` passado por um cliente ACP | Aplica-se somente ao tempo de execução da sessão ACP. |

A precedência é usuário, projeto, local e sessão. As fontes posteriores substituem as fontes anteriores pelo nome do servidor. Configurações equivalentes também são desduplicadas pela assinatura de conteúdo.

Os arquivos `.mcp.json` do projeto são descobertos desde a raiz do espaço de trabalho até o diretório atual. Os arquivos do projeto filho substituem os arquivos pai pelo nome do servidor.

## CLI Commands

Use `iac-code mcp` para gerenciar a configuração persistente do MCP:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

Servidores HTTP remotos podem ser adicionados com o formulário de URL posicional no estilo Claude:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Servidores SSE e WebSocket usam o mesmo formulário de URL posicional com seu próprio transporte:

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

Para wrappers stdio como `mcp-remote`, coloque o comando subprocess após `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Comandos disponíveis:

| Comando | Finalidade |
|---|---|
| `iac-code mcp add` | Adicione um servidor a partir de sinalizadores CLI estruturados. |
| `iac-code mcp add-json` | Adicione um servidor de um objeto JSON. |
| `iac-code mcp list` | Lista servers configurados, scopes, transports e status de aprovação sem conectar. |
| `iac-code mcp list --config-only` | Alias da listagem de configuração padrão. |
| `iac-code mcp list --check` | Conecta brevemente e mostra diagnostics de health limitados. |
| `iac-code mcp get` | Imprima uma configuração de servidor editada sem conectar. |
| `iac-code mcp get --config-only` | Imprima uma configuração de servidor editada sem conectar. |
| `iac-code mcp get --check` | Conecte-se brevemente e mostre diagnósticos de integridade limitados para um servidor. |
| `iac-code mcp remove` | Remova um servidor de um escopo persistente. |
| `iac-code mcp approve` | Aprovar um servidor `.mcp.json` do projeto. |
| `iac-code mcp reject` | Rejeite um servidor `.mcp.json` do projeto. |
| `iac-code mcp reset-project-choices` | Limpe as opções de aprovação de projeto armazenadas. |
| `iac-code mcp auth` | Inicie a autenticação OAuth para um servidor. |
| `iac-code mcp reset-auth` | Exclua tokens OAuth armazenados e segredo do cliente para um servidor. |
| `iac-code mcp reconnect` | Reconecte um servidor ou todos os servidores persistentes com `--all`. |
| `iac-code mcp disable` | Desative um servidor persistente sem editar a configuração do projeto compartilhado. |
| `iac-code mcp enable` | Reative um servidor persistente. |

## Opcoes de comando

O option set abaixo segue `iac-code mcp <command> --help`:

| Comando | Opcoes |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options; somente `--help`. |
| `iac-code mcp reject` | No command-specific options; somente `--help`. |
| `iac-code mcp reset-project-choices` | No command-specific options; somente `--help`. |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

Quando `--scope` é omitido, o código IaC grava em `local` dentro de um projeto e em `user` fora de um projeto.

Para comandos que operam em um servidor persistente existente, o Código IaC pode encontrar um servidor exclusivo em escopos persistentes quando `--scope` é omitido. Se o mesmo nome existir em vários escopos, o comando falhará com os comandos `--scope` exatos para desambiguar.

## Gerenciador MCP interativo

No REPL interativo, `/mcp` abre um gerenciador MCP em tela cheia. Ele agrupa servidores por origem e mostra estado de conexão, estado de autenticação, diagnósticos de configuração, detalhes de falha e local configurado.

No gerenciador, você pode inspecionar tools, resources e prompts de um servidor conectado; autenticar, reautenticar ou limpar a autenticação de servidores remotos; reconectar servidores; ativar ou desativar servidores persistentes; aprovar ou rejeitar servidores `.mcp.json` de projeto; e remover entradas persistentes. Fluxos OAuth mostram a URL de autorização, permitem copiá-la e aceitam uma URL de callback ou código de autorização colado quando o redirecionamento do navegador não consegue alcançar o listener callback local.

`/mcp enable <name>`, `/mcp disable <name>` e `/mcp reconnect <name>` executam ações rápidas sem abrir o gerenciador. Se `/mcp` chegar por stdin canalizado ou outra entrada não TTY, IaC Code imprime uma mensagem informando que um terminal é necessário; use `iac-code mcp <command>` para automação não interativa.

## Stdio Servers

Stdio servers launch a local command:

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

O campo `type` pode ser omitido quando `command` estiver presente. O código IaC passa um ambiente herdado seguro mais o servidor `env`. No Windows, prefira `cmd /c npx` em vez de `npx` simples para servidores baseados em Node.

## HTTP and SSE Servers

Servidores remotos requerem `type` e `url`:

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

Use `type: "sse"` para servidores SSE. Cabeçalhos estáticos são suportados com sintaxe CLI `KEY=VALUE` ou `Name: Value`.

Cabeçalhos dinâmicos podem ser fornecidos por `headersHelper`:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

O helper deve imprimir um JSON object cujas chaves e valores sejam strings. Cabeçalhos dinâmicos substituem cabeçalhos estáticos com o mesmo nome. O IaC Code executa helpers sem shell, sem stdin, com ambiente herdado mínimo, o diretório da fonte de configuração como cwd, timeout de 5 segundos e diagnostics de stderr redigidos. A string de comando `headersHelper` não expande variáveis de ambiente; as variáveis referenciadas são passadas no ambiente do helper, e o helper deve lê-las por conta própria. Helpers em project `.mcp.json` exigem aprovação do projeto antes de executar.

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

O transporte WebSocket do SDK do MCP instalado aceita apenas uma URL. O código IaC rejeita configurações de WebSocket que também definem `headers`, `headersHelper` ou `oauth`.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Variáveis ausentes sem default produzem um MCP warning e o server afetado é ignorado. A expansão de ambiente se aplica recursivamente a strings em listas e objetos, exceto à string de comando `headersHelper`, que permanece literal e recebe as variáveis referenciadas pelo ambiente do helper.

Não armazene segredos de texto simples em cabeçalhos ou valores de ambiente. Use referências de variáveis ​​de ambiente ou armazenamento secreto OAuth.

## Project Approval

O projeto `.mcp.json` pode ser confirmado em um repositório, portanto o Código IaC não confia nele automaticamente.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Pressionar Enter mantém o `N` padrão e rejeita a configuração exata do servidor do projeto. Digite `y` ou `yes` para aprová-lo. A aprovação é armazenada localmente no diretório de configuração do código IaC e inclui o caminho do espaço de trabalho, o caminho do arquivo do projeto, o nome do servidor e a assinatura de configuração. Se a configuração do servidor `.mcp.json` for alterada, a aprovação será invalidada e o servidor ficará pendente novamente.

As startups Headless, ACP e A2A nunca fazem perguntas de aprovação interativas. Servidores de projetos pendentes são ignorados e relatados como avisos.

## Disabled Servers

`iac-code mcp disable <name>` armazena uma entrada privada de estado desativado no diretório de configuração do código IaC. Para servidores com escopo de projeto, isso não altera o arquivo `.mcp.json` compartilhado. As entradas desabilitadas são codificadas por escopo, arquivo de origem, nome do servidor e assinatura de configuração, portanto, alterar a configuração do servidor invalida o estado obsoleto desabilitado.
