<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>Asistente de Infraestructura como Código (IaC) impulsado por IA que genera y gestiona plantillas de orquestación de recursos de Alibaba Cloud (ROS / Terraform) mediante interacción en lenguaje natural.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | <a href="README.zh.md">中文</a> | Español | <a href="README.fr.md">Français</a> | <a href="README.de.md">Deutsch</a> | <a href="README.ja.md">日本語</a> | <a href="README.pt.md">Português</a>
</p>

> **Documentación**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/es/)
<p align="center">
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## Instalación

IaC Code requiere Python 3.10 o superior. Es compatible con macOS, Linux y Windows.

> **Nota de Windows**: En Windows, se debe instalar [Git for Windows](https://gitforwindows.org/) para proporcionar el entorno de shell bash utilizado por la ejecución de herramientas. Si Git Bash está instalado pero no se encuentra en el PATH, configure la variable de entorno `IAC_CODE_GIT_BASH_PATH`.

```bash
pip install iac-code
```

## Uso

En el primer uso, configure el proveedor de LLM y el servicio en la nube de IaC ingresando `/auth` en el modo interactivo.

### Modo Interactivo

Ejecute directamente para ingresar al REPL interactivo:

```bash
iac-code
```

### Modo No Interactivo

Pase un prompt único mediante `--prompt`:

```bash
iac-code --prompt "Crear un VPC y dos instancias ECS"
```

También se admite la lectura desde stdin:

```bash
echo "Crear un bucket OSS" | iac-code --prompt -
```

## Contáctenos

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
