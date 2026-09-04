---
sidebar_position: 2
title: Install and Use the IaC Code Skill
description: Add IaC Code to a Skill-capable agent and use it to manage Alibaba Cloud infrastructure.
---

# Install and Use the IaC Code Skill

The IaC Code Skill lets a compatible agent delegate Alibaba Cloud infrastructure work to IaC Code. You can use it to
plan cloud architectures, generate or review ROS and Terraform templates, estimate costs, select existing resources,
operate ROS stacks, and deploy resources. The package includes its own verified IaC Code Runtime, so you do not need
to install IaC Code separately.

## Download

[Download the latest iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

This fixed URL always points to the latest stable Skill package. Automated installers can read
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
to obtain the current version, immutable download URL, file size, and SHA-256 digest. For reproducible installation,
download `skill.url` from that file and verify `skill.sha256`.

## Install

Before installing, make sure that:

- Your agent supports local Skills defined by `SKILL.md`.
- CPython 3.8–3.14 is available. Use `python3` on macOS or Linux and `py -3` on Windows.
- The environment can access the download URL on first use.

Official Runtime packages support macOS on Apple Silicon, Linux x86_64, and Windows x86_64. The Runtime checks the
operating-system and ABI requirements before it is downloaded.

Extract the ZIP into the Skill directory documented by your agent. The archive already contains the top-level
`iac-code/` directory, so the final layout must be:

```text
<Agent Skill root>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

Common host locations:

- **Codex**: extract to `~/.agents/skills/iac-code/` for all projects, or
  `<repository>/.agents/skills/iac-code/` for one repository. See the
  [Codex Skills documentation](https://developers.openai.com/codex/skills#where-codex-loads-local-skills).
- **Claude Code**: extract to `~/.claude/skills/iac-code/` for all projects, or
  `<repository>/.claude/skills/iac-code/` for one repository. See the
  [Claude Code Skills documentation](https://code.claude.com/docs/en/skills#where-skills-live).

Restart the agent or open a new session after installation. To verify the Runtime in advance, run the following
command from the extracted `iac-code` directory.

macOS or Linux:

```bash
python3 scripts/iac_code.py ensure-runtime
```

Windows PowerShell:

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

On first use, the bridge downloads the Runtime for the current platform and verifies its size and SHA-256 digest.
Later tasks reuse the verified local copy.

## Configure the Model and Alibaba Cloud Identity

The Skill uses the standard IaC Code configuration directory, `~/.iac-code/` by default. If you already configured
IaC Code in the REPL, Web app, or Desktop app, the Skill reuses those settings. You can set `IAC_CODE_CONFIG_DIR` to
select another configuration directory.

For automated environments, inject model settings and Alibaba Cloud credentials through a secret-management
solution. Do not place credentials in `SKILL.md`, prompts, project files, or shell history. Prefer temporary
credentials, RAM roles, or OAuth and grant only the permissions needed by the task.

See [LLM Providers](../configuration/llm-providers.md) and
[Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md) for configuration options and supported
environment variables.

## Choose How to Work

The Skill chooses between two modes according to the request:

- **Normal mode** is the default for resource queries and changes, template work, troubleshooting, and deployment of
  a clear target.
- **Pipeline mode** is used when you explicitly request it or need candidate architectures, cost comparison, plan
  confirmation, and deployment as one guided process.

You normally do not need to select a mode yourself. Describe the outcome you want, and mention Pipeline mode only
when you want the solution-comparison workflow.

## First Use

Open a new session in the host agent and describe an Alibaba Cloud infrastructure task. For example:

```text
Use iac-code to review the ROS template in this project. List security risks and recommended changes without modifying the file.
```

Use `$iac-code` to select the Skill explicitly in Codex, or `/iac-code` in Claude Code. On the first request, the agent verifies the model
and cloud configuration, prepares the Runtime, and starts the task. You do not need to start an A2A server manually.

IaC Code may pause and ask you to:

- approve or deny a tool or deployment operation (`permission`);
- answer a question (`ask_user_question`);
- choose a proposed architecture (`candidate_selection`); or
- review the final solution, price, and deployment parameters, then confirm, adjust, reselect, or cancel
  (`deployment_confirmation`).

Always review the target resources, region, impact, and quoted price before answering. A deployment request does not
pre-approve the later deployment confirmation. After a task finishes, you can continue with a follow-up request in
the same agent session; the Skill keeps the IaC Code conversation context.

IaC Code can return progress and questions in English, Simplified Chinese, Spanish, French, German, Japanese, or
Portuguese according to the conversation language.

## Update and Uninstall

To update manually, download the stable ZIP again and replace the complete `iac-code/` directory. Restart the host
agent or open a new session so it reloads the Skill. Do not replace only the bridge script or edit its Runtime URL and
digest.

To uninstall, remove `iac-code/` from the host agent's Skill directory. Downloaded Runtime packages remain in the IaC
Code configuration directory so other installations and active tasks are not disrupted. If you also want to remove
those packages, first run `cache list`, review the result, and then run `cache clean ... --confirm`.

## Troubleshooting

### Configuration is incomplete

If the model provider or API key is incomplete, the Skill returns `llm_not_configured` before starting a task. Both
Pipeline workflows require Alibaba Cloud credentials and return `cloud_credentials_not_configured` when they are
missing. Normal mode can still perform work that does not call cloud APIs and reports a warning when cloud operations
are unavailable.

### The Runtime cannot start

Run `ensure-runtime` and check the returned error. Confirm the host Python version, operating system, architecture,
network access, and proxy settings. An `incompatible_host` result means the machine does not meet the Runtime
requirements; update or move to a supported host instead of installing an unrelated package or Runtime.

### The task pauses or was interrupted

A pause usually means IaC Code is waiting for a question, permission, candidate selection, or deployment confirmation;
it is not a failure. Answer the request shown by the agent. If the host session is still available after an
interruption, ask it to continue the same task so it can recover the existing job instead of starting over.

### Manage Runtime disk usage

From the installed Skill directory, use:

- `python3 scripts/iac_code.py cache list` to inspect installed Runtime packages;
- `python3 scripts/iac_code.py cache clean --runtime-tag <tag> --confirm` to remove one historical Runtime; or
- `python3 scripts/iac_code.py cache clean --candidates --confirm` to remove candidate packages.

The current Runtime and packages used by a live process are protected from cleanup. On Windows, replace `python3`
with `py -3`.

## Security

- The Runtime listens only on a random `127.0.0.1` port and uses a new Bearer token for each process.
- Task artifacts and result files stay in the selected workspace, under `.iac-code-skill-results/` when applicable.
- Readiness and permission summaries are sanitized and do not include credential values.

## Related Documentation

- [Official IaC Code Skills](./skill-overview.md)
- [IaC Code Skill Host Integration Reference](./skill-host-integration.md)
- [A2A Protocol Overview](./overview.md)
- [LLM Providers](../configuration/llm-providers.md)
- [Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md)
- [Runtime Configuration](../configuration/runtime-configuration.md)
