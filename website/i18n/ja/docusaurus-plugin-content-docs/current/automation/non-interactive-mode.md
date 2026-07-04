---
title: 非対話モード
description: 引数または stdin からワンショットプロンプトを実行します。
---

# 非対話モード

非対話モードは単一のプロンプトを実行して終了します。REPL に留まらずに、繰り返し可能なタスクの出力を IaC Code に生成させたい場合に使用します。

`--prompt` でプロンプトを直接渡します：

```bash
iac-code --prompt "Create an OSS Bucket"
```

`--prompt -` で標準入力からプロンプトを読み取ります：

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

呼び出し元が構造化された出力を必要とする場合は `--output-format` を使用します：

```bash
iac-code --prompt "Create an OSS Bucket" --output-format json
```

エージェントの作業時間を制限するには `--max-turns` を使用します：

```bash
iac-code --prompt "Create a VPC" --max-turns 20
```

サポートされる出力形式：

| 形式 | 目的 |
|---|---|
| `text` | 人間が読みやすい出力。デフォルトです。 |
| `json` | 最終レスポンスを解析する呼び出し元向けの単一 JSON 結果。 |
| `stream-json` | 増分進捗を処理する呼び出し元向けのストリーミング JSON イベント。 |

## セッションバックアップ

`IAC_CODE_CONFIG_BACKUP_DIR` を設定すると、非対話実行は重要なチェックポイントで v2 セッションをミラーします。通常のターン終了チェックポイントは `normal_turn_end` を使用します。この時点のバックアップ失敗は `warning` としてログに記録され、`.backup-state.json` に保存されますが、完了レスポンスを失敗させず、最終出力に warning フィールドを追加しません。Pipeline mode には独自の重要なバックアップ制御があります。

## 自動化における権限制御

非対話実行時に `--permission-mode` を使用して、エージェントのツール承認の処理方法を制御します：

```bash
iac-code --prompt "Deploy the stack" --permission-mode bypass_permissions
```

`bypass_permissions` では、セーフティチェックを除くツールアクションが自動承認されますが、監査レコードを必要とする allow 決定は、監査の永続化に失敗すると引き続き fail-closed になります。Alibaba Cloud 書き込み API は `bypass_permissions` 以外では引き続き別枠で保護されます。より範囲を絞った信頼できる自動化では、`bypass_permissions` を使わず、必要な書き込み API をそれぞれ明示的に許可してください：

```bash
iac-code --prompt "Deploy the stack" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

エージェントが実行できる内容を制限するには、`--allowed-tools` と `--disallowed-tools` を組み合わせます：

```bash
iac-code --prompt "Check the stack status" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

すべての起動パラメータは[コマンドラインオプション](../cli/command-line-options.md)を参照してください。
