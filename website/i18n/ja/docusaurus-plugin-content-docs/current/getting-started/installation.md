---
title: インストール
description: IaC Code のインストールとコマンドの確認。
---

# インストール

IaC Code には Python 3.10 以降が必要です。macOS、Linux、Windows に対応しています。

## インストール

設定済みの Python パッケージインデックスからパッケージをインストールします：

```bash
pip install iac-code
```

コマンドを確認します：

```bash
iac-code --help
```

## オプション機能

対話型 CLI は基本パッケージだけで動作します。一部の実行モードは、`iac-code[<extra>]` の構文でインストールするオプションのエクストラに依存します：

| エクストラ | 有効になる機能 | コマンド |
|---|---|---|
| `http` | ローカルの [Web アプリ](../web-app.md)（`iac-code web`） | `pip install 'iac-code[http]'` |
| `a2a` | [A2A サーバー](../a2a/getting-started.md)（`iac-code a2a`） | `pip install 'iac-code[a2a]'` |

エクストラを入れずに実行モードを起動すると、インストールすべきエクストラを示すメッセージが表示されて失敗します。リポジトリをチェックアウトして開発する場合は、代わりに `uv sync --extra <extra>` を使用してください。

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
