# AGENTS.md

This file applies to the entire repository.

## Project Overview

- `iac-code` is a Python 3.10+ Infrastructure as Code assistant focused on Alibaba Cloud ROS / Terraform template generation and management.
- Source code uses a `src/` layout with the main package at `src/iac_code/` and tests at `tests/`.
- The CLI entry point is declared in `pyproject.toml` as `iac-code = "iac_code.cli.main:app"`. Beyond the default interactive REPL, the same entry point exposes additional run modes as subcommands: `iac-code web` (local Web app), `iac-code acp` (ACP server), `iac-code a2a` (A2A 1.0 server), `iac-code mcp ...` (MCP management), and `iac-code a2a-client ...` (A2A client).
- The native Desktop application uses Tauri 2 with the Python package bundled as a sidecar. It is a desktop shell for the existing product rather than a separate implementation of the agent or Web application.
- The repository also ships an external iac-code Skill package (`skills/iac-code/`) and a CPython 3.12 Skill Runtime release pipeline (`skill-runtime/`) so external agents can operate iac-code over A2A.

## Common Commands

- Install dependencies and hooks: `make install`
- Run tests: `make test` (current interpreter; `make test PY=all` runs the full 3.10–3.14 matrix, `make test PY=3.x` runs one version; tests run in parallel via `-n auto`)
- Run coverage: `make coverage`
- Run lint and type check: `make lint` (runs both `ruff check` and `ty check src/`)
- Format code: `make format`
- Extract, update, and compile translations (both `messages` and `webui` domains): `make translate`
- Start CLI locally: `make run`
- Start CLI in debug mode: `make dev`
- Run Desktop Python tests: `uv run pytest -q tests/desktop`
- Run Desktop host tests: `cargo test --manifest-path desktop/src-tauri/Cargo.toml`
- Run Desktop updater tests: `cargo test --manifest-path desktop/src-tauri/Cargo.toml --features updater`
- Run Desktop helper tests: `cargo test --manifest-path desktop/helpers/Cargo.toml`
- Install Desktop JavaScript dependencies: `cd desktop && npm ci`
- Build a native Desktop package from `desktop/`: `npm run build:macos`, `npm run build:windows`, `npm run build:appimage`, or `npm run build:deb`

Prefer using `uv` and existing Makefile targets. When adding new dependencies, update `pyproject.toml` and `uv.lock` — do not bypass the project's dependency management.

## Code Standards

