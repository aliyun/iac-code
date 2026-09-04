---
sidebar_position: 2
title: IaC Code Skill のインストールと使用
description: Skill 対応エージェントに IaC Code を追加し、Alibaba Cloud インフラを管理します。
---

# IaC Code Skill のインストールと使用

IaC Code Skill を使うと、対応エージェントからクラウド構成の設計、ROS/Terraform テンプレートの生成・
レビュー、料金見積もり、既存リソースの選択、ROS スタック操作、デプロイを IaC Code に委任できます。
検証済み Runtime が含まれるため、IaC Code を別途インストールする必要はありません。

## ダウンロード

[最新の iac-code-skill.zip をダウンロード](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

この固定 URL は常に最新の安定版を指します。自動インストーラーは
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
からバージョン、不変 URL、サイズ、SHA-256 を取得し、`skill.url` と `skill.sha256` を使って検証できます。

## インストール

エージェントが `SKILL.md` 形式のローカル Skill に対応し、CPython 3.8～3.14 が使えることを確認します。
macOS/Linux は `python3`、Windows は `py -3` を使います。公式 Runtime は Apple Silicon macOS、
Linux x86_64、Windows x86_64 に対応し、ダウンロード前に OS と ABI を検証します。

ZIP をエージェント指定の Skill ディレクトリに展開します。アーカイブには最上位の `iac-code/` が含まれます。

```text
<Agent Skill root>/
└── iac-code/
    ├── SKILL.md
    ├── agents/openai.yaml
    └── scripts/iac_code.py
```

主なホストの配置先:

- **Codex**: 全プロジェクトでは `~/.agents/skills/iac-code/`、単一リポジトリでは
  `<repository>/.agents/skills/iac-code/`。詳細は [Codex Skills ドキュメント](https://developers.openai.com/codex/skills#where-codex-loads-local-skills)を参照してください。
- **Claude Code**: 全プロジェクトでは `~/.claude/skills/iac-code/`、単一リポジトリでは
  `<repository>/.claude/skills/iac-code/`。詳細は [Claude Code Skills ドキュメント](https://code.claude.com/docs/en/skills#where-skills-live)を参照してください。

エージェントを再起動するか新しいセッションを開きます。事前確認は展開先で実行します。

```bash
python3 scripts/iac_code.py ensure-runtime
```

Windows PowerShell では `py -3 scripts\iac_code.py ensure-runtime` を使います。初回はプラットフォーム向け
Runtime のサイズと SHA-256 を検証し、以後は検証済みコピーを再利用します。

## モデルと Alibaba Cloud ID の設定

Skill は既定で `~/.iac-code/` を使い、REPL、Web、Desktop アプリの既存設定を再利用します。別の場所は
`IAC_CODE_CONFIG_DIR` で指定できます。自動化では認証情報をシークレット管理から注入し、`SKILL.md`、
プロンプト、プロジェクト、シェル履歴に書かないでください。一時認証情報、RAM ロール、OAuth と最小権限を
推奨します。詳細は [LLM プロバイダー](../configuration/llm-providers.md)と
[Alibaba Cloud 認証情報](../configuration/alibaba-cloud-credentials.md)を参照してください。

## 動作モード

- **通常モード**は、リソース照会・変更、テンプレート作業、トラブルシューティング、対象が明確なデプロイの既定です。
- **Pipeline モード**は、明示的に指定した場合、または候補構成、料金比較、確認、デプロイまでの案内が必要な場合に使います。

通常は目的をそのまま記述し、比較フローが必要な場合だけ Pipeline を指定します。

## 最初のタスク

ホストエージェントの新しいセッションで、例えば次のように依頼します。

```text
iac-code を使って、このプロジェクトの ROS テンプレートをレビューしてください。ファイルは変更せず、セキュリティリスクと改善案を示してください。
```

Codex では `$iac-code`、Claude Code では `/iac-code` で Skill を明示選択できます。設定確認と Runtime 起動は自動で行われ、A2A Server
を手動起動する必要はありません。IaC Code は次の入力を待って一時停止することがあります。

- 操作の許可・拒否（`permission`）
- 質問への回答（`ask_user_question`）
- 候補構成の選択（`candidate_selection`）
- 最終案、料金、パラメーターを確認して確定、調整、再選択、キャンセル（`deployment_confirmation`）

対象、リージョン、影響、見積額を確認して回答してください。最初のデプロイ依頼は後の確認を事前承認しません。
完了後は同じセッションで会話を続けられます。進捗と質問は英語、簡体字中国語、スペイン語、フランス語、
ドイツ語、日本語、ポルトガル語に対応します。

## 更新とアンインストール

更新では安定版 ZIP を再ダウンロードし、`iac-code/` 全体を置き換えてホストを再起動します。ブリッジだけの
差し替えや Runtime URL の編集は行わないでください。アンインストールでは `iac-code/` を削除します。
Runtime も削除する場合は `cache list` を確認してから `cache clean ... --confirm` を実行します。

## トラブルシューティング

- `llm_not_configured`: モデル設定を完了してください。
- `cloud_credentials_not_configured`: Pipeline に必要な Alibaba Cloud 認証情報を設定してください。通常モードではクラウド API 不要の作業を警告付きで続行できます。
- `incompatible_host`: `ensure-runtime` で Python、OS、アーキテクチャ、ネットワーク、プロキシを確認し、対応ホストへ更新または移行してください。
- タスクの一時停止: 質問、権限、候補、デプロイ確認を待つ正常な状態です。中断後もセッションが残る場合は同じタスクを続行します。

Runtime の確認には `python3 scripts/iac_code.py cache list`、過去版の削除には
`python3 scripts/iac_code.py cache clean --runtime-tag <tag> --confirm`、Candidate の削除には
`python3 scripts/iac_code.py cache clean --candidates --confirm` を使います。現在・実行中の Runtime は保護されます。

## セキュリティ

- Runtime はランダムな `127.0.0.1` ポートとプロセスごとの Bearer token を使います。
- 成果物はワークスペース（必要に応じて `.iac-code-skill-results/`）に保存されます。
- 準備状態と権限の表示には認証情報の値を含みません。

## 関連ドキュメント

- [IaC Code 公式 Skills の概要](./skill-overview.md)
- [IaC Code Skill ホスト統合リファレンス](./skill-host-integration.md)
- [A2A プロトコル概要](./overview.md)
- [Runtime 設定](../configuration/runtime-configuration.md)
