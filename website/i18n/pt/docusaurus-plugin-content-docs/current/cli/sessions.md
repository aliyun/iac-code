---
title: Sessões
description: Persistir e retomar conversas entre execuções.
---

# Sessões

O IaC Code persiste automaticamente cada conversa em disco. Você pode retomar qualquer sessão anterior para continuar de onde parou.

## Retomar sessões

### Interativo: `/resume`

No REPL, use o comando `/resume`:

```text
/resume
```

Isso abre um seletor interativo com as sessões recentes do projeto atual. Quando um nome de sessão está definido, ele aparece como título; caso contrário, o último prompt ou, como fallback, o primeiro prompt é usado.

Para retomar uma sessão específica por ID exato, prefixo único de ID ou nome único de sessão:

```text
/resume abc123
```

### Nomear sessões

Use `/rename` para dar à sessão ativa um nome estável e legível:

```text
/rename deploy-prod
```

O nome é armazenado nos metadados da sessão. Ele aparece no banner de boas-vindas ao retomar, na dica de saída e no seletor de `/resume`.

Você pode retomar pelo nome quando ele identifica uma sessão de forma única:

```text
/resume deploy-prod
iac-code --resume deploy-prod
```

### CLI: `--resume` e `--continue`

Retome uma sessão específica pela linha de comando por ID exato, prefixo único de ID ou nome único de sessão:

```bash
iac-code --resume <id-ou-nome-da-sessao>
```

Retome a sessão mais recente:

```bash
iac-code --continue
```

As opções curtas `-r` e `-c` também estão disponíveis:

```bash
iac-code -r <id-ou-nome-da-sessao>
iac-code -c
```

### Sessões entre projetos

Quando uma sessão pertence a outro diretório de projeto, o IaC Code não troca o diretório de trabalho em tempo real. Em vez disso, imprime o comando para retomar no contexto correto:

```text
cd /path/to/other/project && iac-code --resume <session-id>
```

Esse comando também é copiado para a área de transferência quando possível.

## Recuperação de interrupções

Se uma sessão foi interrompida durante a execução, por exemplo porque o processo foi encerrado enquanto uma ferramenta estava rodando, o IaC Code detecta as chamadas de ferramenta órfãs ao retomar e adiciona resultados de erro sintéticos. Isso permite que o modelo se recupere sem ficar preso aguardando uma saída de ferramenta que nunca chegará.

## Seletor de sessões

O seletor de `/resume` mostra:

| Coluna | Descrição |
|--------|-----------|
| Título | Nome da sessão quando definido; caso contrário, último ou primeiro prompt do usuário |
| Branch | Branch Git no momento da sessão |
| Hora | Última modificação |

As sessões são ordenadas da mais recente para a mais antiga. Você pode digitar para filtrar pelo conteúdo do título.
