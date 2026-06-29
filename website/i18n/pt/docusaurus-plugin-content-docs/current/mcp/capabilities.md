---
sidebar_position: 3
title: Ferramentas, recursos, prompts e skills
description: Entenda como capacidades MCP aparecem dentro do IaC Code.
---

# Ferramentas, recursos, prompts e skills

Servidores MCP conectados podem expor quatro tipos de capacidades ao IaC Code.

## Ferramentas

Cada ferramenta MCP se torna uma ferramenta do IaC Code:

```text
mcp__<server>__<tool>
```

Descrições de ferramentas e schemas JSON de entrada vêm do servidor MCP. O IaC Code encaminha a entrada da ferramenta do modelo ao servidor MCP e depois converte blocos de conteúdo MCP em um resultado normal de ferramenta.

Anotações MCP são respeitadas quando possível:

| Anotação MCP | Comportamento no IaC Code |
|---|---|
| `readOnlyHint: true` | A ferramenta é tratada como somente leitura e segura para concorrência. |
| `destructiveHint: true` | A ferramenta é tratada como destrutiva nas decisões de permissão. |

Ferramentas MCP ainda passam pelo sistema de permissões existente do IaC Code. Configure a política com settings normais de `permissions` ou flags CLI como `--allowed-tools`, `--disallowed-tools` e `--permission-mode`.

Notificações de progresso MCP são exibidas no render interativo, na saída de progresso headless, nas atualizações de progresso ACP e nos metadados de ferramenta A2A.

## Resultados de ferramentas e artefatos

O IaC Code converte blocos de conteúdo MCP em texto visível ao modelo:

| Conteúdo MCP | Resultado do IaC Code |
|---|---|
| Conteúdo de texto | Incluído diretamente no resultado da ferramenta. |
| `structuredContent` | Renderizado como JSON formatado em uma seção de conteúdo estruturado. |
| Recursos de texto | Renderizados com proveniência de servidor e URI. |
| `resource_link` | Renderizado como link de recurso com URI e tipo MIME. |
| Dados de imagem, áudio e blob | Armazenados como arquivos privados de artefato e referenciados por id de artefato. |

Artefatos binários são armazenados em:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

O modelo vê o id do artefato e metadados, não dados base64 brutos.

## Recursos

Quando qualquer servidor conectado expõe recursos, o IaC Code registra duas ferramentas globais:

| Ferramenta | Finalidade |
|---|---|
| `list_mcp_resources` | Lista recursos de servidores MCP conectados. Pode filtrar por nome de servidor. |
| `read_mcp_resource` | Lê um recurso por `server` e `uri`. |

Linhas de recurso incluem nome do servidor, URI, nome de recurso opcional e tipo MIME opcional.

## Prompts

Prompts MCP se tornam comandos slash:

```text
/mcp__<server>__<prompt> key=value
```

Ao invocar, o IaC Code chama MCP `prompts/get`, renderiza as mensagens de prompt retornadas, injeta o prompt renderizado na conversa e deixa o modelo continuar. Argumentos de prompt podem ser passados como:

```text
template_name=prod-vpc region=cn-hangzhou
```

ou como JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Argumentos obrigatórios são validados antes da chamada MCP. Valores entre aspas são suportados, incluindo caminhos Windows com barras invertidas.

## Skills

Recursos MCP com URIs `skill://` se tornam comandos de skill:

```text
$mcp__<server>__<skill>
```

O IaC Code lê o recurso de skill remoto, analisa o frontmatter e o registra como um comando normal de skill. Skills MCP remotos têm limites de segurança:

- `allowed_tools` remotos são limpos.
- Regras remotas de autoativação por paths são limpas.
- Corpo e descrição do skill remoto têm limites de tamanho.
- Se o skill remoto conflitar com um comando existente, ele é ignorado com um aviso MCP.

Recursos de skill MCP podem ser lidos durante a inicialização para que o comando seja registrado antes da invocação pelo usuário.

## Atualizações dinâmicas

Se um servidor MCP envia `tools/list_changed`, `resources/list_changed` ou `prompts/list_changed`, o IaC Code atualiza a lista de capacidades afetada e o registro de ferramentas ou comandos. Falhas de atualização são relatadas como avisos MCP e não interrompem a sessão ativa.
