---
sidebar_position: 3
title: IaC Code Skill ホスト統合リファレンス
description: Skill 対応ホストエージェントに IaC Code ブリッジを統合します。
---

# IaC Code Skill ホスト統合リファレンス

本書はエージェントおよび Skill 配布システムの開発者向けです。通常の利用者は
[IaC Code Skill のインストールと使用](./skill-integration.md)を参照してください。

## 統合モデルと設定

パッケージには `SKILL.md` と標準ライブラリだけで動く `scripts/iac_code.py` が含まれます。ホストは
CPython 3.8～3.14 でブリッジを実行し、stdout を安定した JSON、stderr を診断・進捗として扱います。
`jobId`、`contextId`、cursor、入力相関フィールドを保持し、エラー時に別 Runtime や直接クラウド API へ
フォールバックしてはいけません。

配布者は `SKILL.md` の隣に次の `config.json` を配置できます。

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

`channel` には `skill/` が付加されます。`pipelineName` の既定値は `selling_solution_first`、`selling` は
明示的な旧フロー用です。待機ポリシーの `null` は無期限です。未知・不正な値は拒否されます。この設定を
ユーザー依頼から生成、公開、またはタスク中に変更しないでください。

## ジョブの開始と追跡

完全な依頼を UTF-8 ファイルに書き、絶対ワークスペースで開始します。

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

既定は `normal`、比較・確認・デプロイの案内が必要な場合だけ `pipeline` です。言語は `en`、`zh`、`es`、
`fr`、`de`、`ja`、`pt`、`auto` から選び、返された `preferredLanguage` を保持します。
`llm_not_configured` は作成前に停止し、Pipeline の認証情報不足は `cloud_credentials_not_configured` です。

`--follow` は表示・対話境界、`turn_completed`、Pipeline 終端で返ります。`boundaryReached: true` なら
`userUpdates` をすべて表示し、同じ cursor から続けます。

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

`boundaryReached` は完了ではなく、`presentationRequired` は次の呼び出し前に表示が必要という意味です。
通常モードは `finalText` と `artifacts`、Pipeline 終端は `pipelineResult` と `artifacts` を正式結果にします。
診断・復旧時だけ次を使用します。

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

`state: input-required` なのに `inputRequired` がない場合は最新情報を報告し、ジョブを変更しません。

## ユーザー入力

各 `inputRequired` を厳格な対話境界として表示し、明示回答を待ちます。`kind`、`inputId`、
`requestTaskId`、`contextId`、存在する `toolUseId` を保持します。

| `kind` | ホストが表示する情報 | 応答 |
|---|---|---|
| `permission` | 目的、影響、対象、読み取り専用、デプロイ・安全概要、選択肢 | `allow_once` / `deny` |
| `ask_user_question` | 質問、選択肢、許可された自由入力 | 回答 |
| `candidate_selection` | 全候補、Mermaid 図、月額と内訳 | 候補 ID / 番号 |
| `deployment_confirmation` | 案、URL、見積もり、実効値、上書き、Preview、選択肢 | `confirm` / `adjust` / `reselect` / `cancel` |

相関フィールドを含む新しい UTF-8 JSON ファイルで同じジョブを再開します。

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

調整しない場合は `parameterOverrides` を省略します。元のデプロイ依頼やホスト承認から回答を推定しません。

## 継続、キャンセル、復旧

通常ターン完了後、または Pipeline から通常モードへ移行した後は、同じジョブを続けます。

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

同じ `jobId` と `contextId` を保持し、新しい `taskId` を受け入れます。これにより権限待ちやホスト中断から
復旧できます。全体のキャンセルは `python3 scripts/iac_code.py cancel --job-id <job-id>` で行います。

## エラーと Runtime

作成前のエラーは正式な結果です。`incompatible_host` の互換性情報を表示して停止し、pip、別 Runtime、
直接 API に切り替えません。Runtime は
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` にキャッシュされ、構成と整合性は
`skill-runtime/skill-package-contract.json` とリリースマニフェストで検証されます。削除はユーザーが
明示した場合だけ行います。

Runtime はランダムな `127.0.0.1` ポートとプロセス専用 Bearer token を使います。token、ローカル状態、
認証情報、環境値、未加工のツール入出力を公開しないでください。

## 関連ドキュメント

- [IaC Code 公式 Skills の概要](./skill-overview.md)
- [IaC Code Skill のインストールと使用](./skill-integration.md)
- [A2A プロトコル概要](./overview.md)
- [A2A プロトコルリファレンス](./protocol-reference.md)
