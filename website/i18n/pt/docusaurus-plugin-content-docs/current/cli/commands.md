---
title: Comandos slash
description: Referencia completa dos comandos interativos integrados.
---

# Comandos slash

Os comandos slash controlam o IaC Code de dentro de uma sessao interativa. Digite `/` para ver os comandos disponiveis e continue digitando para filtrar a lista. Um comando so e reconhecido quando aparece no inicio da sua mensagem.

A listagem de `/` inclui tanto comandos integrados quanto skills que voce configurou. Para restringir as sugestoes apenas a skills, use `$` em vez disso — `$<nome>` lista e invoca skills exclusivamente, e digitar `$` seguido do nome de um comando integrado (por exemplo `$help`) imprime um erro apontando para o equivalente `/`.

O texto apos o nome do comando e passado como argumentos. Na tabela abaixo, `<arg>` indica um argumento obrigatorio e `[arg]` indica um argumento opcional.

| Comando | Finalidade |
|---|---|
| `/auth` | Configura o acesso ao provedor de modelos e as credenciais da Alibaba Cloud atraves do fluxo de autenticacao interativo. Use ao configurar o IaC Code pela primeira vez, alterar chaves de API, trocar de provedor ou atualizar o acesso a nuvem. Alias: `/login`. |
| `/clear` | Limpa o historico de conversa atual e redefine o gerenciador de contexto ativo. No modo interativo, tambem limpa a tela do terminal e re-renderiza o banner de boas-vindas. Use quando quiser iniciar uma nova solicitacao sem sair do REPL. |
| `/compact` | Resume a conversa atual para reduzir o uso de contexto, preservando as interacoes recentes. Use apos uma sessao longa quando quiser continuar trabalhando com menos contexto acumulado. Se a conversa estiver vazia ou for muito curta, o comando informa que nao ha nada para compactar. |
| `/debug [on\|off\|status]` | Inspeciona ou altera o log de depuracao em tempo de execucao para a sessao ativa. `/debug` e `/debug status` mostram se o log esta habilitado e, quando habilitado, o caminho do arquivo de log. `/debug on` habilita o log para a sessao atual. `/debug off` desabilita-o. |
| `/effort [level]` | Mostra ou altera o esforco de raciocinio do modelo ativo quando o modelo selecionado suporta controle de esforco. Com um nivel, aplica o valor solicitado se for valido para o modelo. Sem nivel, abre um seletor interativo no REPL ou imprime o esforco atual em contextos nao interativos. |
| `/exit` | Sai do REPL interativo. Aliases: `/quit`, `/q`. |
| `/help` | Mostra os comandos disponiveis e atalhos de teclado comuns dentro do REPL. Alias: `/?`. |
| `/memory [<nome>\|search <consulta>\|delete <nome>\|help]` | Listar, ver, pesquisar ou excluir memórias salvas. A criação de memórias em linguagem natural continua sendo feita pelo assistente por meio da ferramenta de memória quando você pede que ele se lembre de algo. |
| `/model [model_name]` | Mostra ou troca o modelo ativo. Com `model_name`, troca diretamente para esse modelo no provedor ativo. Sem argumento, abre um seletor interativo de modelos quando um provedor esta configurado, ou imprime o modelo atual quando nao ha UI de console disponivel. |
| `/rename <nome>` | Nomear a sessão atual. Os nomes aparecem no banner de boas-vindas, na dica de saída e no seletor de `/resume`, e podem ser usados com `/resume` ou `--resume` quando identificam uma sessão de forma única. |
| `/resume [id-da-sessao\|prefixo-unico-de-id\|nome-unico-da-sessao]` | Retomar uma sessão anterior. Com um argumento, o IaC Code resolve-o como ID exato, prefixo único de ID ou nome único de sessão. Sem argumento, abre o seletor interativo de sessões. Sessões de outros projetos imprimem um comando `cd ... && iac-code --resume <id>` em vez de trocar o projeto atual em tempo real. |
| `/skills` | Abrir o seletor de gerenciamento de habilidades. Pesquise por nome ou descrição, ordene por nome/origem/tamanho e ative ou desative habilidades de usuário ou de projeto. Habilidades integradas permanecem bloqueadas e ativadas. |
| `/status` | Mostrar o ID da sessão atual, provedor, modelo, região da Alibaba Cloud, diretório de trabalho, uso registrado de tokens de API, contagem de turnos e utilização do contexto. |

A lista exata de comandos pode mudar entre versoes. Use `/help` ou digite `/` no REPL para inspecionar os comandos disponiveis na sua versao instalada.
