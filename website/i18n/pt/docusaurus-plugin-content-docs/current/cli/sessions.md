---
title: Sessões
description: Persistir e retomar conversas entre execuções.
---

# Sessões

Web, Desktop e headless compartilham o armazenamento de sessões. As conversas são salvas automaticamente no diretório de configuração do runtime.

## Web e Desktop

### Continuar uma conversa

1. Abra a conversa sob seu projeto na barra lateral ou procure pelo título em **Pesquisar**.

2. Confira as mensagens e os arquivos salvos.

3. Digite e envie o próximo pedido. Para uma tarefa independente, use **Nova conversa**.

### Renomear ou fixar uma conversa

1. Abra a conversa e o menu **…**.

2. Escolha **Renomear conversa**, digite o título e clique em **Salvar**. A barra lateral mostra o novo título.

3. Para encontrá-la facilmente, use a opção de fixar no mesmo menu. Depois, a ação inversa remove a fixação.

### Arquivar e restaurar uma conversa

1. Escolha **Arquivar conversa** no menu **…**. Ela sai da lista ativa.

2. Abra **Configurações → Conversas arquivadas** e encontre-a com a pesquisa e o filtro de projeto.

3. Clique em **Desarquivar**, volte à barra lateral e abra a conversa para continuar.

:::note

Conversas criadas por CLI, A2A ou ACP ficam ocultas por padrão. Para consultá-las, ative a opção de somente leitura em **Configurações → Geral → Visibilidade de sessões externas**. Compartilhar o armazenamento não permite editá-las no Web/Desktop.

:::

## Continuar em modo headless

Execute no diretório do projeto da sessão. `--resume` continua uma sessão específica e `--continue` a mais recente do projeto. As formas curtas são `-r` e `-c`, que não podem ser combinadas.

```bash
iac-code --resume deploy-prod --prompt "Gerar um modelo de VPC"
iac-code --continue --prompt "Gerar um modelo de VPC"
```

## Outros projetos

No Web ou Desktop, abra a conversa sob seu projeto na barra lateral. Em headless, entre primeiro no diretório desse projeto:

```bash
cd /path/to/project
iac-code --resume <session-id> --prompt "Gerar um modelo de VPC"
```

## Conversas interrompidas

Reabra a conversa salva para consultar o histórico e continuar. A recuperação concilia chamadas de ferramentas incompletas. O progresso e as decisões pendentes seguem a [recuperação de Pipeline](../automation/pipeline-mode.md). Execuções únicas com `--prompt` não executam pipelines.
