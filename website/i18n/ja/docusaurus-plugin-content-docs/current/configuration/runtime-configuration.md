---
title: 設定
description: ランタイム設定の優先順位とローカルファイル。
---

# 設定

IaC Code は CLI 引数、環境変数、およびランタイム設定ディレクトリ内のファイルから設定を読み取ります。

設定の優先順位：

```text
CLI 引数 > 環境変数 > 設定ファイル
```

ランタイムディレクトリは既定で以下です：

```text
~/.iac-code/
```

`IAC_CODE_CONFIG_DIR` 環境変数を設定すると、ディレクトリを変更できます（`~` と `$VAR` の展開をサポート）。設定すると、永続化されるすべての成果物 — 認証情報、設定、履歴、`projects/`、`image-cache/`、`tool-results/`、`logs/`、`memory/`、`a2a/`、`telemetry/`、`skills/` — が新しい場所に追従します。

主要ファイル：

| ファイル | 説明 |
|---|---|
| `.credentials.yml` | LLM 認証情報 |
| `.cloud-credentials.yml` | クラウドプロバイダー認証情報 |
| `settings.yml` | 選択されたプロバイダー、モデル、および関連設定 |
| `IAC-CODE.md` | 永続的な指示として読み込まれるユーザーメモリ |
| history files | 対話ワークフローの入力履歴 |

このディレクトリのファイルにはシークレットやローカル設定が含まれる場合があるため、コミットや共有は避けてください。

## メモリファイル

IaC Code には公開されているメモリ場所が 2 つあります。

| 場所 | 用途 |
|---|---|
| `<project-root>/IAC-CODE.md` | プロジェクトメモリ。プロジェクトで作業する全員に役立つ指示であれば、コミットできます。 |
| `<config-dir>/IAC-CODE.md` | ユーザーメモリ。`IAC_CODE_CONFIG_DIR` に従い、ローカルユーザー専用です。 |

プロジェクトの auto-memory トピックファイルは以下に保存されます。

```text
<config-dir>/projects/<project-key>/memory/
```

そのフォルダー内の `MEMORY.md` はトピックインデックスです。auto-memory がオンの場合、IaC Code は side call を使って関連するトピックファイルを選択し、隠し会話コンテキストとして追加できます。

## プロジェクト設定

ユーザーレベルの `~/.iac-code/settings.yml` に加えて、IaC Code は現在の作業ディレクトリからプロジェクトレベルの設定を読み込みます：

| ファイル | 範囲 |
|---|---|
| `.iac-code/settings.yml` | プロジェクト共有設定（コミットしても安全）。 |
| `.iac-code/settings.local.yml` | ローカル上書き（.gitignore に追加すべき）。 |

マージ順序：**ユーザー設定 → プロジェクト設定 → プロジェクトローカル設定 → CLI 引数**（後のソースが前のものを上書きします）。

## ツール権限設定

`settings.yml` の `permissions` セクションで、どのツールアクションを許可、拒否、または確認を必要とするかを設定します：

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| フィールド | 説明 |
|---|---|
| `mode` | 権限モード：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |
| `allow` | 自動承認するツール権限パターンのリスト。 |
| `deny` | 自動拒否するツール権限パターンのリスト。 |
| `ask` | 常に確認が必要なツール権限パターンのリスト。 |
| `additional_directories` | cwd 以外でエージェントが書き込み可能な追加ディレクトリ。 |

### パターン構文

ツール権限パターンは `tool_name(rule)` の形式に従います：

| パターン | 意味 |
|---|---|
| `bash` | すべての bash コマンドにマッチ（ツール名のみ）。 |
| `bash(git *)` | `git` で始まる bash コマンドにマッチ。 |
| `bash(curl:*)` | `curl` で始まる bash コマンドにマッチ。 |
| `write_file` | すべての write_file ツール呼び出しにマッチ。 |

ルールは次の順序で評価されます：**deny → ask → allow → デフォルト動作**。CLI 引数（`--allowed-tools`、`--disallowed-tools`）が最も高い優先度を持ちます。
