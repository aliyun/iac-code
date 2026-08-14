---
title: Desktop App
description: Install and use the native IaC Code app on macOS, Windows, and Linux.
---

# Desktop App

The IaC Code Desktop app provides the same agent, providers, cloud integrations, projects, and conversations as the CLI and Web app in an installed native application. A Tauri host starts the bundled Python runtime and loads the local IaC Code interface over a loopback connection; it does not expose a public Web service.

## Supported packages

Download the package for your platform from [GitHub Releases](https://github.com/aliyun/iac-code/releases).

| Operating system | Architecture | Package | Update method |
|---|---|---|---|
| macOS | Apple Silicon | `.dmg` | In-app updater |
| Windows | x64 | setup `.exe` | In-app updater |
| Linux | x64 | `.AppImage` | In-app updater |
| Debian / Ubuntu | x64 | `.deb` | Install a newer package |

Release assets also include `SHA256SUMS`, a software bill of materials (SBOM), and third-party notices.

## Installation

### macOS

1. Download the `.dmg`, open it, and drag **IaC Code** to **Applications**.
2. Open IaC Code from Applications.
3. Stable packages are signed with Apple Developer ID and notarized by Apple. Confirm the publisher information and checksum if macOS reports that the package cannot be verified.

### Windows

1. Download and run the setup `.exe`. The installer installs IaC Code for the current user and creates application shortcuts.
2. Stable packages carry an Authenticode publisher signature. Confirm the displayed publisher and verify `SHA256SUMS` before continuing if Microsoft Defender SmartScreen displays a warning.
3. The package includes the WebView2 bootstrap support needed by the interface. On first launch, IaC Code also checks for Git Bash and offers an installation guide if it is unavailable.

### Linux AppImage

Make the downloaded file executable, then run it:

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

The desktop environment may let you create a launcher after the first run. The AppImage can update itself when a signed update is available.

### Debian or Ubuntu

Install the deb package with APT so that system dependencies are resolved:

```bash
sudo apt install ./iac-code_*_amd64.deb
```

Start **IaC Code** from the application menu. A deb installation does not use the in-app updater; download and install the newer deb when upgrading.

## First launch

IaC Code asks you to select a project directory the first time it starts. The selected directory becomes the workspace for file access, generated templates, tools, and conversations. You can switch projects later from the project selector.

If you have already used the CLI or Web app, the Desktop app reuses the configuration in `~/.iac-code/` (or `IAC_CODE_CONFIG_DIR`), including model providers, Alibaba Cloud credentials, settings, and saved sessions. Otherwise, open **Settings** to configure a model provider and cloud credentials before starting a task.

The interface supports English, Simplified Chinese, Japanese, French, German, Spanish, and Portuguese. Language and color theme can be changed under **Settings > General**.

## Updates and package signatures

The macOS, Windows, and AppImage builds periodically read the stable release metadata and can download and apply a newer package. Every updater payload is verified with the IaC Code updater public key before installation. The deb package follows the normal Linux package workflow instead.

An updater signature is not the same as an operating-system publisher signature. The updater verifies that an update was produced by IaC Code, while Apple Developer ID signing/notarization and Windows Authenticode identify the publisher to the operating system. Stable macOS and Windows packages pass both layers of verification. Always obtain packages from the official release page and verify `SHA256SUMS`.

## Troubleshooting

- **The app remains on the startup screen:** use the recovery actions to retry startup or open the diagnostics folder. The log identifies missing runtime files, an occupied loopback port, or a sidecar startup failure.
- **Windows reports that Git Bash is missing:** follow the installation prompt, restart IaC Code, and run the check again. Git Bash is required for shell-based agent tools on Windows.
- **Linux opens the deb as an archive:** install it with the APT command above instead of opening it in an archive manager.
- **A stack or external link does not open on Linux:** configure a default browser for the desktop session, then retry the link.
- **Settings or sessions are not shared with the CLI:** confirm that both applications use the same `IAC_CODE_CONFIG_DIR` value and the same operating-system user account.

For CLI installation and commands, see [Installation](./getting-started/installation.md) and [CLI Usage](./cli/usage.md). For the browser-based interface, see the [Web App](./web-app.md).
