---
title: Operações comuns
description: Usar configurações, menus de conversa e controles de entrada no Desktop e Web.
---

# Operações comuns

Os passos valem para Desktop e Web, que usam a mesma interface de conversa.

## Iniciar uma conversa

1. Abra o Desktop ou execute `iac-code` em um terminal interativo para abrir o Web.

2. Clique em **Nova conversa** na barra lateral. Abaixo da entrada, escolha o diretório do projeto com os arquivos existentes ou onde salvar os modelos gerados.

3. Antes da primeira mensagem, escolha **Modo normal** ou **Modo pipeline**. Para pipeline, selecione também o fluxo desejado.

4. Descreva a tarefa e envie. A conversa aparece sob o projeto na barra lateral e pode ser continuada ali.

## Referenciar arquivos ou anexar imagens

1. Abra uma conversa do projeto que contém o arquivo.

2. Digite `@` e parte do nome do arquivo e selecione o resultado. Uma referência aparece junto à entrada. Para uma imagem, use o controle de anexos.

3. Acrescente a instrução, por exemplo “Verifique este modelo ROS e explique seus parâmetros”, confira as referências e envie.

## Escolher o modelo e o raciocínio

1. Siga primeiro a [configuração do modelo](../configuration/llm-providers.md).

2. Na conversa, abra o controle de modelo abaixo da entrada e escolha um provedor e modelo disponíveis.

3. Se o modelo permitir, escolha o esforço de raciocínio e ative ou desative **Pensando** para esta conversa.

4. Confira o modelo exibido antes de enviar a próxima solicitação. As opções de raciocínio dependem do modelo.

## Responder a uma aprovação de ferramenta

1. Antes de enviar, abra o controle de permissões na entrada e escolha o modo desejado.

2. Quando aparecer um cartão de aprovação, confira a ferramenta e a operação proposta, incluindo o arquivo ou a API de nuvem de destino.

3. Aprove ou negue no cartão. A aprovação permite continuar a operação; a negação é devolvida ao assistente. Responda separadamente aos cartões de perguntas.

## Editar instruções e gerenciar a memória

1. Abra **Configurações → Memória** e escolha o projeto no topo.

2. Em **Memória do projeto**, escreva as instruções específicas desse projeto e clique no **Salvar** dessa seção.

3. Em **Memória do usuário**, escreva preferências válidas entre projetos e clique no **Salvar** correspondente. Confira a confirmação após cada edição.

4. Ative ou desative **Memória automática → Registro automático** para permitir o registro de fatos úteis durante o trabalho. O ajuste é salvo automaticamente.

5. Pesquise e leia entradas na **Biblioteca de memória**. Para remover uma entrada obsoleta, use seu controle de exclusão e confirme.

## Mudar o idioma ou a aparência

1. Abra **Configurações → Geral**.

2. Selecione o **Idioma**. A escolha é salva automaticamente e a página é recarregada.

3. Clique em um tema em **Esquema de cores**. A interface muda e salva a escolha automaticamente.

## Outras tarefas

Veja os passos em [Sessões](./sessions.md), [Skills](./skills.md) e [Início rápido do MCP](../mcp/quick-start.md).

## Atalhos opcionais

Em uma conversa normal existente, `/` mostra alguns atalhos:

| Atalho | Finalidade |
|---|---|
| `/status` | Mostrar o status atual da sessão. |
| `/compact` | Resumir a conversa para reduzir o contexto. |
| `/mcp` | Mostrar o status MCP; gerencie conexões em **Plugins → MCP**. |

Pipeline oculta `/compact`; esses atalhos não aparecem no rascunho de uma nova conversa. Selecione um skill nas sugestões, acrescente o pedido e envie. Veja [Skills](./skills.md).
