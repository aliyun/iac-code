---
title: インストール
description: IaC Code のインストールとコマンドの確認。
---

# インストール

## デスクトップのインストール

GUI を使う場合は、OS に対応する[デスクトップパッケージ](../desktop-app.md)をインストールします。Python ランタイムが同梱されているため、Python の手動インストールや pip の実行は不要です。プラットフォーム要件はデスクトップのガイドにあります。

## Web と headless のインストール

ローカル Web やコマンドライン自動化には Python 3.10 以降をインストールし、以下のパッケージを追加します。macOS、Linux、Windows に対応しています。

```bash
pip install iac-code
```

コマンドを確認します：

```bash
iac-code --help
```

対話端末で `iac-code` を実行すると Web とブラウザーが開きます。ブラウザーを開かずサーバーを起動するには `iac-code web --no-open` を使います。次は[クイックスタート](./quick-start.md)を参照してください。

## オプション機能

標準インストールには Web アプリと HTTP ランタイムの依存関係が含まれます。`[http]` エクストラは互換性のための別名として残りますが、個別のインストールは不要です。リポジトリから実行する場合は `uv sync` だけで準備できます。

追加のトランスポートには引き続きオプションのエクストラを使用します。gRPC は `pip install 'iac-code[a2a-grpc]'`、Redis は `pip install 'iac-code[a2a-redis]'` でインストールできます。

## Windows の要件

Windows では、ツール実行環境として使用する bash シェルを提供するために [Git for Windows](https://gitforwindows.org/) のインストールが必要です。

Git Bash がインストール済みだが PATH に含まれていない場合は、`IAC_CODE_GIT_BASH_PATH` 環境変数に `bash.exe` のパスを設定してください：

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

winget を使用して Git for Windows をインストールできます：

```powershell
winget install --id Git.Git -e --source winget
```
