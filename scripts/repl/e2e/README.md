# REPL Pipeline E2E Runner

This directory contains real terminal end-to-end helpers for pipeline behavior. The runner drives the REPL through a real PTY and is POSIX-only because it uses `pexpect`.

Run from the repository root:

```bash
uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

By default, run artifacts are written under the system temporary directory, in:

```text
iac-code-repl-e2e-runs/<scenario>/<timestamp>-<pid>-<id>/
```

Use `--run-dir` to choose a fixed collection directory for local debugging or CI smoke artifacts.

The runner is for manual or smoke validation. It uses the developer's configured provider and may call real Alibaba Cloud tools when `--allow-real-cloud` is enabled. Non-multimodal scenarios default to `deepseek-v4-flash-0731`, while every `image-*` scenario defaults to `qwen3.8-max`; mixed runs select the model per scenario, and an explicit `--model` overrides all selected scenarios. The real E5 read-only canary also defaults to `deepseek-v4-flash-0731`, while deterministic provider fixtures keep their fixture model. Automated unit tests must not require real LLMs or real cloud credentials; pytest coverage for this directory is limited to pure helpers and argument behavior.
