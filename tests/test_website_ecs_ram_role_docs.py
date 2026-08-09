from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
WEBSITE_ROOT = PROJECT_ROOT / "website"

LOCALE_DOC_ROOTS = {
    "en": WEBSITE_ROOT / "docs",
    "zh-Hans": WEBSITE_ROOT / "i18n" / "zh-Hans" / "docusaurus-plugin-content-docs" / "current",
    "ja": WEBSITE_ROOT / "i18n" / "ja" / "docusaurus-plugin-content-docs" / "current",
    "fr": WEBSITE_ROOT / "i18n" / "fr" / "docusaurus-plugin-content-docs" / "current",
    "de": WEBSITE_ROOT / "i18n" / "de" / "docusaurus-plugin-content-docs" / "current",
    "es": WEBSITE_ROOT / "i18n" / "es" / "docusaurus-plugin-content-docs" / "current",
    "pt": WEBSITE_ROOT / "i18n" / "pt" / "docusaurus-plugin-content-docs" / "current",
}

ROLE_NAME_ENVIRONMENT_NOTE = {
    "en": "does not select the mode by itself",
    "zh-Hans": "不会自行选择认证模式",
    "ja": "この変数だけではモードは選択されません",
    "fr": "ne sélectionne pas le mode à lui seul",
    "de": "wählt den Modus aber nicht selbst aus",
    "es": "no selecciona el modo por sí solo",
    "pt": "não seleciona o modo por si só",
}

AUTO_DISCOVERY_NOTE = {
    "en": "auto-discovery",
    "zh-Hans": "自动发现",
    "ja": "自動検出",
    "fr": "détection automatique",
    "de": "automatische Erkennung",
    "es": "detección automática",
    "pt": "detecção automática",
}

NO_STATIC_CREDENTIAL_NOTE = {
    "en": "does not store an AccessKey ID",
    "zh-Hans": "不会保存 AccessKey ID",
    "ja": "AccessKey ID、AccessKey Secret、STS トークンを設定ファイルに保存しません",
    "fr": "n'enregistre aucun AccessKey ID",
    "de": "speichert weder AccessKey-ID",
    "es": "no guarda un AccessKey ID",
    "pt": "não salva AccessKey ID",
}

ECS_RUNTIME_BOUNDARY_NOTE = {
    "en": "only where ECS IMDS is reachable",
    "zh-Hans": "只有运行环境能够访问 ECS IMDS",
    "ja": "成功するには ECS IMDS にアクセスでき",
    "fr": "n'aboutissent que si IMDS d'ECS est accessible",
    "de": "nur erfolgreich, wenn ECS IMDS erreichbar ist",
    "es": "solo funcionan cuando IMDS de ECS está accesible",
    "pt": "só funcionam quando o ECS IMDS está acessível",
}

ECS_ENVIRONMENT_VARIABLES = {
    "ALIBABA_CLOUD_ECS_METADATA",
    "ALIBABA_CLOUD_ECS_METADATA_DISABLED",
    "ALIBABA_CLOUD_IMDSV1_DISABLED",
}


def test_website_documents_ecs_ram_role_in_all_locales() -> None:
    errors: list[str] = []
    common_tokens = {
        "EcsRamRole",
        "ram_role_name",
        "IMDS",
        "/auth",
        "REPL",
        "Web",
        "Desktop",
        ".cloud-credentials.yml",
        "~/.aliyun/config.json",
        *ECS_ENVIRONMENT_VARIABLES,
    }

    for locale, root in LOCALE_DOC_ROOTS.items():
        path = root / "configuration" / "alibaba-cloud-credentials.md"
        if not path.exists():
            errors.append(f"{path.relative_to(PROJECT_ROOT)}: missing file")
            continue
        text = path.read_text(encoding="utf-8")
        required = {
            *common_tokens,
            ROLE_NAME_ENVIRONMENT_NOTE[locale],
            AUTO_DISCOVERY_NOTE[locale],
            NO_STATIC_CREDENTIAL_NOTE[locale],
            ECS_RUNTIME_BOUNDARY_NOTE[locale],
        }
        for token in sorted(required):
            if token not in text:
                errors.append(f"{path.relative_to(PROJECT_ROOT)}: missing {token!r}")

    assert not errors, "\n".join(errors)


def test_website_documents_ecs_metadata_environment_variables_in_all_locales() -> None:
    errors: list[str] = []

    for locale, root in LOCALE_DOC_ROOTS.items():
        path = root / "configuration" / "environment-variables.md"
        if not path.exists():
            errors.append(f"{path.relative_to(PROJECT_ROOT)}: missing file")
            continue
        text = path.read_text(encoding="utf-8")
        required = {
            "EcsRamRole",
            "IMDSv1",
            "IMDSv2",
            ROLE_NAME_ENVIRONMENT_NOTE[locale],
            *ECS_ENVIRONMENT_VARIABLES,
        }
        for token in sorted(required):
            if token not in text:
                errors.append(f"{path.relative_to(PROJECT_ROOT)}: missing {token!r}")

    assert not errors, "\n".join(errors)
