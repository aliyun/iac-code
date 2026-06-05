---
title: Referencia do protocolo
description: Referencia completa de metodos e eventos do protocolo ACP para integracao com o iac-code.
sidebar_position: 3
---

# Referencia do protocolo

Este documento fornece uma referencia completa dos metodos e eventos de streaming do ACP (Agent Client Protocol) expostos pelo servidor iac-code.

## Visao geral do ciclo de vida

Uma sessao ACP tipica segue este fluxo:

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** — Handshake. Negocia a versao do protocolo e descobre as capacidades do servidor.
2. **session/new** — Cria uma nova sessao com um runtime de agente independente.
3. **session/prompt** — Envia entrada do utilizador; recebe eventos de streaming ate uma resposta final.
4. **session/close** — Libera a sessao e seus recursos.

As sessoes tambem podem ser carregadas do historico (`session/load`) ou retomadas (`session/resume`) em vez de criar novas.

---

## Metodos

### initialize

Handshake do protocolo. Deve ser a primeira chamada em cada conexao.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `protocolVersion` | integer | Sim | Versao do protocolo solicitada (atualmente `1`) |
| `clientInfo` | object | Nao | Nome e versao do cliente |
| `clientCapabilities` | object | Nao | Capacidades suportadas pelo cliente |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `protocolVersion` | integer | Versao do protocolo negociada |
| `agentCapabilities` | object | Capacidades do servidor (veja abaixo) |
| `agentInfo` | object | Nome e versao do servidor |
| `authMethods` | array | Metodos de autenticacao disponiveis (vazio se usar credenciais integradas) |

**Capacidades do agente**

| Capacidade | Valor | Significado |
|-----------|-------|---------|
| `loadSession` | `true` | Suporta restauracao de sessoes a partir do historico |
| `promptCapabilities.embeddedContext` | `true` | Aceita conteudo de recurso incorporado em prompts |
| `promptCapabilities.image` | `false` | Entrada de imagem nao suportada (degrada para marcador de texto) |
| `promptCapabilities.audio` | `false` | Entrada de audio nao suportada (degrada para marcador de texto) |
| `sessionCapabilities.list` | `{}` | Suporta listagem de sessoes |
| `sessionCapabilities.close` | `{}` | Suporta fechamento de sessoes |

---

### session/new

Cria uma nova sessao com um runtime de agente independente, registro de ferramentas e contexto de LLM.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `cwd` | string | Sim | Caminho absoluto para o diretorio de trabalho |
| `mcpServers` | object | Nao | Configuracao de servidor MCP (aceito mas ainda nao funcional) |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `sessionId` | string | Identificador unico da sessao para chamadas subsequentes |
| `modes` | object | Modos disponiveis e modo atual |
| `models` | object | Modelos disponiveis e modelo atual |

:::note
Cada sessao cria um AgentLoop independente. Multiplas sessoes podem ser executadas simultaneamente, mas cada uma consome uma conexao LLM.
:::

---

### session/load

Carrega uma sessao previamente persistida do disco, restaurando seu historico de mensagens.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `cwd` | string | Sim | Caminho do diretorio de trabalho |
| `sessionId` | string | Sim | ID da sessao a carregar |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `models` | object | Modelos disponiveis e estado do modelo atual |
| `modes` | object | Modos disponiveis e estado do modo atual |

:::note
Carregar uma sessao le o historico de `~/.iac-code/sessions/`, repara automaticamente mensagens interrompidas e injeta o historico num novo AgentLoop.
:::

---

### session/fork

Bifurca uma sessao existente para criar uma ramificacao independente com o mesmo historico.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `cwd` | string | Sim | Caminho do diretorio de trabalho |
| `sessionId` | string | Sim | ID da sessao a bifurcar |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `sessionId` | string | Novo ID de sessao para a ramificacao bifurcada |
| `models` | object | Modelos disponiveis e estado do modelo atual |
| `modes` | object | Modos disponiveis e estado do modo atual |

---

### session/resume

Retoma ou reconecta a uma sessao existente. Carrega automaticamente o historico se necessario.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `cwd` | string | Sim | Caminho do diretorio de trabalho |
| `sessionId` | string | Sim | ID da sessao a retomar |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `models` | object | Modelos disponiveis e estado do modelo atual (opcional) |
| `modes` | object | Modos disponiveis e estado do modo atual (opcional) |

:::note
Ao contrario de `session/new`, a resposta nao inclui um campo `sessionId` pois o cliente ja conhece o ID da sessao a partir da requisicao.
:::

---

### session/prompt

Envia entrada do utilizador e aciona respostas do agente em streaming.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `sessionId` | string | Sim | ID da sessao alvo |
| `prompt` | array | Sim | Array de blocos de conteudo (veja Tipos de blocos de conteudo abaixo) |

**Tipos de blocos de conteudo**

| Tipo | Descricao |
|------|-------------|
| `TextContentBlock` | Entrada de texto simples do utilizador |
| `EmbeddedResourceContentBlock` | Conteudo de arquivo incorporado inline |
| `ResourceContentBlock` | Referencia de link de recurso |
| `ImageContentBlock` | Imagem (degrada para marcador de texto `[image: mime/type]`) |
| `AudioContentBlock` | Audio (degrada para marcador de texto `[audio: mime/type]`) |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `stopReason` | string | Por que o prompt foi concluido (veja Motivos de parada) |
| `usage` | object | Uso de tokens: `inputTokens`, `outputTokens`, `totalTokens` |

