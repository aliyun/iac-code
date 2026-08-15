---
title: デスクトップアプリ
description: macOS、Windows、Linux に IaC Code のネイティブアプリをインストールして使用します。
---

# デスクトップアプリ

IaC Code デスクトップアプリでは、CLI や Web アプリと同じエージェント、モデルプロバイダー、クラウド連携、プロジェクト、会話を、インストール型のネイティブアプリとして利用できます。Tauri ホストが同梱の Python ランタイムを起動し、ループバック接続を介してローカルの IaC Code 画面を読み込みます。外部に公開される Web サービスは起動しません。

## 対応パッケージ

下記の安定版リンクから各環境向けの最新インストーラーをダウンロードできます。すべてのファイルや過去のバージョンは [GitHub Releases](https://github.com/aliyun/iac-code/releases) で確認できます。

| OS | アーキテクチャ | パッケージ | 更新方法 |
|---|---|---|---|
| macOS | Apple シリコン | [`.dmg`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg) | アプリ内更新 |
| Windows | x64 | [`.exe` インストーラー](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe) | アプリ内更新 |
| Linux | x64 | [`.AppImage`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage) | アプリ内更新 |
| Debian / Ubuntu | x64 | [`.deb`](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb) | 新しいパッケージをインストール |

各リリースには `SHA256SUMS`、ソフトウェア部品表（SBOM）、サードパーティーソフトウェアに関する通知も含まれます。

## インストール

### macOS

1. `.dmg` をダウンロードして開き、**IaC Code** を**アプリケーション**フォルダーにドラッグします。
2. 「アプリケーション」から IaC Code を開きます。
3. 安定版パッケージは Apple Developer ID で署名され、Apple の公証を受けています。それでも macOS がパッケージを検証できないと表示した場合は、発行元情報とチェックサムを確認してください。

### Windows

1. `.exe` インストーラーをダウンロードして実行します。IaC Code は現在のユーザー向けにインストールされ、アプリのショートカットが作成されます。
2. 安定版パッケージには Authenticode の発行元署名が付いています。Microsoft Defender SmartScreen が警告を表示した場合は、続行前に発行元情報と `SHA256SUMS` を確認してください。
3. パッケージには、画面表示に必要な WebView2 の導入支援機能が含まれています。初回起動時には Git Bash の有無も確認し、未導入の場合はインストール手順を案内します。

### Linux AppImage

ダウンロードしたファイルに実行権限を付けて起動します。

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

初回起動後に、デスクトップ環境からランチャーの作成を提案される場合があります。署名済みの更新が公開されると、AppImage は自動更新できます。

### Debian または Ubuntu

システムの依存関係も解決されるよう、APT で deb パッケージをインストールします。

```bash
sudo apt install ./iac-code_*_amd64.deb
```

インストール後はアプリケーションメニューから **IaC Code** を起動します。deb 版はアプリ内更新に対応していないため、更新時は新しい deb パッケージをダウンロードしてインストールしてください。

## 初回起動

IaC Code を初めて起動すると、プロジェクトフォルダーの選択を求められます。このフォルダーが、ファイル操作、テンプレート生成、ツール実行、会話保存のワークスペースになります。プロジェクトは後からプロジェクトセレクターで切り替えられます。

CLI または Web アプリをすでに利用している場合、デスクトップアプリは `~/.iac-code/`（または `IAC_CODE_CONFIG_DIR`）にあるモデルプロバイダー、Alibaba Cloud の認証情報、設定、保存済みセッションをそのまま使用します。未設定の場合は、タスクを始める前に**設定**を開き、モデルプロバイダーとクラウド認証情報を登録してください。

画面表示は英語、簡体字中国語、日本語、フランス語、ドイツ語、スペイン語、ポルトガル語に対応しています。言語とカラーテーマは**設定 > 一般**で変更できます。

## 更新とパッケージの署名

macOS 版、Windows 版、AppImage 版は安定版の更新情報を定期的に確認し、新しいバージョンをダウンロードしてインストールできます。更新パッケージは、インストール前に IaC Code の更新用公開鍵で必ず検証されます。deb 版は通常の Linux パッケージと同じ手順で更新します。

更新用の署名と、OS が確認する発行元署名は別のものです。前者は更新が IaC Code によって作成されたことを確認し、Apple Developer ID の署名と公証、および Windows Authenticode は OS に対して発行元を示します。安定版 macOS／Windows パッケージは両方の検証を通過します。必ず公式リリースページから入手し、`SHA256SUMS` を確認してください。

## トラブルシューティング

- **起動画面から進まない：**復旧操作から起動をやり直すか、診断フォルダーを開いてください。ログで、ランタイムファイルの不足、ループバックポートの競合、補助プロセスの起動失敗などを確認できます。
- **Windows で Git Bash が見つからないと表示される：**案内に従ってインストールし、IaC Code を再起動してからもう一度確認してください。Windows でシェルを使うエージェントツールには Git Bash が必要です。
- **Linux で deb が圧縮ファイルとして開く：**アーカイブマネージャーではなく、上記の APT コマンドでインストールしてください。
- **Linux でスタックや外部リンクが開かない：**デスクトップセッションの既定のブラウザーを設定してから、もう一度リンクを開いてください。
- **CLI と設定やセッションが共有されない：**両方が同じ `IAC_CODE_CONFIG_DIR` を使用し、同じ OS ユーザーで実行されているか確認してください。

CLI のインストール方法とコマンドについては[インストール](./getting-started/installation.md)と [CLI の使い方](./cli/usage.md)を、ブラウザー版については [Web アプリ](./web-app.md)をご覧ください。
