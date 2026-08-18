---
sidebar_position: 7
title: Skill 統合
description: 外部エージェントがパッケージ化された iac-code Skill と Skill Runtime を通じて iac-code を駆動する。
---

# Skill 統合

iac-code は、外部エージェント向けにパッケージ化された Skill を同梱しています。外部エージェント（プラナーエージェントやエージェントプラットフォーム）は iac-code の Python パッケージをインストールせず、headless コマンドも直接呼び出しません。標準ライブラリのみで書かれたブリッジスクリプト経由でローカルの認証済み A2A runtime を駆動し、ROS/Terraform テンプレート生成、コスト見積もり、リソース選択、デプロイなどの Alibaba Cloud インフラ作業を実行します。

## 構成要素

| コンポーネント | 場所 | 説明 |
|---|---|---|
| Skill パッケージ | `skills/iac-code/` | `SKILL.md` の使用手順、`agents/` のエージェントメタデータ、ブリッジスクリプト `scripts/iac_code.py` |
| Skill Runtime | プラットフォームごとに公開 | iac-code A2A サーバーを組み込んだ CPython 3.12 ネイティブ実行ファイル |
| 配布契約 | `skill-runtime/skill-package-contract.json`、`skill-runtime/publisher-contract.json` | Skill パッケージと発行者の形式・検証ルール |

ブリッジスクリプトは完全に Python 標準ライブラリで書かれており、Python 3.8 以上との互換性を保ちます。CI は 3.8–3.14 の全マトリックスでコンパイルとスモーク実行を行います。ブリッジにサードパーティ依存や新しいバージョン専用の構文を追加しないでください。

## Runtime の取得とキャッシュ

ブリッジは初回実行時にマニフェストを読み取り、現在のプラットフォームの成果物をダウンロードし、サイズと SHA-256 を検証してからインストールし、`<IAC_CODE_CONFIG_DIR または ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` 配下にキャッシュします。

- `python3 scripts/iac_code.py ensure-runtime` — Runtime を事前に準備します。キャッシュ済みなら再利用します。
- `python3 scripts/iac_code.py cache list` — インストール済み Runtime と候補パッケージを表示します。
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — Runtime キャッシュまたは候補パッケージを削除します。明示的な `--confirm` が必要です。

## 構成プリフライト

`start` はジョブ作成前に Runtime を通じて構成の準備状況チェックを実行します。プリフライトは秘密情報そのものを読み取らず、準備状況のみを報告します:

| 状況 | 結果 |
|---|---|
| LLM プロバイダーまたは API Key が不完全 | `llm_not_configured` を返し、ジョブ作成を拒否 |
| selling Pipeline かつ Alibaba Cloud 資格情報が不完全 | `cloud_credentials_not_configured` を返し、ジョブ作成を拒否 |
| normal モードかつ Alibaba Cloud 資格情報が不完全 | クラウド API を呼び出さない作業は続行可能だが、プリフライト警告を出す |

## コマンドリファレンス

