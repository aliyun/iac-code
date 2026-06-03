---
title: コマンドラインオプション
description: IaC Code の起動オプションとワンショット実行パラメータのリファレンス。
---

# コマンドラインオプション

コマンドラインオプションは IaC Code の起動方法を制御します。対話型 REPL に入る前に使用するか、`--prompt` と組み合わせてワンショット自動化に使用します。

| オプション | 目的 |
|---|---|
| `-h`, `--help` | CLI ヘルプを表示して終了します。インストールされたバージョンがサポートするオプションを確認できます。 |
| `-v`, `-V`, `--version` | インストールされた IaC Code のバージョンを表示して終了します。 |
| `-m <model>`, `--model <model>` | 特定の LLM モデルで起動します。今回の実行で保存済みモデルを上書きします。 |
| `-p <prompt>`, `--prompt <prompt>` | 単一のプロンプトを実行して終了します。非対話モードが有効になります。`--prompt -` で標準入力からプロンプトを読み取ります。 |
| `--output-format <format>` | 非対話モードの出力形式を設定します。サポートされる値は `text`、`json`、`stream-json` です。デフォルトは `text` です。 |
| `--max-turns <number>` | 非対話モードでのエージェントの最大ターン数を制限します。デフォルトは `100` です。 |
| `-d`, `--debug` | 今回の実行でデバッグログを有効にします。対話モードでは、起動後に `/debug` を使用してデバッグログを確認または変更できます。 |
| `-r <セッションIDまたは名前>`, `--resume <セッションIDまたは名前>` | 正確なセッション ID、一意な ID プレフィックス、または一意なセッション名で以前のセッションを再開します。別プロジェクトとして解決されたセッションは、現在のプロジェクトをその場で切り替えず、`cd ... && iac-code --resume <id>` コマンドを表示します。 |
| `-c`, `--continue` | 最新のセッションを再開します。`--resume` と同時に使用できません。 |
| `--allowed-tools <patterns>` | 許可するツール権限パターンをカンマ区切りで指定します。例：`'bash(git *),write_file'`。 |
| `--disallowed-tools <patterns>` | 拒否するツール権限パターンをカンマ区切りで指定します。例：`'bash(rm *)'`。 |
| `--permission-mode <mode>` | 権限モード：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |

## 権限モード

`--permission-mode` パラメータは、エージェントがツールの権限チェックをどのように処理するかを制御します：

| モード | 動作 |
|---|---|
| `default` | ツールアクションが承認を必要とする場合、エージェントが確認を求めます。 |
| `accept_edits` | 編集とみなされるファイルシステムコマンド（例：`mkdir`、`cp`）を自動承認します。その他のアクションは引き続き確認を求めます。 |
| `bypass_permissions` | セーフティチェックを除くすべてのツールアクションを自動承認します。信頼できる自動化向けです。 |
| `dont_ask` | 通常確認が必要なアクションを黙って拒否します。厳密な読み取り専用実行に便利です。 |

## よく使う起動コマンド

保存済みモデルで対話型 REPL を起動する：

```bash
iac-code
```

今回の実行で特定のモデルを指定する：

```bash
iac-code --model qwen3.6-plus
```

ワンショットプロンプトを実行する：

```bash
iac-code --prompt "Create an OSS Bucket"
```

標準入力からプロンプトを読み取る：

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

最新のセッションを再開する：

```bash
iac-code --continue
```

git と読み取り専用の bash コマンドのみ許可する：

```bash
iac-code --allowed-tools 'bash(git *)'
```

対話プロンプトなしで自動化実行する：

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