**Motivos de parada**

| Valor | Significado |
|-------|---------|
| `end_turn` | Modelo concluiu normalmente |
| `max_turn_requests` | Atingiu o limite maximo de loop de chamadas de ferramentas |
| `max_tokens` | Limite de tokens de saida atingido |
| `cancelled` | Cliente cancelou o prompt |
| `refusal` | Modelo recusou responder |

:::note
Durante a execucao, o servidor envia notificacoes `session/update` com eventos de streaming antes de retornar a resposta final.
:::

---

### session/cancel

Cancela uma tarefa de prompt em execucao.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `sessionId` | string | Sim | Sessao com o prompt em execucao |

**Comportamento**

- Para de consumir eventos do stream
- Ferramentas em execucao nao sao terminadas forcosamente, mas os resultados sao descartados
- A chamada `prompt` pendente retorna com `stopReason: "cancelled"`

---

### session/close

Fecha uma sessao e libera seus recursos.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `sessionId` | string | Sim | Sessao a fechar |

**Comportamento**

- Sessao removida da memoria
- O historico persistido permanece no disco
- Chamadas `prompt` subsequentes a esta sessao retornam um erro

---

### sessions/list

Lista todas as sessoes persistidas para um determinado diretorio de trabalho.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `cwd` | string | Sim | Diretorio de trabalho para delimitar a listagem |

**Campos da resposta**

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `sessions` | array | Lista de objetos de sessao com `sessionId` e metadados |

---

### config/set

Define dinamicamente uma opcao de configuracao para uma sessao.

**Parametros da requisicao**

| Campo | Tipo | Obrigatorio | Descricao |
|-------|------|----------|-------------|
| `sessionId` | string | Sim | Sessao alvo |
| `configId` | string | Sim | Chave de configuracao a definir |
| `value` | any | Sim | Novo valor |

---

## Eventos de streaming

Durante a execucao de `session/prompt`, o servidor envia notificacoes `session/update` contendo dados de eventos de streaming.

### Formato do evento

Cada notificacao `session/update` carrega um objeto de atualizacao com um tipo especifico:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### Mapeamento de tipos de evento

| Evento interno | Tipo de atualizacao ACP | Descricao |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | Saida de texto incremental do agente |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | Conteudo de raciocinio/pensamento do modelo |
| `ToolUseStartEvent` | `ToolCallStart` | Inicio da invocacao de ferramenta |
| `ToolResultEvent` | `ToolCallProgress` | Resultado da ferramenta (concluido ou falhou) |
| `CompactionEvent` | `AgentMessageChunk` | Notificacao de compactacao de contexto |
| `ErrorEvent` | `AgentMessageChunk` | Informacao de erro |

### Ciclo de vida da chamada de ferramenta

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, raw_input=tool input)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← sucesso
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← falha
```

**Mapeamento de tipo de ferramenta**

| Ferramenta | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| Outros | `other` |

---

## Solicitacoes de permissao

Antes de executar ferramentas de alto risco, o iac-code envia um callback `request_permission` ao cliente.

### Categorias de permissao de ferramentas

| Categoria | Ferramentas | Auto-aprovada |
|----------|-------|-------------|
| Somente leitura | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | Sim |
| Escrita | `write_file`, `edit_file` | Nao — requer aprovacao |
| Execucao | `bash`, `agent` | Nao — requer aprovacao |

### Evento request_permission

O servidor envia um callback `request_permission` com:

| Campo | Tipo | Descricao |
|-------|------|-------------|
| `options` | array | Opcoes de permissao disponiveis |
| `sessionId` | string | Sessao solicitando permissao |
| `toolCall` | object | Detalhes da chamada de ferramenta (titulo, tipo, entrada) |

### Opcoes de permissao

| ID da opcao | Significado |
|-----------|---------|
| `allow_once` | Permitir esta invocacao especifica |
| `allow_always` | Permitir todas as futuras chamadas desta ferramenta nesta sessao quando a ferramenta oferece permissao global; nao e oferecido para `bash` por padrao |
| `allow_rule:<rules>` | Permitir futuras chamadas que correspondam as regras sugeridas nesta sessao |
| `deny_rule:<rules>` | Negar futuras chamadas que correspondam as regras sugeridas nesta sessao |
| `reject_once` | Negar esta invocacao especifica |
| `reject_always` | Negar todas as futuras chamadas desta ferramenta nesta sessao |

### Formato de resposta

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

Ou para negar:

```json
{
  "outcome": "denied"
}
```

| Resposta do cliente | Comportamento da ferramenta |
|----------------|---------------|
| `AllowedOutcome` | Ferramenta executa normalmente |
| `DeniedOutcome` | Ferramenta ignorada; modelo recebe erro "Permission denied." |

---

## Tratamento de erros

### Formato RequestError

Os erros seguem o formato de erro JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### Codigos de erro comuns

| Codigo | Nome | Descricao |
|------|------|-------------|
| `-32700` | Erro de analise | JSON invalido |
| `-32600` | Requisicao invalida | JSON-RPC malformado |
| `-32601` | Metodo nao encontrado | Metodo desconhecido |
| `-32602` | Parametros invalidos | Parametros ausentes ou invalidos (ex.: ID de sessao desconhecido) |
| `-32603` | Erro interno | Falha no lado do servidor |
