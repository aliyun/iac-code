<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>KI-gestützter Infrastructure-as-Code-Assistent (IaC), der Alibaba Cloud Ressourcen-Orchestrierungsvorlagen (ROS / Terraform) durch natürlichsprachliche Interaktion generiert und verwaltet.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | Deutsch | <a href="README.ja.md">日本語</a> | <a href="README.pt.md">Português</a>
</p>

> **Dokumentation**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/de/)
<p align="center">
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## Installation

IaC Code erfordert Python 3.10 oder höher. Es unterstützt macOS, Linux und Windows.

> **Windows-Hinweis**: Unter Windows muss [Git for Windows](https://gitforwindows.org/) installiert sein, um die bash-Shell-Umgebung für die Werkzeugausführung bereitzustellen. Wenn Git Bash installiert, aber nicht im PATH ist, setzen Sie die Umgebungsvariable `IAC_CODE_GIT_BASH_PATH`.

```bash
pip install iac-code
```

## Verwendung

Bei der ersten Nutzung konfigurieren Sie den LLM-Anbieter und den IaC-Cloud-Dienst, indem Sie `/auth` im interaktiven Modus eingeben.

### Interaktiver Modus

Direkt ausführen, um die interaktive REPL zu starten:

```bash
iac-code
```

### Nicht-interaktiver Modus

Übergeben Sie einen einmaligen Prompt über `--prompt`:

```bash
iac-code --prompt "Erstelle ein VPC und zwei ECS-Instanzen"
```

Das Lesen von stdin wird ebenfalls unterstützt:

```bash
echo "Erstelle einen OSS-Bucket" | iac-code --prompt -
```

## Mitwirken

Installieren Sie [uv](https://docs.astral.sh/uv/getting-started/installation/), dann:

```bash
make install   # Abhängigkeiten und Pre-Commit-Hooks installieren
make dev       # im Debug-Modus ausführen
make test      # Tests ausführen
make lint      # Linter ausführen
make format    # Code formatieren
```

Weitere Details finden Sie im [Beitragsleitfaden](https://aliyun.github.io/iac-code/de/getting-started/contributing).

## Kontakt

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
