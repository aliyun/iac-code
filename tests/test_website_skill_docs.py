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
        "a2a/skill-integration.md": [
            "ensure-runtime",
            "cache clean",
            "llm_not_configured",
            "cloud_credentials_not_configured",
            "boundaryReached",
            "inputRequired",
            "ask_user_question",
            "candidate_selection",
            "allow_once",
            "skill-package-contract.json",
            "skill-runtime/<runtime-tag>/<target>/",
            ".iac-code-skill-results/",
            "127.0.0.1",
        ],
        "a2a/protocol-reference.md": [
            "metadata.iac_code.preferredLanguage",
            "metadata.iac_code.candidatePresentation",
            "rich-v1",
            "permission_ack",
            "inputReceived",
            "schemaVersion",
        ],
        "a2a/overview.md": [
            "skill-integration.md",
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
    assert "'a2a/skill-integration'" in sidebars, "a2a/skill-integration is not registered in sidebars.ts"
    assert (WEBSITE_ROOT / "docs" / "a2a" / "skill-integration.md").exists(), (
        "English source document for a2a/skill-integration is missing"
    )
