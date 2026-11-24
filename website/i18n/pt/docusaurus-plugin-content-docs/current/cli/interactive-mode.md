---
title: Interface de terminal anterior
description: Entrada de compatibilidade para fluxos de terminal existentes.
---

# Interface de terminal anterior

TUI é a interface anterior para conversar e usar ferramentas diretamente no terminal. Esta página atende aos fluxos de terminal já existentes.

:::note

TUI não recebe mais atualizações ou manutenção. Novas funções de interface são desenvolvidas para [Web](../web-app.md) e [Desktop](../desktop-app.md); os mesmos controles e formas de exibição não são garantidos no terminal.

:::

## Iniciar e usar o TUI

1. Instale o [pacote Python](../getting-started/installation.md) e configure um [provedor de modelo](../configuration/llm-providers.md). Para tarefas na nuvem, configure também as [credenciais Alibaba Cloud](../configuration/alibaba-cloud-credentials.md). O TUI reutiliza a configuração salva.

2. Abra um terminal interativo, entre no diretório do projeto e execute:

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. Após o aviso de fim de manutenção, a entrada aparece. Digite e envie o pedido. Respostas e atividades de ferramentas aparecem no terminal; responda ali às perguntas e aprovações necessárias.

4. Use `/help` para consultar os comandos da versão instalada, `/status` para ver a sessão atual e `/exit` para sair.

O TUI precisa de um terminal interativo. Para scripts ou CI sem terminal, use a [automação headless](../automation/non-interactive-mode.md). Sem subcomando, `iac-code` abre o Web por padrão.

## Continuar uma conversa do terminal

Execute no diretório do projeto da conversa. Para selecionar uma sessão por ID ou nome único:

```bash
iac-code tui --resume <session-id-or-name>
```

Para continuar a sessão mais recente do projeto atual:

```bash
iac-code tui --continue
```

Não combine `--resume` e `--continue`. Consulte `iac-code tui --help` para as opções de sua versão.

## Mudar para Web ou Desktop

1. Execute `iac-code web` ou abra o Desktop e escolha o mesmo diretório de projeto.

2. Com o mesmo usuário do sistema e `IAC_CODE_CONFIG_DIR`, é possível reutilizar configurações de modelo, credenciais de nuvem e instruções em memória. Passe a gerenciá-las nas configurações; veja [Operações comuns](./commands.md).

3. Crie uma nova conversa para continuar o trabalho. Para consultar uma conversa antiga do terminal, ative a opção de somente leitura em **Configurações → Geral → Visibilidade de sessões externas**. O armazenamento compartilhado não permite editá-las no Web/Desktop; veja [Sessões](./sessions.md).
