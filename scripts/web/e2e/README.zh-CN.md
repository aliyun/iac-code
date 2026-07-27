# Web 契约 E2E

`run_contract_scenario.py` 启动真实 iac-code Web 应用、确定性 OpenAI-compatible provider、Aliyun transport
fixture 和本地 OTLP 接收器。它通过 HTTP API 创建 normal 会话并调用 `aliyun_api`，随后使用真实 Chrome
打开会话、检查 DOM、刷新页面并再次检查恢复后的 DOM。

```bash
uv run python scripts/web/e2e/run_contract_scenario.py \
  --run-dir /tmp/iac-code-contract-e4
```

验收覆盖：Web 请求与流式响应、持久化 business body、`aliyun_http` 内部 metadata、API/DOM 无 metadata
泄漏、刷新恢复、以及 provider attempt 唯一终态。`--skip-browser` 仅用于定位后端问题，不能作为完整 E4
通过证据。

关键产物包括 `summary.json`、`browser-audit.json`、`browser.png`、`aliyun-contract-audit.json`、
`telemetry-audit.json`、Web server 日志和公开 API payload 快照。
