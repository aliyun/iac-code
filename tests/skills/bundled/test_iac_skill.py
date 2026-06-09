import importlib.util
import re
from pathlib import Path
from types import ModuleType

from iac_code.skills.bundled import _bundled_skills, get_bundled_skills, init_bundled_skills

POLICY_ROOT = Path("src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policies")
POLICY_PACK_ROOT = POLICY_ROOT / "packs"
POLICY_GENERATION_REFERENCE = Path("src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policy-generation.md")
POLICY_GENERATOR_SCRIPT = Path("src/iac_code/skills/bundled/iac_aliyun/scripts/generate_infraguard_policies.py")
POLICY_SCENARIOS = {
    "security",
    "high-availability",
    "cost-optimization",
    "compliance",
    "best-practice",
    "operations",
    "network-architecture",
    "elasticity",
}


def _rule_policy_files() -> list[Path]:
    return sorted(path for scenario in POLICY_SCENARIOS for path in (POLICY_ROOT / scenario).glob("*.rego"))


def _rego_files_with(symbol: str) -> list[Path]:
    return sorted(path for path in POLICY_ROOT.rglob("*.rego") if symbol in path.read_text(encoding="utf-8"))


def _rule_ids_from_policy_files(paths: list[Path]) -> set[str]:
    rule_ids: set[str] = set()
    for path in paths:
        match = re.search(r'"id": "([^"]+)"', path.read_text(encoding="utf-8"))
        assert match is not None
        rule_ids.add(match.group(1))
    return rule_ids


def _pack_rule_ids(pack: Path) -> set[str]:
    content = pack.read_text(encoding="utf-8")
    return set(re.findall(r'"([^"]+)"', content.split('"rules":', 1)[1]))


def _iac_aliyun_asset_text() -> str:
    root = Path("src/iac_code/skills/bundled/iac_aliyun")
    parts = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".py", ".rego"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _policy_translation_block(content: str, field: str) -> dict[str, str]:
    match = re.search(rf'"{field}":\s*\{{(?P<body>[^{{}}]*)\}}', content, re.DOTALL)
    assert match is not None
    return dict(re.findall(r'"([a-z]{2})": "([^"]+)"', match.group("body")))


