<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>Assistente de Infraestrutura como Código (IaC) impulsionado por IA que gera e gerencia templates de infraestrutura em nuvem por meio de interação em linguagem natural. Atualmente oferece suporte a fluxos de trabalho Alibaba Cloud ROS e Terraform.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.de.md">Deutsch</a> | <a href="README.ja.md">日本語</a> | Português
</p>

> **Documentação**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/pt/)

<p align="center">
  <a href="https://github.com/aliyun/iac-code/releases/latest"><img src="https://img.shields.io/badge/Baixar-IaC%20Code%20Desktop-5268f2?style=for-the-badge" alt="Baixar IaC Code Desktop"></a>
  <br>
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg">macOS Apple Silicon</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe">Windows x64</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage">Linux AppImage</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb">Linux deb</a> ·
  <a href="https://github.com/aliyun/iac-code/releases/latest">Todos os arquivos da versão</a>
</p>

<p align="center">
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## Instalação

O IaC Code requer Python 3.10 ou superior. É compatível com macOS, Linux e Windows.

> **Nota sobre Windows**: No Windows, o [Git for Windows](https://gitforwindows.org/) deve estar instalado para fornecer o ambiente de shell bash utilizado pela execução de ferramentas. Se o Git Bash estiver instalado mas não estiver no PATH, defina a variável de ambiente `IAC_CODE_GIT_BASH_PATH`.

```bash
pip install iac-code
```

## Uso

No primeiro uso, configure o provedor de LLM e o serviço de nuvem IaC digitando `/auth` no modo interativo.

### Modo Interativo

Execute diretamente para entrar no REPL interativo:

```bash
iac-code
```

### Modo Não Interativo

Passe um prompt único via `--prompt`:

```bash
iac-code --prompt "Criar um VPC e duas instâncias ECS"
```

A leitura a partir do stdin também é suportada:

```bash
echo "Criar um bucket OSS" | iac-code --prompt -
```

### Aplicativo web

Prefere uma interface gráfica? Inicie o aplicativo web local, que executa o mesmo motor da CLI e compartilha as mesmas sessões. O aplicativo web precisa do extra `http`, então instale-o primeiro:

```bash
pip install 'iac-code[http]'
iac-code web
```

Por padrão, ele abre `http://127.0.0.1:8766` no seu navegador (apenas loopback). Consulte o [guia do aplicativo web](https://aliyun.github.io/iac-code/pt/web-app) para mais detalhes.

### Aplicativo para desktop

Para usar o IaC Code como aplicativo nativo, baixe o pacote da sua plataforma na [versão mais recente do GitHub](https://github.com/aliyun/iac-code/releases/latest):

- Mac com Apple Silicon: `.dmg`
- Windows x64: instalador `.exe`
- Linux x64: `.AppImage` ou `.deb`

O aplicativo para desktop executa o mesmo mecanismo do IaC Code e compartilha provedores de modelos, credenciais de nuvem, configurações, projetos e sessões com a CLI e o aplicativo web. Na primeira inicialização, selecione a pasta do projeto em que o IaC Code deverá trabalhar. No Windows, o aplicativo também verifica se o Git Bash está instalado e, caso não esteja, apresenta as instruções de instalação.

As versões para macOS, Windows e AppImage podem procurar e aplicar, no próprio aplicativo, atualizações com assinatura criptográfica. O pacote deb é atualizado com a instalação de uma versão mais recente. Os pacotes estáveis para macOS são assinados com Apple Developer ID e notarizados pela Apple; os pacotes estáveis para Windows têm assinatura de editor Authenticode. Sempre baixe os pacotes pela página oficial da versão e confira o arquivo `SHA256SUMS` publicado junto com ela. Consulte o [guia do aplicativo para desktop](https://aliyun.github.io/iac-code/pt/docs/desktop-app) para ver as instruções e a solução de problemas.

## Contribuir

Instale o [uv](https://docs.astral.sh/uv/getting-started/installation/), depois:

```bash
make install   # instalar dependências e hooks de pre-commit
make dev       # executar em modo de depuração
make test      # executar testes
make lint      # executar linters
make format    # formatar código
```

Veja o [Guia de contribuição](https://aliyun.github.io/iac-code/pt/getting-started/contributing) para mais detalhes.

## Fale Conosco

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
