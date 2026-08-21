---
sidebar_position: 7
title: Install and Use the IaC Code Skill
description: Download and install the IaC Code Skill so an external agent can manage Alibaba Cloud infrastructure.
---

# Install and Use the IaC Code Skill

The IaC Code Skill is designed for external agents that support Skills. Once installed, a host agent can delegate
cloud architecture planning, ROS or Terraform template generation and review, cost estimation, resource selection,
stack operations, and deployment to IaC Code. The Skill uses a Python standard-library bridge to start a locally
authenticated A2A Runtime. You do not need to install IaC Code with pip, and the host must not fall back to headless
commands.

## Download the Skill

### Latest stable release

Download the latest stable release directly:

[Download iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

This fixed URL always points to the Skill package promoted to the stable channel. It is suitable for browser downloads
and manual installation, and it does not change when a new version is released.

Installers that need the version, file size, SHA-256 digest, and immutable version URL can read the stable channel
metadata:

[View latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

The document contains:

- `skillVersion`: the current stable Skill version.
- `skill.url`: the immutable ZIP URL for that version.
- `skill.sha256` and `skill.size`: values used to verify the download.
- `manifest.url`: the immutable release manifest for that version.

For strict verification or reproducible automated installation, read `latest.json`, download `skill.url`, and verify
`skill.sha256`. Do not construct a version URL yourself.

## Install the Skill

### Prerequisites

- The host agent supports local Skills defined by `SKILL.md`.
- CPython 3.8–3.14 is installed. Use `python3` on macOS/Linux and prefer `py -3` on Windows.
- The environment can access the OSS URLs above to download the Skill ZIP and the Runtime required on first use.
- Model service configuration is available. A least-privilege Alibaba Cloud identity is also required for tasks that
  query or manage cloud resources.

Official Skill Runtime releases support these platforms:

| Operating system | Architecture |
|---|---|
| macOS | Apple Silicon (arm64) |
| Linux | x86_64 |
| Windows | x86_64 |

The minimum operating-system and Linux glibc versions are defined by the Runtime manifest pinned by the Skill. The
bridge checks compatibility before downloading. On an unsupported platform, it returns an error instead of
downloading an artifact for another platform or ABI.

### Extract into the host agent's Skill directory

Extract the ZIP directly into the host agent's Skill root. The exact Skill root varies by product; follow the host
product's documentation. The final layout must be:

```text
<Agent Skill root>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

The ZIP already contains the top-level `iac-code/` directory. Do not add another directory with the same name. After
installing or updating, restart the host agent or open a new session so that it discovers the Skill again.

### Verify the installation

In the extracted `iac-code` directory, run this command on macOS or Linux:

```bash
python3 scripts/iac_code.py ensure-runtime
```

In Windows PowerShell, run:

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

On first use, the command downloads the Runtime for the current platform, verifies its size and SHA-256 digest, and
prints JSON containing `skillVersion`, `runtimeTag`, and the installation path. A verified cached Runtime is reused
without another download.

## Configure the Model and Alibaba Cloud Identity

The Skill Runtime uses the same configuration directory as other IaC Code modes: `~/.iac-code/` by default. If you
already configured IaC Code through the REPL, Web app, or Desktop app, the Skill can reuse those settings. Set
`IAC_CODE_CONFIG_DIR` to use a different configuration directory.

In automated environments, provide these variables through a secret-management solution:

| Category | Environment variable | Description |
|---|---|---|
| Model | `IAC_CODE_PROVIDER` | Model provider |
| Model | `IAC_CODE_MODEL` | Model name |
| Model | `IAC_CODE_API_KEY` | Model service API key |
| Model | `IAC_CODE_BASE_URL` | Optional compatible endpoint override |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey secret |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | Security token for STS credentials |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | Default region |

Never put real credentials in `SKILL.md`, host-agent prompts, project files, or shell history. Prefer temporary
credentials, RAM roles, or OAuth, and grant only the cloud API permissions required by the task. See
[LLM Providers](../configuration/llm-providers.md) and
[Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md) for complete instructions.

## First Use

After installation and configuration, open a new session in the host agent and describe an Alibaba Cloud
infrastructure task directly. For example:

```text
Use iac-code to review the ROS template in this project. List security risks and recommended changes without modifying the file.
```

Hosts that support explicit Skill syntax can use `$iac-code` to select the Skill. The host reads `SKILL.md`, writes the
complete request to a UTF-8 file inside the workspace, and uses the bridge to create and follow one task. The user does
not need to start an A2A Server manually.

Expected flow:

1. The bridge checks whether model and Alibaba Cloud configuration is ready.
2. On first use, it downloads and verifies the IaC Code Runtime pinned by the Skill.
3. The Runtime listens only on a random `127.0.0.1` port and generates a process-specific Bearer token.
4. The host agent presents progress, questions, candidate plans, and permission requests returned by IaC Code.
5. When the task completes, the host agent returns the final result and files generated in the workspace.

## Update and Uninstall

For a manual update, download `skill/stable/iac-code-skill.zip` again and replace the complete `iac-code/` directory in
the host's Skill root. An automatic updater can compare `skillVersion` from `latest.json`, then download and verify the
new package using its immutable URL and SHA-256 digest. Each official Skill is pinned to a verified Runtime. Do not
replace only `scripts/iac_code.py` or edit its Runtime URL or digest manually.

To uninstall, remove `iac-code/` from the host agent's Skill root. The Runtime cache is not removed with the Skill
directory. Run `cache list` and `cache clean` only when the user explicitly asks to remove it.

## Runtime Cache

The Runtime downloaded on first use is cached under
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` and reused automatically. Normal use does
not require managing this directory. To inspect disk usage or remove historical versions, use:

- `python3 scripts/iac_code.py cache list` — list installed Runtimes and candidate packages.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — remove Runtime caches or
  candidate packages; `--confirm` is required.

The current Runtime and any Runtime used by a live process are protected from cleanup. The package format and Runtime
constraints are defined by `skill-runtime/skill-package-contract.json` in the source repository; users do not need to
modify this file.

## Troubleshooting

### Configuration is incomplete

The Skill checks configuration before creating a task but never reads or returns secret values:

| Situation | Result |
|---|---|
| LLM provider or API key is incomplete | Returns `llm_not_configured` and does not create the task |
| Alibaba Cloud credentials are incomplete for the selling Pipeline | Returns `cloud_credentials_not_configured` and does not create the task |
| Alibaba Cloud credentials are incomplete in normal mode | Tasks that do not call cloud APIs may continue with a preflight warning |

### Why execution pauses

IaC Code pauses when it needs permission, additional information, or a plan selection. The host agent presents the
request directly:

- A tool or deployment permission request (`permission`).
- A multiple-choice question or request for more information (`ask_user_question`).
- A Pipeline candidate plan selection (`candidate_selection`).

Before confirming, review the target resource, region, expected impact, and price. The host agent cannot override a
denial from IaC Code. A one-time approval is represented as `allow_once` in the protocol.

> **Host agent integration note**
>
> When a bridge result contains `inputRequired`, the host agent must present the current request and wait for a
> response. `boundaryReached` marks a presentation or interaction boundary, not task completion; the host must show
> the update and continue following the same task.

## Security

- The Runtime listens only on a random `127.0.0.1` port. Every start generates a new Bearer token, and every bridge
  request carries that token.
- The bridge keeps artifacts and results in the job workspace. Results are written to `.iac-code-skill-results/`.
- Preflight and permission display fields are sanitized; secrets and credentials do not appear in display fields.

## Related Documentation

- [A2A Protocol Overview](./overview.md)
- [A2A Protocol Reference](./protocol-reference.md)
- [LLM Providers](../configuration/llm-providers.md)
- [Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md)
- [Runtime Configuration](../configuration/runtime-configuration.md)
