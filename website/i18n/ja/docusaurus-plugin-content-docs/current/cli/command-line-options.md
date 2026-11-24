---
title: コマンドラインオプション
description: IaC Code の起動オプションとワンショット実行パラメータのリファレンス。
---

# コマンドラインオプション

ルートコマンドは既定で Web を起動します。以下は Web の起動、単発 headless 実行、stream-json サブプロセスのオプションです。対話会話の設定は Web またはデスクトップで行います。

「使用できる入口」列は各オプションを指定できる場所を示します。`headless` は `iac-code --prompt ...`、`process` は stream-json サブプロセスモードです。Web のモデルと権限は画面で設定します。

| オプション | 目的 | 使用できる入口 |
|---|---|---|
| `-h`, `--help` | CLI ヘルプを表示して終了します。インストールされたバージョンがサポートするオプションを確認できます。 | `iac-code`, `web` |
| `-v`, `-V`, `--version` | インストールされた IaC Code のバージョンを表示して終了します。 | `iac-code` |
| `-m <model>`, `--model <model>` | 特定の LLM モデルで起動します。今回の実行で保存済みモデルを上書きします。 | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | 単一のプロンプトを実行して終了します。非対話モードが有効になります。`--prompt -` で標準入力からプロンプトを読み取ります。 | `iac-code` |
| `--output-format <format>` | 非対話モードの出力形式を設定します。サポートされる値は `text`、`json`、`stream-json` です。デフォルトは `text` です。 | `headless`, `process` |
| `--max-turns <number>` | 非対話モードでのエージェントの最大ターン数を制限します。デフォルトは `100` です。 | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | ワンショットの非対話リクエストで thinking を明示的に有効化するかどうかを制御します。デフォルトは `--thinking-enabled` です。`--no-thinking-enabled` はこの実行だけ `thinking_enabled=false` を送信し、`settings.yml` は書き換えません。 | `headless` |
| `-d`, `--debug` | 今回の実行でデバッグログを有効にします。 | `headless`, `process`, `web` |
| `--verbose` | ヘッドレス（非対話）実行の進捗を標準エラー出力に表示します。 | `headless` |
| `-r <セッションIDまたは名前>`, `--resume <セッションIDまたは名前>` | 正確なセッション ID、一意の ID プレフィックス、一意の名前で復元します。headless は対象プロジェクトのディレクトリで実行してください。 | `headless` |
| `-c`, `--continue` | 最新のセッションを再開します。`--resume` と同時に使用できません。 | `headless` |
| `--allowed-tools <patterns>` | 許可するツール権限パターンをカンマ区切りで指定します。例：`'bash(git *),write_file'`。 | `headless`, `process` |
| `--disallowed-tools <patterns>` | 拒否するツール権限パターンをカンマ区切りで指定します。例：`'bash(rm *)'`。 | `headless`, `process` |
| `--permission-mode <mode>` | 権限モード：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 | `headless`, `process` |

## 権限モード

`--permission-mode` パラメータは、エージェントがツールの権限チェックをどのように処理するかを制御します：

| モード | 動作 |
|---|---|
| `default` | ツールアクションが承認を必要とする場合、エージェントが確認を求めます。 |
| `accept_edits` | 編集とみなされるファイルシステムコマンド（例：`mkdir`、`cp`）を自動承認します。その他のアクションは引き続き確認を求めます。 |
| `bypass_permissions` | セーフティチェックを除くツールアクションを自動承認します。監査レコードを必要とする allow 決定は、監査の永続化に失敗すると fail-closed になります。信頼できる自動化向けです。 |
| `dont_ask` | 承認が必要な操作を拒否します。ただし、明示的な許可ルールに一致する操作は実行できます。 |

`aliyun_api` 経由の Alibaba Cloud 書き込み API 呼び出しは別枠で保護されます。裸の `aliyun_api` allow ルールはそれらを一括承認せず、`bypass_permissions` 以外では、書き込み allow ルールは正規化された `product:action` ペアに正確に一致する必要があります。`bypass_permissions` はそれらを自動承認しますが、他の監査対象 allow 決定と同じく、権限監査レコードを永続化できない場合はアクションが拒否されます。信頼できる自動化で特定の書き込み API だけを許可する場合は、`aliyun_api(ros:CreateStack)` のような正確なルールを使用してください。

## よく使う起動コマンド

ローカル Web アプリを起動する：

```bash
iac-code
```

今回の実行で特定のモデルを指定する：

```bash
iac-code --prompt "VPC を作成" --model qwen3.7-max
```

ワンショットプロンプトを実行する：

```bash
iac-code --prompt "Create an OSS Bucket"
```

このリクエストだけ thinking を無効にしてワンショットプロンプトを実行する：

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

標準入力からプロンプトを読み取る：

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

単一プロンプトで最新のセッションを続けます。

```bash
iac-code --continue --prompt "VPC テンプレートを生成する"
```

`git *` に一致する bash コマンドを事前に許可する：

```bash
iac-code --prompt "git の状態を表示" --allowed-tools 'bash(git *)'
```

特定の Alibaba Cloud 書き込み API を事前に許可する：

```bash
iac-code --prompt "スタックをデプロイ" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

対話プロンプトなしで自動化実行する：

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
