---
sidebar_position: 2
title: Inicio rapido MCP
description: Adicionar e conectar servidores MCP no painel Plugins do Desktop ou Web.
---

# Inicio rapido MCP

No Desktop e Web, adicione e gerencie servidores na aba **MCP** de **Plugins**. Os comandos CLI e arquivos abaixo servem para automação e configurações avançadas.

## Conectar um servidor HTTP remoto

1. Abra **Plugins → MCP**, escolha o projeto e clique em **Adicionar servidor**.

2. Informe o **Nome**, escolha o **Escopo** e selecione **http (remoto)** como transporte.

3. Preencha a **URL** e os cabeçalhos necessários; use referências a variáveis de ambiente para segredos.

4. Clique em **Criar**. Se a configuração de projeto aguardar aprovação, revise e clique em **Aprovar**.

5. Se necessário, clique em **Autenticar**, conclua a autorização no navegador e volte ao painel.

6. Clique em **Verificar conexão** e em **Ver capacidades**. Confira as ferramentas, recursos ou prompts esperados antes de usá-los.

## Conectar um servidor stdio local

1. Abra **Plugins → MCP**, escolha o projeto e clique em **Adicionar servidor**.

2. Informe o **Nome**, o **Escopo** e selecione **stdio (comando local)**.

3. Preencha o **Comando**, um argumento por linha em **Argumentos**, e as variáveis de ambiente necessárias. O executável deve existir na máquina que executa o IaC Code.

4. Clique em **Criar** e aprove a configuração de projeto se necessário.

5. Use **Verificar conexão** e **Ver capacidades** para confirmar a conexão e a descoberta.

:::note

O escopo de usuário vale entre projetos; o local é privado ao diretório escolhido; o de projeto grava um `.mcp.json` compartilhável e exige aprovação local. Para alterar um servidor, use sua ação de edição e clique em **Salvar**.

:::

## Automação CLI

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## Proximos passos

- Consulte [Configuração MCP](./configuration.md) para escopos, arquivos de projeto, cabeçalhos, opções OAuth e formatos JSON avançados.
- Consulte [OAuth e segurança](./oauth-and-security.md) para armazenamento de tokens, redefinição e aprovações.
- Consulte [Solução de problemas](./troubleshooting.md) se o servidor precisar de aprovação ou autenticação, ou falhar ao conectar.