def _load_policy_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_infraguard_policies", POLICY_GENERATOR_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIacSkill:
    def setup_method(self):
        _bundled_skills.clear()

    def test_iac_skill_registered(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skills = [s for s in skills if s.name == "iac-aliyun"]
        assert len(iac_skills) == 1

    def test_iac_skill_not_user_invocable(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert iac_skill.is_user_invocable is False

    def test_iac_skill_has_description(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert len(iac_skill.description) > 0

    def test_iac_skill_has_auto_trigger_metadata(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert iac_skill.auto_trigger == {"script": "auto_trigger.py"}

    def test_iac_skill_mentions_parameter_recommendation_reference(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert "references/template-parameter-recommendation.md" in iac_skill.content
        assert "已有模板参数推荐" in iac_skill.content

    def test_iac_skill_mentions_infraguard_policy_generation(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert "InfraGuard" in iac_skill.content
        assert "references/infraguard-policy-generation.md" in iac_skill.content
        assert "cloud-infrastructure-security-baseline" not in iac_skill.content
        assert "references/infraguard-policies/rules/ros/" not in iac_skill.content
        assert "package infraguard.rules" not in iac_skill.content
        assert "helpers.resources_by_type" not in iac_skill.content
        assert "#### InfraGuard Rego 结构" not in iac_skill.content

    def test_iac_aliyun_assets_do_not_reference_removed_baseline_layout(self):
        assets = _iac_aliyun_asset_text()
        assert "cloud-infrastructure-security-baseline" not in assets
        assert "references/infraguard-policies/rules/ros/" not in assets
        assert "infraguard-policies/rules" not in assets
        assert "cloud infrastructure security baseline" not in assets.lower()
        assert "Cloud Infrastructure Security Baseline" not in assets
        assert "云基础设施安全基线" not in assets

    def test_iac_skill_mentions_infraguard_policy_dimensions_and_generated_catalog(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert "100+ 公共 InfraGuard 策略" not in iac_skill.content
        reference = POLICY_GENERATION_REFERENCE.read_text(encoding="utf-8")
        assert "生成 100+ 个 InfraGuard 策略" in reference
        assert "覆盖 8 个场景" in reference
        for dimension in ["安全性", "高可用", "成本优化", "合规性", "最佳实践", "可运维性", "网络架构", "弹性能力"]:
            assert dimension in reference

    def test_infraguard_policy_generation_reference_exists(self):
        assert POLICY_GENERATION_REFERENCE.exists()
        reference = POLICY_GENERATION_REFERENCE.read_text(encoding="utf-8")
        assert "references/infraguard-policies/" in reference
        assert "infraguard policy validate" in reference
        assert "deny contains result if" in reference
        assert "rule_meta" in reference

    def test_infraguard_policy_catalog_contains_100_policies_across_8_scenarios(self):
        scenario_dirs = {path.name for path in POLICY_ROOT.iterdir() if path.is_dir()}
        assert POLICY_SCENARIOS <= scenario_dirs

        policies = _rule_policy_files()
        assert len(policies) >= 100

        for scenario in POLICY_SCENARIOS:
            assert list((POLICY_ROOT / scenario).glob("*.rego"))

    def test_infraguard_policy_catalog_readme_matches_assets(self):
        readme = (POLICY_ROOT / "README.md").read_text(encoding="utf-8")
        scenario_counts = {
            scenario: len(list((POLICY_ROOT / scenario).glob("*.rego"))) for scenario in POLICY_SCENARIOS
        }
        packs = list(POLICY_PACK_ROOT.glob("*.rego"))

        assert f"- Total rule policies: {len(_rego_files_with('rule_meta :='))}" in readme
        assert f"- Scenario policy files: {sum(scenario_counts.values())}" in readme
        assert f"- Packs: {len(packs)}" in readme
        assert "cloud infrastructure security baseline" not in readme.lower()
        assert "云基础设施安全基线" not in readme

        for scenario, count in scenario_counts.items():
            assert f"`{scenario}`" in readme
            assert f"({count} rules)" in readme

    def test_infraguard_policy_catalog_entries_are_structured_rego(self):
        policies = _rule_policy_files()
        assert policies

        rule_ids: set[str] = set()
        for policy in policies:
            content = policy.read_text(encoding="utf-8")
            assert "package infraguard.rules.aliyun." in content
            assert "import rego.v1" in content
            assert "rule_meta :=" in content
            assert "deny contains result if" in content
            assert '"resource_types":' in content
            assert '"dimension":' not in content

            match = re.search(r'"id": "([^"]+)"', content)
            assert match is not None
            rule_id = match.group(1)
            assert rule_id not in rule_ids
            rule_ids.add(rule_id)

    def test_infraguard_policy_metadata_does_not_copy_zh_into_other_locales(self):
        copied_translations = []
        metadata_fields = ("name", "description", "reason", "recommendation")
        non_zh_languages = ("ja", "de", "es", "fr", "pt")

        for policy in _rule_policy_files():
            content = policy.read_text(encoding="utf-8")
            for field in metadata_fields:
                translations = _policy_translation_block(content, field)
                zh_value = translations.get("zh")
                if not zh_value:
                    continue

                for language in non_zh_languages:
                    if translations.get(language) == zh_value:
                        copied_translations.append(f"{policy.relative_to(POLICY_ROOT)}:{field}:{language}")

        assert not copied_translations, "Policy metadata locales copied from zh:\n" + "\n".join(copied_translations)

    def test_infraguard_policy_metadata_non_cjk_locales_do_not_contain_chinese_text(self):
        chinese_text_translations = []
        metadata_fields = ("name", "description", "reason", "recommendation")
        non_cjk_languages = ("de", "es", "fr", "pt")
        han_character = re.compile(r"[\u4e00-\u9fff]")

        for policy in _rule_policy_files():
            content = policy.read_text(encoding="utf-8")
            for field in metadata_fields:
                translations = _policy_translation_block(content, field)
                for language in non_cjk_languages:
                    value = translations.get(language, "")
                    if han_character.search(value):
                        chinese_text_translations.append(f"{policy.relative_to(POLICY_ROOT)}:{field}:{language}")

        assert not chinese_text_translations, "Policy metadata non-CJK locales contain Chinese text:\n" + "\n".join(
            chinese_text_translations
        )

    def test_ecs_instance_name_required_has_localized_policy_metadata(self):
        policy = POLICY_ROOT / "best-practice" / "ecs-instance-name-required.rego"
        content = policy.read_text(encoding="utf-8")

        expected = {
            "name": {
                "en": "ECS instance must configure name",
                "zh": "ECS 实例必须配置名称",
                "ja": "ECS インスタンスには名前を設定する必要があります",
                "de": "Für ECS-Instanzen muss ein Name konfiguriert sein",
                "es": "Las instancias ECS deben tener un nombre configurado",
                "fr": "Les instances ECS doivent avoir un nom configuré",
                "pt": "As instâncias ECS devem ter um nome configurado",
            },
            "description": {
                "en": "Checks ECS instance must configure name",
                "zh": "检查ECS 实例必须配置名称",
                "ja": "ECS インスタンスに名前が設定されていることを確認します",
                "de": "Prüft, ob für ECS-Instanzen ein Name konfiguriert ist",
                "es": "Comprueba que las instancias ECS tengan un nombre configurado",
                "fr": "Vérifie que les instances ECS ont un nom configuré",
                "pt": "Verifica se as instâncias ECS têm um nome configurado",
            },
            "reason": {
                "en": "ECS instance must configure name is not satisfied.",
                "zh": "ECS 实例必须配置名称未满足。",
                "ja": "ECS インスタンス名の設定要件を満たしていません。",
                "de": "Für die ECS-Instanz ist kein Name konfiguriert.",
                "es": "La instancia ECS no tiene un nombre configurado.",
                "fr": "L'instance ECS n'a pas de nom configuré.",
                "pt": "A instância ECS não tem um nome configurado.",
            },
            "recommendation": {
                "en": "Configure InstanceName on ALIYUN::ECS::Instance to satisfy the policy.",
                "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
                "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に InstanceName を設定してください。",
                "de": "Konfigurieren Sie InstanceName für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
                "es": "Configure InstanceName en ALIYUN::ECS::Instance para cumplir la política.",
                "fr": "Configurez InstanceName sur ALIYUN::ECS::Instance pour satisfaire la politique.",
                "pt": "Configure InstanceName em ALIYUN::ECS::Instance para atender à política.",
            },
        }

        for field, translations in expected.items():
            assert _policy_translation_block(content, field) == translations

    def test_infraguard_policy_generator_renders_localized_metadata(self):
        generator = _load_policy_generator()
        policy = generator.SCENARIOS["best-practice"]["rules"][1]
        content = generator.render_policy("best-practice", "最佳实践", policy)

        expected_languages = {"en", "zh", "ja", "de", "es", "fr", "pt"}
        for field in ("name", "description", "reason", "recommendation"):
            translations = _policy_translation_block(content, field)
            assert set(translations) == expected_languages
            assert translations["ja"] != translations["zh"]
            for language in ("de", "es", "fr", "pt"):
                assert translations[language] != translations["zh"]
                assert not re.search(r"[\u4e00-\u9fff]", translations[language])

    def test_infraguard_policy_catalog_has_scenario_packs(self):
        rule_ids = set()
        for policy in _rule_policy_files():
            match = re.search(r'"id": "([^"]+)"', policy.read_text(encoding="utf-8"))
            assert match is not None
            rule_ids.add(match.group(1))

        packs = sorted(POLICY_PACK_ROOT.glob("iac-code-*-pack.rego"))
        assert len(packs) == len(POLICY_SCENARIOS)
        for pack in packs:
            scenario = pack.stem.removeprefix("iac-code-").removesuffix("-pack")
            scenario_rule_ids = _rule_ids_from_policy_files(list((POLICY_ROOT / scenario).glob("*.rego")))
            content = pack.read_text(encoding="utf-8")
            assert "package infraguard.packs.aliyun.iac_code_" in content
            assert "pack_meta :=" in content
            pack_rule_ids = re.findall(r'"([^"]+)"', content.split('"rules":', 1)[1])
            assert pack_rule_ids
            assert set(pack_rule_ids) <= rule_ids
            assert set(pack_rule_ids) == scenario_rule_ids

        assert not (POLICY_PACK_ROOT / "cloud-infrastructure-security-baseline.rego").exists()

    def test_infraguard_security_pack_owns_merged_security_rules(self):
        assert not (POLICY_ROOT / "rules").exists()

        security_rule_ids = _rule_ids_from_policy_files(list((POLICY_ROOT / "security").glob("*.rego")))
        security_pack_rule_ids = _pack_rule_ids(POLICY_PACK_ROOT / "iac-code-security-pack.rego")

        assert security_pack_rule_ids == security_rule_ids
        assert {
            "security-api-gateway-api-auth-required",
            "security-ecs-instance-no-public-ip",
            "security-oss-bucket-private-acl",
            "security-oss-bucket-encryption-configured",
            "security-oss-bucket-logging-configured",
            "security-ram-user-mfa-required",
            "security-rds-instance-ssl-required",
            "security-rds-instance-tde-enabled",
        }.isdisjoint(security_rule_ids)

    def test_infraguard_security_no_public_ip_policy_checks_common_exposure_paths(self):
        policy = POLICY_ROOT / "security" / "ecs-running-instance-no-public-ip.rego"
        content = policy.read_text(encoding="utf-8")
        assert "InternetMaxBandwidthOut" in content
        assert "ALIYUN::VPC::EIPAssociation" in content
        assert "helpers.is_referencing" in content
        assert "helpers.is_get_att_referencing" in content

    def test_infraguard_rego_files_are_packaged(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        assert '"**/*.rego"' in pyproject

    def test_parameter_recommendation_reference_exists(self):
        reference = Path("src/iac_code/skills/bundled/iac_aliyun/references/template-parameter-recommendation.md")
        assert reference.exists()
        content = reference.read_text(encoding="utf-8")
        assert "GetTemplateParameterConstraints" in content
        assert "PreviewStack" in content
        assert "Preview-Validated Parameter Set" in content
        assert "ParametersOrder" in content
        assert "纯 Terraform" in content
        assert "IaCService" in content
        assert "脱敏后的摘要" in content
