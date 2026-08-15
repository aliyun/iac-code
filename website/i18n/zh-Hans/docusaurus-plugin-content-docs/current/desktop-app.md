---
title: 桌面应用
description: 在 macOS、Windows 和 Linux 上安装并使用原生 IaC Code 应用。
---

# 桌面应用

IaC Code 桌面应用把 CLI 和 Web 应用中的同一套智能体、模型服务、云服务集成、项目与会话封装成可安装的原生应用。Tauri 宿主会启动随应用打包的 Python 运行时，并通过回环连接加载本地 IaC Code 界面，不会对外提供公开的 Web 服务。

## 支持的安装包

可通过下方稳定版直链下载各平台的最新安装包；如需全部文件或历史版本，请访问 [GitHub Releases](https://github.com/aliyun/iac-code/releases)。

| 操作系统 | 架构 | 安装包 | 更新方式 |
|---|---|---|---|
| macOS | Apple 芯片 | [`.dmg`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg) | 应用内更新 |
| Windows | x64 | [`.exe` 安装程序](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe) | 应用内更新 |
| Linux | x64 | [`.AppImage`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage) | 应用内更新 |
| Debian / Ubuntu | x64 | [`.deb`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb) | 安装新版软件包 |

每个版本还会提供 `SHA256SUMS`、软件物料清单（SBOM）和第三方软件声明。

## 安装

### macOS

1. 下载并打开 `.dmg`，将 **IaC Code** 拖入**应用程序**目录。
2. 从“应用程序”中打开 IaC Code。
3. 稳定版安装包使用 Apple Developer ID 签名并经过 Apple 公证。如果 macOS 仍提示无法验证安装包，请核对发布者信息和校验和。

### Windows

1. 下载并运行 `.exe` 安装程序。IaC Code 会安装到当前用户，并创建应用快捷方式。
2. 稳定版安装包带有 Authenticode 发布者签名。如果 Microsoft Defender SmartScreen 仍显示警告，请先核对发布者信息和 `SHA256SUMS` 再继续。
3. 安装包带有界面运行所需的 WebView2 引导支持。IaC Code 首次启动时还会检查 Git Bash；如果尚未安装，会提供安装引导。

### Linux AppImage

为下载的文件添加执行权限，然后运行：

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

首次运行后，桌面环境可能会提供创建启动器的选项。发现带签名的新版本时，AppImage 可以自行更新。

### Debian 或 Ubuntu

请使用 APT 安装 deb 包，以便系统自动处理依赖：

```bash
sudo apt install ./iac-code_*_amd64.deb
```

安装后可从应用菜单启动 **IaC Code**。deb 版本不使用应用内更新；升级时请下载并安装新版 deb 包。

## 首次启动

IaC Code 首次启动时会要求选择项目目录。该目录将作为文件访问、模板生成、工具执行和会话的工作区。之后可以通过项目选择器切换项目。

如果你已经用过 CLI 或 Web 应用，桌面应用会复用 `~/.iac-code/`（或 `IAC_CODE_CONFIG_DIR`）中的配置，包括模型服务、阿里云凭证、应用设置和已保存的会话。若此前没有配置，请先在**设置**中添加模型服务和云凭证，再开始任务。

界面支持 English、简体中文、日本語、Français、Deutsch、Español 和 Português。可以在**设置 > 常规**中修改语言和配色主题。

## 更新与安装包签名

macOS、Windows 和 AppImage 版本会定期读取稳定版更新信息，并可下载和安装新版本。每个更新包在安装前都会使用 IaC Code 的更新公钥进行验证。deb 版本则沿用 Linux 常规的软件包更新方式。

更新签名与操作系统的发布者签名并不是一回事：前者用于确认更新确实由 IaC Code 发布；Apple Developer ID 签名/公证和 Windows Authenticode 则用于向操作系统表明发布者身份。稳定版 macOS 和 Windows 安装包会同时通过这两层验证。请始终从官方版本页面下载安装包，并核对 `SHA256SUMS`。

## 故障排查

- **应用一直停留在启动页：**使用恢复操作重试启动或打开诊断目录。日志会指出运行时文件缺失、回环端口占用或 sidecar 启动失败等问题。
- **Windows 提示缺少 Git Bash：**按照安装引导完成安装，重启 IaC Code 后再次检查。Windows 上基于 Shell 的智能体工具依赖 Git Bash。
- **Linux 双击 deb 后显示压缩包内容：**不要用归档管理器打开，请改用上面的 APT 命令安装。
- **Linux 上无法打开资源栈或外部链接：**先为当前桌面会话设置默认浏览器，然后重试。
- **桌面应用没有与 CLI 共用设置或会话：**确认两者使用相同的 `IAC_CODE_CONFIG_DIR`，并由同一个操作系统用户运行。

CLI 的安装与命令说明见[安装](./getting-started/installation.md)和 [CLI 使用](./cli/usage.md)；浏览器界面的说明见 [Web 应用](./web-app.md)。
