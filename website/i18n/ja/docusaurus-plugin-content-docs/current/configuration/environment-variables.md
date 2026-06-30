---
title: 環境変数
description: サポートされるすべての環境変数と優先順位ルール。
---

# 環境変数

IaC Code は CLI 引数、環境変数、設定ファイルから設定を読み取ります。優先順位は以下の通りです：

```text
CLI 引数 > 環境変数 > 設定ファイル
```

環境変数は、設定ファイルを編集せずに CI/CD パイプライン、コンテナ、一時的な上書きに便利です。

## LLM 設定

| 変数 | 説明 |
|---|---|
| `IAC_CODE_PROVIDER` | モデルプロバイダー名（大文字小文字不問）。有効な値：`DashScope`、`DashScope Token Plan`、`OpenAI`、`Anthropic`、`DeepSeek`、`Gemini`、`Azure OpenAI`、`ModelScope`、`Kimi CN`、`Kimi Intl`、`MiniMax CN`、`MiniMax Intl`、`ZhiPu CN`、`ZhiPu Intl`、`Volcengine CN`、`SiliconFlow CN`、`SiliconFlow Intl`、`Aliyun CodingPlan`、`Aliyun CodingPlan Intl`、`ZhiPu CN CodingPlan`、`ZhiPu Intl CodingPlan`、`Volcengine CodingPlan`、`OpenAPI Compatible`、`Anthropic Compatible`、`OpenRouter`、`Ollama`、`LM Studio` |
| `IAC_CODE_MODEL` | モデル名 |
| `IAC_CODE_BASE_URL` | `OpenAPI Compatible` と `Anthropic Compatible` 専用の API エンドポイント。他のプロバイダーでは無視されます |
| `IAC_CODE_API_KEY` | プロバイダー API キー。`.credentials.yml` のアクティブプロバイダーのキーを上書きします |

詳細は [LLM プロバイダー](./llm-providers.md) をご覧ください。

## Alibaba Cloud 認証情報

| 変数 | 説明 |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS トークン。設定すると認証モードが STS に切り替わります |
| `ALIBABA_CLOUD_REGION_ID` | デフォルトリージョン |

詳細は [Alibaba Cloud 認証情報](./alibaba-cloud-credentials.md) をご覧ください。

## テレメトリ

| 変数 | 説明 |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` / `true` / `yes` / `on` に設定すると、重要でないテレメトリトラフィックを無効にします |
| `DISABLE_TELEMETRY` | `1` / `true` / `yes` / `on` に設定すると、すべてのテレメトリを無効にします |
| `IAC_CODE_TELEMETRY_ENDPOINT` | ベース OTLP エンドポイント。個別のシグナルエンドポイントはこの値がデフォルトになります |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | トレース用のオーバーライドエンドポイント |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | メトリクス用のオーバーライドエンドポイント |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | ログ用のオーバーライドエンドポイント |
| `IAC_CODE_TELEMETRY_HEADERS` | カスタム OTLP ヘッダー（JSON またはキー=値形式） |

## その他

| 変数 | 説明 |
|---|---|
| `IAC_CODE_CONFIG_DIR` | ランタイム設定ディレクトリを上書き（デフォルト `~/.iac-code/`）。`~` と `$VAR` の展開をサポート。永続化されるすべての成果物（認証情報、設定、履歴、projects、image-cache、skills、telemetry など）はこのディレクトリに従います |
| `IAC_CODE_LOG_DIR` | ローカルの起動/デバッグログディレクトリを上書き（デフォルト `<config-dir>/logs/`）。`~` と `$VAR` の展開をサポート。権限監査レコードは引き続き `<config-dir>/logs/permission-audit.jsonl` に保存されます |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | `permissions.audit.include_tool_input` を上書きします。`1` / `true` / `yes` / `on` に設定すると、権限監査レコードに形状のみのツール入力を含め、業務 payload の生文字列の代わりに型/長さ/フィンガープリントを記録し、ホワイトリスト外のフィールド名もフィンガープリント化します |
| `IAC_CODE_ENV` | デプロイ環境ラベル（デフォルト：`production`） |
| `IAC_CODE_TENANT_ID` | テレメトリ用テナント識別子。`iac_tenant_` プレフィックスが付いていない場合は自動的に付加されます |
| `IAC_CODE_GIT_BASH_PATH` | Windows で Git Bash が PATH にない場合の `bash.exe` パス |
| `IAC_CODE_A2A_PUSH_KEYRING` | 環境管理された A2A 暗号化プッシュシークレットキーリング（JSON 形式） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 標準 OpenTelemetry エンドポイント。設定すると OTLP エクスポートが有効になります |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | スパンで GenAI メッセージ/ツールコンテンツをキャプチャ：`SPAN_ONLY`、`EVENT_ONLY`、`SPAN_AND_EVENT` |
