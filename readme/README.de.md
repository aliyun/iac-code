<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>KI-gestuetzter Infrastructure-as-Code-Assistent (IaC), der Cloud-Infrastrukturvorlagen durch natuerlichsprachliche Interaktion generiert und verwaltet. Unterstuetzt derzeit Alibaba Cloud ROS- und Terraform-Workflows.</em>
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
  <a href="https://github.com/aliyun/iac-code/releases/latest"><img src="https://img.shields.io/badge/Herunterladen-IaC%20Code%20Desktop-5268f2?style=for-the-badge" alt="IaC Code Desktop herunterladen"></a>
  <br>
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg">macOS Apple Silicon</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe">Windows x64</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage">Linux AppImage</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb">Linux deb</a> ·
  <a href="https://github.com/aliyun/iac-code/releases/latest">Alle Veröffentlichungsdateien</a>
</p>

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

### Web-App

Sie bevorzugen eine grafische Oberfläche? Starten Sie die lokale Web-App, die dieselbe Engine wie die CLI ausführt und dieselben Sitzungen teilt. Die Web-App benötigt das `http`-Extra, installieren Sie es daher zuerst:

```bash
pip install 'iac-code[http]'
iac-code web
```

Standardmäßig öffnet sie `http://127.0.0.1:8766` in Ihrem Browser (nur Loopback). Weitere Details finden Sie im [Web-App-Leitfaden](https://aliyun.github.io/iac-code/de/web-app).

<p align="center">
  <img src="../website/static/img/screenshots/iac-code-web-en.jpg" alt="IaC Code Web-App" width="100%">
</p>

### Desktop-App

Für die Nutzung als native Anwendung laden Sie das passende Paket aus der [neuesten GitHub-Version](https://github.com/aliyun/iac-code/releases/latest) herunter:

- Mac mit Apple-Chip: `.dmg`
- Windows x64: `.exe`-Installationsprogramm
- Linux x64: `.AppImage` oder `.deb`

Die Desktop-App verwendet dieselbe IaC-Code-Engine und dieselben Modellanbieter, Cloud-Zugangsdaten, Einstellungen, Projekte und Sitzungen wie CLI und Web-App. Beim ersten Start wählen Sie den Projektordner aus, in dem IaC Code arbeiten soll. Unter Windows prüft die App außerdem, ob Git Bash installiert ist, und führt bei Bedarf durch die Installation.

<p align="center">
  <img src="../website/static/img/screenshots/iac-code-desktop-en.jpg" alt="IaC Code Desktop-App" width="100%">
</p>

Die macOS-, Windows- und AppImage-Versionen können kryptografisch signierte Aktualisierungen direkt in der App suchen und installieren. Das deb-Paket wird durch die Installation einer neueren Paketversion aktualisiert. Stabile macOS-Pakete sind mit einer Apple Developer ID signiert und von Apple notarisiert; stabile Windows-Pakete tragen eine Authenticode-Herausgebersignatur. Beziehen Sie Pakete immer von der offiziellen Release-Seite und prüfen Sie die dort veröffentlichte Datei `SHA256SUMS`. Installationshinweise und Hilfe bei Problemen finden Sie im [Leitfaden zur Desktop-App](https://aliyun.github.io/iac-code/de/docs/desktop-app).

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
