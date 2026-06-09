import re
from pathlib import Path

from iac_code.skills.bundled import _bundled_skills, get_bundled_skills, init_bundled_skills

POLICY_ROOT = Path("src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policies")
POLICY_PACK_ROOT = POLICY_ROOT / "packs"
POLICY_GENERATION_REFERENCE = Path("src/iac_code/skills/bundled/iac_aliyun/references/infraguard-policy-generation.md")
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
        assert "package infraguard.rules" not in iac_skill.content
        assert "helpers.resources_by_type" not in iac_skill.content
        assert "#### InfraGuard Rego 结构" not in iac_skill.content

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
            assert len(list((POLICY_ROOT / scenario).glob("*.rego"))) >= 12

    def test_infraguard_policy_catalog_readme_matches_assets(self):
        readme = (POLICY_ROOT / "README.md").read_text(encoding="utf-8")
        scenario_counts = {
            scenario: len(list((POLICY_ROOT / scenario).glob("*.rego"))) for scenario in POLICY_SCENARIOS
        }
        baseline_rules = list((POLICY_ROOT / "rules" / "ros").glob("*.rego"))
        packs = list(POLICY_PACK_ROOT.glob("*.rego"))

        assert f"- Total rule policies: {len(_rego_files_with('rule_meta :='))}" in readme
        assert f"- Scenario policy files: {sum(scenario_counts.values())}" in readme
        assert f"- Cloud infrastructure security baseline rules: {len(baseline_rules)}" in readme
        assert f"- Packs: {len(packs)}" in readme

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

    def test_infraguard_policy_catalog_has_scenario_packs(self):
        rule_ids = set()
        for policy in _rule_policy_files():
            match = re.search(r'"id": "([^"]+)"', policy.read_text(encoding="utf-8"))
            assert match is not None
            rule_ids.add(match.group(1))

        packs = sorted(POLICY_PACK_ROOT.glob("iac-code-*-pack.rego"))
        assert len(packs) == len(POLICY_SCENARIOS)
        for pack in packs:
            content = pack.read_text(encoding="utf-8")
            assert "package infraguard.packs.aliyun.iac_code_" in content
            assert "pack_meta :=" in content
            pack_rule_ids = re.findall(r'"([^"]+)"', content.split('"rules":', 1)[1])
            assert len(pack_rule_ids) >= 12
            assert set(pack_rule_ids) <= rule_ids

        baseline_pack = POLICY_PACK_ROOT / "cloud-infrastructure-security-baseline.rego"
        assert baseline_pack.exists()
        baseline_content = baseline_pack.read_text(encoding="utf-8")
        assert "package infraguard.packs.aliyun.cloud_infrastructure_security_baseline" in baseline_content
        assert '"id": "cloud-infrastructure-security-baseline"' in baseline_content

    def test_infraguard_security_no_public_ip_policy_checks_common_exposure_paths(self):
        policy = POLICY_ROOT / "security" / "security-ecs-instance-no-public-ip.rego"
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
