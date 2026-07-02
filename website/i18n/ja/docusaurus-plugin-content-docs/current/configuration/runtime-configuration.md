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

`IAC_CODE_CONFIG_DIR` 環境変数を設定すると、ディレクトリを変更できます（`~` と `$VAR` の展開をサポート）。設定すると、永続化されるすべての成果物 — 認証情報、設定、履歴、`projects/`、`image-cache/`、`tool-results/`、`logs/`、`memory/`、`a2a/`、`telemetry/`、`skills/` — が新しい場所に追従します。起動/デバッグログはデフォルトで `<config-dir>/logs/` に置かれ、`IAC_CODE_LOG_DIR` で別に移動できます。権限監査レコードは `<config-dir>/logs/` に残ります。

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
    thinkingEnabled: true
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingEnabled: false
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| フィールド | スコープ | 説明 |
|---|---|---|
| `thinkingEnabled` | Provider またはモデル | 任意の boolean thinking スイッチ。`true` は対応する provider/model に thinking の有効化を要求し、`false` は無効化を要求します。省略時は provider/model のデフォルトを維持します。 |
| `thinkingBudget` | Provider またはモデル | 正の整数の reasoning/thinking 予算。対応している provider に渡されます。 |
| `maxCompletionTokens` | Provider またはモデル | `max_completion_tokens` を使う provider/model 向けの、正の整数の上書き値。 |
| `effort` | Provider またはモデル | thinking effort の任意の上書き値。effort 制御に対応したモデルでのみ有効です。 |

`providers.<provider>.models.<model>` 以下のモデル単位の有効な値は、provider 単位の値を上書きします。無効な数値は無視されるため、IaC Code は provider 単位の値または組み込みのモデルポリシーにフォールバックします。

Alibaba Cloud DashScope と DashScope Token Plan では、IaC Code は `glm-5.2` と `kimi-k2.7-code` に組み込みで `thinkingBudget=8192` を設定しています。`maxCompletionTokens` が未設定の場合、リクエスト上限は通常の回答 token 上限に有効な thinking budget を加えて計算されます。

A2A リクエストは、`message.metadata.iac_code.thinking` または `iac-code a2a-client call` の `--thinking-enabled`、`--thinking-effort`、`--thinking-budget` フラグを通じて、この設定を 1 回の message turn だけ上書きできます。明示的な A2A thinking metadata が送信されない場合、runtime は上記の設定と provider の通常のデフォルトを使用します。DashScope compatible-mode の base URL を持つ汎用 `openai_compatible` provider では、明示的な thinking ポリシーがある場合にのみ、iac-code は DashScope ネイティブの thinking wire format に切り替えます。

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| フィールド | 説明 |
|---|---|
| `mode` | 権限モード：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |
| `allow` | 自動承認するツール権限パターンのリスト。 |
| `deny` | 自動拒否するツール権限パターンのリスト。 |
| `ask` | 常に確認が必要なツール権限パターンのリスト。 |
| `additional_directories` | cwd 以外でエージェントが書き込み可能な追加ディレクトリ。 |
| `audit` | ローカルの権限監査ログ設定。 |

### パターン構文

ツール権限パターンは `tool_name(rule)` の形式に従います：

| パターン | 意味 |
|---|---|
| `bash` | すべての bash コマンドにマッチ（ツール名のみ）。 |
| `bash(git *)` | `git` で始まる bash コマンドにマッチ。 |
| `bash(curl:*)` | `curl` で始まる bash コマンドにマッチ。 |
| `write_file` | すべての write_file ツール呼び出しにマッチ。 |
| `aliyun_api(ros:CreateStack)` | Alibaba Cloud API の product/action ペア 1 つにマッチ。 |

ルールは次の順序で評価されます：**deny → ask → allow → デフォルト動作**。CLI 引数（`--allowed-tools`、`--disallowed-tools`）が最も高い優先度を持ちます。

### Alibaba Cloud API 権限

`aliyun_api` は、読み取り専用 API 呼び出しとクラウドリソースを変更し得る呼び出しを区別します。読み取り専用 API アクションは自動的に許可されます。読み取り専用ではない API 呼び出しには確認、またはその product/action に対する正確な allow ルールが必要です。例：

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

裸の `aliyun_api` allow ルールは Alibaba Cloud の書き込み API を一括承認しません。`bypass_permissions` 以外では、書き込み allow ルールは正規化された `product:action` ペアに正確に一致する必要があります。`bypass_permissions` モードでは、保護された Alibaba Cloud 書き込み API は自動承認されますが、監査レコードを必要とする allow 決定は、監査の永続化に失敗すると引き続き fail-closed になります。ワイルドカードは deny ルールや ask ルール、読み取り専用ルールのマッチングには引き続き有用です。

ROA 形式のリクエストは、メソッドが `GET` で body がない場合にのみ読み取り専用として扱われます。読み取り専用ではない ROA リクエストは、RPC 形式の書き込み API と同じく、正規化された `product:action` に正確に一致する allow ルールが必要です。`aliyun_api(cs:CreateCluster)` のような正確なルールなら書き込みを承認できますが、ワイルドカードの allow ルールは読み取り専用ではない呼び出しを引き続き承認しません。

### 権限監査ログ

ユーザープロンプト、ツールキャッシュ境界、自動化承認、または resolver 承認をまたぐ権限決定は、次のファイルに追記されます：

```text
<config-dir>/logs/permission-audit.jsonl
```

デフォルトでは `~/.iac-code/logs/permission-audit.jsonl` です。権限監査ログは `IAC_CODE_CONFIG_DIR` に従います。`IAC_CODE_LOG_DIR` で移動されるのは起動/デバッグログだけです。監査 writer はファイルロック付きで JSONL レコードを追記し、ファイルをローテーションし、OS が対応している場合はローカルファイル権限を制限します。通常の読み取り専用自動許可は省略されることがありますが、拒否、プロンプト、キャッシュ済み決定、自動化承認、resolver 承認、その他の監査対象の権限境界は記録されます。

監査設定は `permissions.audit` の下で構成します：

| フィールド | 既定値 | 説明 |
|---|---:|---|
| `include_tool_input` | `false` | JSONL 監査レコードに、形状のみのツール入力を含めます。文字列値は型、長さ、フィンガープリントとして保存されます。secret らしいキーはマスクされます。ホワイトリスト外のフィールド名はフィンガープリントで表される場合があります。業務 payload の生文字列は書き込まれません。Alibaba Cloud API の項目では安全な操作サマリーも保持します。 |
| `max_file_bytes` | `10485760` | `permission-audit.jsonl` がこのサイズを超えたらローテーションします。 |
| `max_files` | `5` | 保持するローテーション済み監査ファイル数。組み込み上限を超える値は上限に丸められます。 |

監査レコードを必要とする allow 決定を監査ログへ永続化できない場合、IaC Code は fail-closed となり、監査証跡なしで実行する代わりにそのアクションを拒否します。
