<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>Assistente de Infraestrutura como Código (IaC) impulsionado por IA que gera e gerencia modelos de orquestração de recursos do Alibaba Cloud (ROS / Terraform) por meio de interação em linguagem natural.</em>
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

## Fale Conosco

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
