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
| `AGENTS.md` | 永続的な指示として読み込まれるユーザーメモリ |
| history files | 対話ワークフローの入力履歴 |

このディレクトリのファイルにはシークレットやローカル設定が含まれる場合があるため、コミットや共有は避けてください。

## メモリファイル

IaC Code には公開されているメモリ場所が 2 つあります。

| 場所 | 用途 |
|---|---|
| `<project-root>/AGENTS.md` | プロジェクトメモリ。プロジェクトで作業する全員に役立つ指示であれば、コミットできます。 |
| `<config-dir>/AGENTS.md` | ユーザーメモリ。`IAC_CODE_CONFIG_DIR` に従い、ローカルユーザー専用です。 |

別の instruction memory ファイル名を使うには `IAC_CODE_INSTRUCTION_MEMORY_FILE` を設定します。例: `IAC-CODE.md`。

プロジェクトの auto-memory トピックファイルは以下に保存されます。

```text
<config-dir>/projects/<project-key>/memory/
```

そのフォルダー内の `MEMORY.md` は auto-memory side call が使うトピックインデックスです。常時コンテキストとしては読み込まれません。auto-memory がオンの場合、IaC Code は関連するトピックファイルを選択し、隠し会話コンテキストとして追加できます。

## プロジェクト設定

ユーザーレベルの `~/.iac-code/settings.yml` に加えて、IaC Code は現在の作業ディレクトリからプロジェクトレベルの設定を読み込みます：

| ファイル | 範囲 |
|---|---|
| `.iac-code/settings.yml` | プロジェクト共有設定（コミットしても安全）。 |
| `.iac-code/settings.local.yml` | ローカル上書き（.gitignore に追加すべき）。 |

マージ順序：**ユーザー設定 → プロジェクト設定 → プロジェクトローカル設定 → CLI 引数**（後のソースが前のものを上書きします）。

## Provider リクエストポリシー

`settings.yml` の provider エントリには、OpenAI 互換 provider 向けのリクエストポリシーフィールドを設定できます。モデルが表示される回答 token と reasoning/thinking token を分けて扱う場合に役立ちます。

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| フィールド | スコープ | 説明 |
|---|---|---|
| `thinkingBudget` | Provider またはモデル | 正の整数の reasoning/thinking 予算。対応している provider に渡されます。 |
| `maxCompletionTokens` | Provider またはモデル | `max_completion_tokens` を使う provider/model 向けの、正の整数の上書き値。 |
| `effort` | Provider またはモデル | thinking effort の任意の上書き値。effort 制御に対応したモデルでのみ有効です。 |

`providers.<provider>.models.<model>` 以下のモデル単位の有効な値は、provider 単位の値を上書きします。無効な数値は無視されるため、IaC Code は provider 単位の値または組み込みのモデルポリシーにフォールバックします。

Alibaba Cloud DashScope と DashScope Token Plan では、IaC Code は `glm-5.2` と `kimi-k2.7-code` に組み込みで `thinkingBudget=8192` を設定しています。`maxCompletionTokens` が未設定の場合、リクエスト上限は通常の回答 token 上限に有効な thinking budget を加えて計算されます。

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
