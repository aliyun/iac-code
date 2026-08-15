---
title: Aplicativo para desktop
description: Instale e use o aplicativo nativo do IaC Code no macOS, Windows e Linux.
---

# Aplicativo para desktop

O aplicativo para desktop oferece o mesmo agente, provedores, integrações com a nuvem, projetos e conversas da CLI e do aplicativo web, mas em um aplicativo nativo instalado. O host Tauri inicia o ambiente Python incluído no pacote e carrega a interface local do IaC Code por uma conexão de loopback; nenhum serviço web público é exposto.

## Pacotes compatíveis

Use os links estáveis abaixo para baixar os instaladores mais recentes de cada plataforma ou consulte [GitHub Releases](https://github.com/aliyun/iac-code/releases) para ver todos os arquivos e versões anteriores.

| Sistema operacional | Arquitetura | Pacote | Forma de atualização |
|---|---|---|---|
| macOS | Apple Silicon | [`.dmg`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg) | Atualização pelo aplicativo |
| Windows | x64 | [instalador `.exe`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe) | Atualização pelo aplicativo |
| Linux | x64 | [`.AppImage`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage) | Atualização pelo aplicativo |
| Debian / Ubuntu | x64 | [`.deb`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb) | Instalação de um pacote mais recente |

Cada versão também inclui o arquivo `SHA256SUMS`, uma lista de materiais de software (SBOM) e os avisos de componentes de terceiros.

## Instalação

### macOS

1. Baixe e abra o arquivo `.dmg`, depois arraste o **IaC Code** para **Aplicativos**.
2. Abra o IaC Code pela pasta Aplicativos.
3. Os pacotes estáveis são assinados com Apple Developer ID e notarizados pela Apple. Se o macOS ainda informar que não consegue verificar o pacote, confira os dados do editor e a soma de verificação.

### Windows

1. Baixe e execute o instalador `.exe`. O IaC Code é instalado para o usuário atual e cria os atalhos do aplicativo.
2. Os pacotes estáveis têm assinatura de editor Authenticode. Se o Microsoft Defender SmartScreen ainda exibir um aviso, confira o editor e o arquivo `SHA256SUMS` antes de prosseguir.
3. O pacote inclui o suporte de inicialização do WebView2 necessário para a interface. Na primeira execução, o IaC Code também verifica se o Git Bash está instalado e, caso não esteja, apresenta as instruções de instalação.

### Linux AppImage

Dê permissão de execução ao arquivo baixado e execute-o:

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

Após a primeira execução, o ambiente gráfico pode oferecer a criação de um atalho. A AppImage consegue se atualizar quando houver uma atualização assinada.

### Debian ou Ubuntu

Instale o pacote deb com o APT para que as dependências do sistema sejam resolvidas:

```bash
sudo apt install ./iac-code_*_amd64.deb
```

Abra o **IaC Code** pelo menu de aplicativos. A instalação deb não usa o atualizador interno; para fazer upgrade, baixe e instale o pacote deb mais recente.

## Primeira execução

Na primeira vez que é aberto, o IaC Code pede que você escolha uma pasta de projeto. Essa pasta passa a ser o espaço de trabalho usado para acessar arquivos, gerar modelos, executar ferramentas e guardar conversas. Depois, é possível trocar de projeto pelo seletor de projetos.

Se você já usou a CLI ou o aplicativo web, o aplicativo para desktop reaproveita a configuração em `~/.iac-code/` (ou `IAC_CODE_CONFIG_DIR`), incluindo provedores de modelos, credenciais do Alibaba Cloud, preferências e sessões salvas. Caso contrário, abra **Configurações** e cadastre um provedor de modelos e as credenciais de nuvem antes de iniciar uma tarefa.

A interface está disponível em inglês, chinês simplificado, japonês, francês, alemão, espanhol e português. O idioma e o tema de cores podem ser alterados em **Configurações > Geral**.

## Atualizações e assinaturas dos pacotes

As versões para macOS, Windows e AppImage consultam periodicamente as informações da versão estável e podem baixar e instalar uma nova versão. Antes da instalação, cada atualização é validada com a chave pública de atualização do IaC Code. O pacote deb segue o processo convencional de pacotes do Linux.

A assinatura de atualização não é a mesma assinatura de editor verificada pelo sistema operacional. A primeira confirma que a atualização foi produzida pelo IaC Code; a assinatura e notarização Apple Developer ID e o Windows Authenticode identificam o editor para o sistema. Os pacotes estáveis para macOS e Windows passam pelas duas camadas de verificação. Sempre baixe os pacotes da página oficial da versão e confira o arquivo `SHA256SUMS`.

## Solução de problemas

- **O aplicativo não sai da tela de inicialização:** use as ações de recuperação para tentar novamente ou abrir a pasta de diagnóstico. O registro identifica arquivos ausentes no ambiente, uma porta de loopback ocupada ou uma falha ao iniciar o processo auxiliar.
- **O Windows informa que o Git Bash não foi encontrado:** siga as instruções de instalação, reinicie o IaC Code e refaça a verificação. No Windows, as ferramentas do agente que usam shell dependem do Git Bash.
- **O Linux abre o deb como arquivo compactado:** instale-o com o comando APT acima, em vez de abri-lo no gerenciador de arquivos compactados.
- **Uma pilha ou um link externo não abre no Linux:** configure um navegador padrão para a sessão gráfica e tente novamente.
- **As configurações ou sessões não são compartilhadas com a CLI:** confirme que os dois aplicativos usam o mesmo valor de `IAC_CODE_CONFIG_DIR` e são executados pelo mesmo usuário do sistema.

Para saber como instalar e usar a CLI, consulte [Instalação](./getting-started/installation.md) e [Uso da CLI](./cli/usage.md). Para a interface no navegador, consulte o [Aplicativo web](./web-app.md).
