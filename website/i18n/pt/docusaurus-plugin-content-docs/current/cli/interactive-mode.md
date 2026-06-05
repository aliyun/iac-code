---
title: Modo interativo
description: Use o REPL para trabalho iterativo de infraestrutura.
---

# Modo interativo

Execute sem argumentos para entrar no REPL interativo:

```bash
iac-code
```

O modo interativo e util quando deseja refinar requisitos de infraestrutura ao longo de varias interacoes.

Comece com a autenticacao:

```text
/auth
```

Em seguida, descreva o que deseja construir:

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```

## Comandos

Digite `/` para descobrir os comandos slash disponíveis. Comandos operacionais comuns incluem `/status` para o estado da sessão atual, `/skills` para gerenciamento de habilidades, `/memory` para arquivos de memória de projeto e usuário, `/rename` para nomear a sessão ativa e `/resume` para alternar sessões.

Digite `$` para descobrir e invocar apenas habilidades.

## Editar entrada

Use `Shift+Enter` para inserir uma nova linha sem enviar o prompt. Pressione `Enter` sozinho para enviar o prompt completo.

Se o seu terminal nao informar `Shift+Enter` separadamente, pressione `Esc` e depois `Enter` para inserir uma nova linha. Prompts com varias linhas sao salvos como uma unica entrada de historico, entao `Up` restaura o prompt completo.

## Shell escapes

Prefixe uma linha com `!` para executar um comando shell local a partir do REPL por meio da ferramenta `bash` integrada:

```text
!pwd
!git status --short
```

O IaC Code aplica as verificacoes normais de permissao de ferramentas, executa o comando no contexto do projeto atual e mostra a saida no terminal. O comando nao e enviado ao modelo como mensagem de chat.
