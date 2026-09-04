from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
WEBSITE_ROOT = PROJECT_ROOT / "website"

LOCALE_DOC_ROOTS = [
    WEBSITE_ROOT / "docs",
    WEBSITE_ROOT / "i18n" / "zh-Hans" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "ja" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "fr" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "de" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "es" / "docusaurus-plugin-content-docs" / "current",
    WEBSITE_ROOT / "i18n" / "pt" / "docusaurus-plugin-content-docs" / "current",
]


def test_website_documents_external_skill_integration_in_all_locales() -> None:
    missing: list[str] = []
    checks = {
        "a2a/skill-overview.md": [
            "iac-code-skill.zip",
            "alibabacloud-iac-code",
            "alibabacloud-ros-agent",
            "npx skills add",
            "https://skills.aliyun.com/",
            "/api/public/skills/alibabacloud-iac-code/download",
            "/api/public/skills/alibabacloud-ros-agent/download",
            "ros:StartChat",
            "ros:StopChat",
            "~/.iac-code/",
            "skill-integration.md",
            "skill-host-integration.md",
        ],
        "a2a/skill-integration.md": [
            "ensure-runtime",
            "cache clean",
            "llm_not_configured",
            "cloud_credentials_not_configured",
            "ask_user_question",
            "candidate_selection",
            "deployment_confirmation",
            "incompatible_host",
            "Pipeline",
            "iac-code-skill.zip",
            "~/.agents/skills/iac-code/",
            "~/.claude/skills/iac-code/",
            "/iac-code",
            ".iac-code-skill-results/",
            "127.0.0.1",
            "skill-host-integration.md",
        ],
        "a2a/skill-host-integration.md": [
            "config.json",
            "preferredLanguage",
            "boundaryReached",
            "presentationRequired",
            "inputRequired",
            "turn_completed",
            "ask_user_question",
            "candidate_selection",
            "deployment_confirmation",
            "allow_once",
            "continue --job-id",
            "poll --job-id",
            "incompatible_host",
            "skill-package-contract.json",
            "skill-runtime/<runtime-tag>/<target>/",
            "127.0.0.1",
        ],
        "a2a/protocol-reference.md": [
            "metadata.iac_code.channel",
            "metadata.iac_code.preferredLanguage",
            "metadata.iac_code.candidatePresentation",
            "rich-v1",
            "permission_ack",
            "inputReceived",
            "schemaVersion",
        ],
        "a2a/overview.md": [
            "skill-overview.md",
            "skill-integration.md",
            "skill-host-integration.md",
            "preferredLanguage",
            "allow_once",
        ],
        "automation/pipeline-mode.md": [
            "rich-v1",
            "preferredLanguage",
            "allow_once",
        ],
    }

    for root in LOCALE_DOC_ROOTS:
        for relative_path, needles in checks.items():
            path = root / relative_path
            if not path.exists():
                missing.append(f"{path.relative_to(PROJECT_ROOT)}: missing file")
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle not in text:
                    missing.append(f"{path.relative_to(PROJECT_ROOT)}: missing {needle!r}")

    assert not missing, "\n".join(missing)


def test_skill_integration_registered_in_sidebar() -> None:
    sidebars = (WEBSITE_ROOT / "sidebars.ts").read_text(encoding="utf-8")
    assert "label: 'IaC Code Skill'" in sidebars, "IaC Code Skill category is not registered in sidebars.ts"
    assert "'a2a/skill-overview'" in sidebars, "a2a/skill-overview is not registered in sidebars.ts"
    assert "'a2a/skill-integration'" in sidebars, "a2a/skill-integration is not registered in sidebars.ts"
    assert "'a2a/skill-host-integration'" in sidebars, "a2a/skill-host-integration is not registered in sidebars.ts"
    assert (WEBSITE_ROOT / "docs" / "a2a" / "skill-overview.md").exists(), (
        "English source document for a2a/skill-overview is missing"
    )
    assert (WEBSITE_ROOT / "docs" / "a2a" / "skill-integration.md").exists(), (
        "English source document for a2a/skill-integration is missing"
    )
    assert (WEBSITE_ROOT / "docs" / "a2a" / "skill-host-integration.md").exists(), (
        "English source document for a2a/skill-host-integration is missing"
    )