- Target version is Python 3.10; use modern type annotations and standard library capabilities. The codebase must stay compatible across the 3.10–3.14 test matrix.
- Ruff is configured in `pyproject.toml`: line width 120, `target-version = "py310"`, lint rules `E/F/I/N/W` enabled. Type checking uses `ty` (Astral's type checker), not mypy.
- Keep changes focused; do not refactor, rename, or move files outside the scope of the current task.
- Follow existing module boundaries. User-facing interfaces:
  - `cli/` — Typer CLI application and subcommands (default REPL plus the `web`/`acp`/`a2a`/`mcp` run modes).
  - `ui/` — terminal REPL UI (components, dialogs, keybindings, input suggestions).
  - `web/` — local Web app: server, session/runtime management, SSE event bridging, output/diagram derivation, and the `static/` frontend.
  - `desktop/` — Tauri/Rust host, bootstrap and recovery UI, installers, icons, native packaging, and release automation.
  - `src/iac_code/desktop/` — Python Desktop runtime for control IPC, loopback ports, subprocess environment, Git Bash and tool discovery, diagnostics, and installation recovery. `desktop/sidecar/sidecar_entry.py` delegates to `iac_code.desktop.__main__`.
- External Skill integration (repository root, distinct from the bundled agent skills in `src/iac_code/skills/`):
  - `skills/iac-code/` — the packaged external Skill: `SKILL.md`, agent metadata (`agents/`), and `scripts/iac_code.py`, a bridge that drives iac-code over A2A. The bridge must stay standard-library-only and compatible with Python 3.8+ (CI compiles and smoke-runs it on 3.8–3.14); do not add third-party imports or newer-only syntax. `tests/skill_bridge/` validates the bridge and the release scripts offline — keep those tests free of network and cloud dependencies.
  - `skill-runtime/` — release tooling for the CPython 3.12 Skill Runtime executable (build/package/manifest scripts and PyInstaller spec) plus the skill-package and publisher contracts. Build outputs go to `skill-runtime/dist/` (git-ignored); never commit runtime archives, skill ZIPs, or generated manifests.
- Agent core:
  - `agent/` — agent loop, system prompts, and message types.
  - `commands/` — REPL commands.
  - `tools/` — agent-callable tools (including `bash/` and `cloud/`).
  - `skills/` — skill loading, rendering, discovery, and bundled skill resources (`bundled/`).
  - `memory/` — project and agent memory management and recall.
- Providers and services:
  - `providers/` — LLM provider adapters.
  - `services/` — session, context, credentials, capabilities, permissions, telemetry, and other business services.
  - `services/configuration_readiness.py` — non-secret readiness report (LLM + Alibaba Cloud credential completeness) for runtimes that embed iac-code.
- Orchestration and integration protocols:
  - `pipeline/` — multi-step IaC pipeline engine (`engine/`) and the selling flow (`selling/`: candidate generation, cost estimation, and `ros_deploy` deployment orchestration). Selling-flow steps support per-surface `surface_overrides` in `pipeline.yaml` (prompt file, injected tools, conclusion schema) — for example the `a2a` and `a2a_rich` variants of `confirm_and_select`; keep rich candidate presentation scoped to Skill/A2A surfaces so REPL/Web behavior stays unchanged.
  - `a2a/` — A2A 1.0 server and client with multiple transports (`transports/`: gRPC, stdio, unix socket, Redis streams), plus input-required permission coordination (`input_required.py`) and request-scoped overrides such as the caller's preferred language (`runtime_overrides.py`).
  - `acp/` — ACP server.
  - `mcp/` — MCP client integration and configuration.
- Supporting modules:
  - `i18n/` — translation infrastructure and compiled `locales/`.
  - `state/`, `tasks/`, `types/`, `utils/` — shared app state, background task management, shared type definitions, and common utilities.

## Testing Requirements

- For behavioral changes, prioritize adding or updating pytest cases under `tests/`.
- Tests must not depend on real LLMs, real Alibaba Cloud accounts, real network calls, or local user configuration.
- When testing environment variables and credential reading, use `tmp_path`, `patch.dict`, or mocks to isolate state.
- For small changes, run at least the relevant tests; after changes to shared logic, CLI, providers, credentials, or tool execution paths, run `make test` and `make lint` if necessary.
- Tests must pass across the full Python matrix (3.10–3.14). Keep code cross-platform: CI also runs on Windows, so watch for path-separator assumptions, binary-vs-text file I/O, `expanduser` reading `USERPROFILE` on Windows, and subprocess encoding (decode Node/other subprocess output with `encoding="utf-8"`).

## Desktop Development

- Keep Desktop changes limited to behavior required by the installed application. Reuse the existing Python services, Web UI, sessions, settings, credentials, and pipeline logic; do not duplicate or opportunistically redesign the Web product.
- Desktop packages must be built natively on their target operating system. The sidecar build uses CPython 3.12; release builders also require Node.js 22 and stable Rust.
- The supported release artifacts are macOS Apple Silicon DMG/updater bundles, Windows x64 NSIS setup/updater bundles, and Linux x64 AppImage and deb packages. In-app updates are supported for macOS, Windows, and AppImage; deb users install a newer package through their normal package workflow.
- Treat the OS publisher signature and the Tauri updater signature as separate concerns. Unsigned/ad-hoc-signed installers may trigger platform warnings, while updater payloads must still use the configured persistent updater signing key.
- Never commit signing keys, passwords, certificates, platform credentials, or generated Desktop artifacts such as `desktop/dist/`, Cargo `target/`, DMG, EXE, AppImage, deb, updater archives, checksums, or SBOM output.
- When Desktop behavior changes, run the relevant Python and Rust tests above. Run packaging only on the affected native platform when the change touches bundling, resources, menus, icons, sidecars, installers, or updater behavior.
- A Desktop pull request that changes only shared integration files under `src/` or `tests/` must carry the `desktop` label. The Desktop workflow uses that label to force the scope audit; changes to Desktop-owned paths enforce the audit automatically. Ordinary non-Desktop pull requests remain outside this boundary.

## Configuration and Credentials

- The runtime configuration directory defaults to `~/.iac-code/`, containing `.credentials.yml`, `.cloud-credentials.yml`, `settings.yml`, `.multimodal-cache.yml`, and input history (`.input_history`). Override by setting `IAC_CODE_CONFIG_DIR` (supports `~` and `$VAR` expansion); all subdirectories (`projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/`, `state/`, `tasks/`) follow.
- `IAC_CODE_INSTRUCTION_MEMORY_FILE` selects which project instruction/memory file is loaded (e.g. `make run`/`make dev` set it to `IAC-CODE.md`).
- Dependency installs resolve through the Aliyun PyPI mirror configured under `[tool.uv]` in `pyproject.toml`.
- Do not commit, print, or hard-code real API keys, AccessKeys, Secrets, tokens, cookies, or user configuration file contents.
- Alibaba Cloud credential-related tests must use fake values and avoid triggering real cloud APIs.

## i18n and Bundled Skills

- Translations span **two Babel domains**, and `make translate` extracts/updates/compiles both across the `zh es fr de ja pt` locales:
  - `messages` — Python strings marked with `_()` or `translate_message()`.
  - `webui` — frontend strings extracted from the Web app JS (`babel_webui.cfg`, which excludes `vendor/`).
- Python (`messages`) domain: do not use an f-string with `_()` (e.g. `f"hello {_('world')}"`); Python 3.10's tokenizer treats the entire f-string as a single token so Babel cannot extract nested `_()` calls — use `str.format` instead (e.g. `_('hello {}').format(name)`).
- `_()` resolves against the process-wide locale and serves text displayed on the local machine (REPL, Web UI, local errors). Text that the A2A server returns in the caller's requested language must use `i18n.translate_message(msgid, language=...)`, which resolves the English msgid from the `messages` catalog per request, so concurrent tasks with different `preferredLanguage` values do not fight over the global locale. Do not nest the two, and keep the msgid a plain string literal as the first argument so Babel can extract it (`translate_message` is registered as an extraction keyword in `babel.cfg`).
- Web (`webui`) domain: a new frontend `t()` entry must exist and be non-empty in **all** locales (there is a strict catalog-completeness test). Frontend JS modules are cache-busted with per-module `?v=` tokens — changing a module means bumping its token in `index.html` and keeping `tests/web/test_frontend_static.py` in sync.
- After modifying user-facing translatable strings (Python or frontend), run `make translate`. Compiled `.mo` files are build artifacts and are not committed — but a missing `.mo` makes the UI silently fall back to English, so ensure compilation succeeds locally.
- Public Desktop documentation is maintained in English, Simplified Chinese, Spanish, French, German, Japanese, and Portuguese. A Desktop documentation change must update `README.md`, every matching file in `readme/`, and every matching website document under `website/docs/` and `website/i18n/`. Write natural translations; do not use English copies or placeholder translations. The website locale name for Simplified Chinese is `zh-Hans`.
- Markdown files and scripts under `src/iac_code/skills/bundled/iac_aliyun/` are bundled skill resources; when modifying them, maintain consistency between templates, parameter descriptions, and conversion scripts.
- Do not commit generated translations, build artifacts, or coverage outputs unless the current task explicitly requires it.

## Git Collaboration

- The workspace may contain changes from others; do not revert changes you did not make.
- Do not use `git reset --hard`, force push, or other destructive operations unless the user explicitly requests it.
- Before committing, verify that `git status` only includes files related to the current task.
