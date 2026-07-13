---
sidebar_position: 3
title: Ferramentas, recursos, prompts e habilidades
description: Entenda como as capacidades MCP aparecem dentro do IaC Code.
---

# Ferramentas, recursos, prompts e habilidades

MCP servers conectados podem expor quatro tipos de capabilities ao IaC Code.

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

As descrições das ferramentas e os esquemas de entrada JSON vêm do servidor MCP. O Código IaC encaminha a entrada da ferramenta do modelo para o servidor MCP e, em seguida, converte os blocos de conteúdo do MCP em um resultado de ferramenta normal.

Os prompts de permissão e os metadados de auditoria incluem o nome do servidor MCP, o nome da ferramenta original, o nome da ferramenta pública normalizada e anotações somente leitura/destrutivas.

As anotações da ferramenta MCP são respeitadas sempre que possível:

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | A ferramenta é tratada como somente leitura e segura para simultaneidade. |
| `destructiveHint: true` | A ferramenta é tratada como destrutiva para decisões de permissão. |

As ferramentas MCP ainda passam pelo sistema de permissão existente do Código IaC. Configure a política de permissão com configurações normais de `permissions` ou sinalizadores CLI, como `--allowed-tools`, `--disallowed-tools` e `--permission-mode`.

As notificações de progresso do MCP aparecem em renderização interativa, saída de progresso headless, atualizações de progresso da ferramenta ACP e metadados da ferramenta A2A.

## Tool Results and Artifacts

O Código IaC converte blocos de conteúdo MCP em texto visível ao modelo:

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; texto grande é salvo como artifact privado `.txt`, `.json` ou `.md`. |
| `structuredContent` | Renderizado como JSON formatado em uma seção de conteúdo estruturado. |
| Recursos de texto | Renderizado com origem de servidor e URI. |
| `resource_link` | Renderizado como um link de recurso com URI e tipo MIME. |
| Dados de imagem, áudio e blob | Armazenados como arquivos de artefatos privados e referenciados pelo ID do artefato. |

Os artefatos binários são armazenados no diretório de resultados da ferramenta MCP de propriedade da sessão para sessões v2:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

Sessões legadas sem um marcador de layout compatível continuam a usar:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. Artifacts de texto grande incluem um path so the full output can be read without flooding the conversation.

## Resources

Quando qualquer servidor conectado expõe recursos, o Código IaC registra duas ferramentas globais:

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | Lista recursos de servidores MCP conectados. Opcionalmente, filtre por nome do servidor. |
| `read_mcp_resource` | Lê um recurso por `server` e `uri`. |

As linhas de recursos incluem nome do servidor, URI, nome do recurso opcional e tipo MIME opcional.

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

Quando invocado, o Código IaC chama o MCP `prompts/get`, renderiza as mensagens de prompt retornadas, injeta o prompt renderizado na conversa e permite que o modelo continue. Argumentos de prompt podem ser passados como:

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Os argumentos de prompt necessários são validados antes da chamada do MCP. Os valores entre aspas são suportados, incluindo caminhos do Windows com barras invertidas.

## Skills

Recursos MCP com URIs `skill://` tornam-se comandos de habilidade:

```text
$mcp__<server>__<skill>
```

O Código IaC lê o recurso de habilidade remota, analisa o frontmatter e o registra como um comando de habilidade normal. As habilidades remotas do MCP são limitadas pela segurança:

- Remote `allowed_tools` are cleared.
- As regras de caminho de disparo automático remoto são apagadas.
- O corpo da habilidade remota e o comprimento da descrição são limitados.
- Se a habilidade remota entrar em conflito com um comando existente, ela será ignorada com um aviso do MCP.

Os recursos da habilidade MCP podem ser lidos durante a inicialização para que o comando possa ser registrado antes que o usuário o invoque.

Quando não há conflito de comando, as habilidades do MCP também recebem um alias de compatibilidade:

```text
$<server>:<skill>
```

Por exemplo, `$mcp__yuque__search` e `$yuque:search` podem resolver para a mesma habilidade remota.

## Server Instructions (instruções do servidor)

Se um servidor conectado retornar `instructions` da inicialização, o Código IaC as injeta no prompt do agente como uma seção dedicada de instruções do servidor MCP. Estas instruções são tratadas como orientação no escopo do servidor e não substituem as instruções do projeto local.

## Elicitation (solicitações interativas)

Sessões interativas podem rotear solicitações de MCP elicitation para o usuário. A elicitation em modo URL pode pedir que o usuário conclua um fluxo de URL externo e então repetir o MCP tool call original até um limite definido. Contextos não interativos cancelam a elicitation com segurança.

## Dynamic Updates

Se um servidor MCP enviar `tools/list_changed`, `resources/list_changed` ou `prompts/list_changed`, o código IaC atualizará a lista de capacidades afetadas e atualizará o registro de ferramentas ou comandos. As falhas de atualização são relatadas como avisos do MCP e não interrompem a sessão ativa.