| コマンド | 用途 |
|---|---|
| `start` | ジョブ作成: `--mode normal|pipeline`、`--pipeline-name`、`--cwd` 絶対ワークスペース、`--prompt-file` UTF-8 プロンプトファイル、`--language auto|en|zh|es|fr|de|ja|pt`、任意で `--follow` |
| `follow` | 次のインタラクション境界までイベントストリームを消費: `--job-id`、`--cursor`、`--wait-seconds`（既定 60 秒、最大 120 秒） |
| `continue` | 同じジョブで normal モードの会話を継続: `--job-id`、`--prompt-file`、任意で `--follow` |
| `respond` | 保留中の入力への応答。[ユーザー入力](#input-required)を参照 |
| `poll` | 診断・復旧専用の一回限りポーリング。`follow` の代用にしないこと |
| `cancel` | ジョブのキャンセル |
| `ensure-runtime` / `cache list` / `cache clean` | Runtime とキャッシュの管理 |

`start --follow` と `follow` はステップ境界と低頻度ハートビートを stderr に書き出し、stdout には 1 件のみ制限された JSON 結果を出力します。

## インタラクション境界 {#boundaries}

`--follow` は、次のステップ境界、権限リクエスト、ユーザー質問、候補選択、`turn_completed`、または終端状態に達するまでイベントストリームを消費します。境界の結果には次が含まれます:

- `boundaryReached: true` — 境界に達したことを示します。ジョブの完了を意味し**ません**。
- `presentationRequired: true` と `userUpdates` — ユーザーにすぐ表示できるローカライズ済み文字列。
- 継続に必要な `cursor`。

外部エージェントは、受け取った `userUpdates` の文字列をすべてユーザーに見える返信で提示してから、返された `cursor` で直ちに `follow` を再び呼び出す必要があります。follow の実行中は、インフラタスクに並行して回答したり、無関係な質問を投げかけたりしないでください。

## ユーザー入力 {#input-required}

結果に `inputRequired` が含まれる場合、ユーザー入力が必要です。種類は 3 つあります:

- `permission` — ツールまたはデプロイの権限リクエスト。エンベロープには `inputId`、`toolUseId`、タイトル、目的、影響、対象、読み取り専用フラグ、`safeSummary` が含まれ、デプロイリクエストには `deploymentSummary` も含まれます。外部エージェントは自身の権限ポリシーに従って決定してください。直接実行時に確認なしで進む同等の操作なら `allow_once`、ポリシーが拒否するなら `deny`、それ以外はユーザーに質問します。iac-code 自身の拒否決定は覆せません。
- `ask_user_question` — 選択式または自由記述の質問。プロンプトと選択肢をそのまま提示します。自由記述は `allowFreeText` が `true` の場合のみ受け付けます。
- `candidate_selection` — Pipeline のプラン選択。各候補のサマリー、アーキテクチャ図（Mermaid）、月額総コスト、コスト内訳をまず提示し、選択した候補を返します。提示された価格を概算で置き換えないでください。

`respond` には 2 つの形式があります:

```bash
# 権限のインライン決定
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# 質問と候補選択は応答ファイルを使用
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

応答は保留中入力の相関フィールドをすべてそのまま保持する必要があり、現在の `kind`、`inputId`、`requestTaskId`、`contextId` にのみ束縛されます。他のリクエストの応答を再利用したり、リソース選択の応答をデプロイ確認として解釈し直したりしないでください。

## 言語制御

`start --language` はジョブの優先言語を設定します（不明な場合は `auto`）。そのジョブのすべての結果は `preferredLanguage` を繰り返します。これを永続的な制御状態として扱ってください。進捗、質問、権限プロンプト、候補プラン、最終結果はその言語で提示され、プロトコルフィールド名、列挙値、ID、コマンドは変わりません。権威あるテキストがすでにその言語を使っている場合は、そのまま提示するか同じ言語で要約し、中国語のユーザー向けコンテンツを英語に翻訳しないでください。

## A2A プロトコルとの関係

ブリッジは HTTP A2A JSON-RPC でローカル Runtime と通信します。タスク状態、artifact、権限インタラクションはすべて iac-code の A2A プロトコルを再利用します:

- 権限のサイドバンド応答は `schemaVersion 1` のメッセージ形式を使います。フィールドと制約は[プロトコルリファレンス](./protocol-reference.md)を参照してください。
- Pipeline モードで `candidatePresentation: rich-v1` を渡すと、構造化された候補表示ペイロードが返されます。
- ジョブ結果の状態は A2A タスク状態に対応します。`turn_completed` は normal ターンの完了を示し、Pipeline の終端状態は `completed`、`failed`、`canceled`、`rejected` で、`pipelineResult` と `artifacts` が権威ある結果です。

## セキュリティ境界

- Runtime は `127.0.0.1` のランダムポートでのみリッスンします。起動ごとに新しいランダム Bearer token が生成され、ブリッジのすべてのリクエストがそれを携行します。
- ブリッジは成果物と結果をジョブワークスペース内に保ち、結果はワークスペースの `.iac-code-skill-results/` に書き込まれます。
- プリフライト報告と権限表示フィールドはいずれもサニタイズ済みです。秘密情報や資格情報が表示フィールドに出ることはありません。
