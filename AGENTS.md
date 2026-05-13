# AGENTS.md

This file applies to the entire repository.

## Project Overview

- `iac-code` is a Python 3.10+ Infrastructure as Code assistant focused on Alibaba Cloud ROS / Terraform template generation and management.
- Source code uses a `src/` layout with the main package at `src/iac_code/` and tests at `tests/`.
- The CLI entry point is declared in `pyproject.toml` as `iac-code = "iac_code.cli.main:app"`.

## Common Commands

- Install dependencies and hooks: `make install`
- Run tests: `make test`
- Run coverage: `make coverage`
- Run lint/type check: `make lint`
- Format code: `make format`
- Update translation files: `make translate`
- Start CLI locally: `make run`
- Start CLI in debug mode: `make dev`

Prefer using `uv` and existing Makefile targets. When adding new dependencies, update `pyproject.toml` and `uv.lock` — do not bypass the project's dependency management.

## Code Standards

- Target version is Python 3.10; use modern type annotations and standard library capabilities.
- Ruff is configured in `pyproject.toml`: line width 120, rules `E/F/I/N/W` enabled.
- Keep changes focused; do not refactor, rename, or move files outside the scope of the current task.
- Follow existing module boundaries:
  - `agent/` — agent loop, system prompts, and message types.
  - `commands/` — REPL commands.
  - `providers/` — LLM provider adapters.
  - `services/` — session, context, credentials, telemetry, and other business services.
  - `tools/` — agent-callable tools.
  - `skills/` — skill loading, rendering, discovery, and bundled skill resources.

## Testing Requirements

- For behavioral changes, prioritize adding or updating pytest cases under `tests/`.
- Tests must not depend on real LLMs, real Alibaba Cloud accounts, real network calls, or local user configuration.
- When testing environment variables and credential reading, use `tmp_path`, `patch.dict`, or mocks to isolate state.
- For small changes, run at least the relevant tests; after changes to shared logic, CLI, providers, credentials, or tool execution paths, run `make test` and `make lint` if necessary.

## Configuration and Credentials

- The runtime configuration directory is `~/.iac-code/`, containing `.credentials.yml`, `.cloud-credentials.yml`, `settings.yml`, and input history.
- Do not commit, print, or hard-code real API keys, AccessKeys, Secrets, tokens, cookies, or user configuration file contents.
- Alibaba Cloud credential-related tests must use fake values and avoid triggering real cloud APIs.

## i18n and Bundled Skills

- After modifying user-facing translatable strings, check whether `make translate` needs to be run.
- Markdown files and scripts under `src/iac_code/skills/bundled/iac_aliyun/` are bundled skill resources; when modifying them, maintain consistency between templates, parameter descriptions, and conversion scripts.
- Do not commit generated translations, build artifacts, or coverage outputs unless the current task explicitly requires it.

## Git Collaboration

- The workspace may contain changes from others; do not revert changes you did not make.
- Do not use `git reset --hard`, force push, or other destructive operations unless the user explicitly requests it.
- Before committing, verify that `git status` only includes files related to the current task.
