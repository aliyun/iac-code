from pathlib import Path

from iac_code.skills.bundled import _bundled_skills, get_bundled_skills, init_bundled_skills

IAC_SKILL_ROOT = Path("src/iac_code/skills/bundled/iac_aliyun")


def _iac_aliyun_asset_text() -> str:
    parts = []
    for path in sorted(IAC_SKILL_ROOT.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".py", ".rego"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


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

    def test_iac_skill_delegates_infraguard_work_to_pac_skill(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        iac_skill = next(s for s in skills if s.name == "iac-aliyun")
        assert "pac-aliyun" in iac_skill.content
        assert "references/infraguard-policy-generation.md" not in iac_skill.content
        assert "references/infraguard-policies/" not in iac_skill.content
        assert "package infraguard.rules" not in iac_skill.content
        assert "helpers.resources_by_type" not in iac_skill.content

    def test_iac_aliyun_assets_do_not_embed_infraguard_policy_catalog(self):
        assets = _iac_aliyun_asset_text()
        assert "references/infraguard-policies/" not in assets
        assert "package infraguard.rules" not in assets
        assert "rule_meta :=" not in assets
        assert "deny contains result if" not in assets
        assert "generate_infraguard_policies" not in assets
        assert not (IAC_SKILL_ROOT / "references" / "infraguard-policies").exists()
        assert not (IAC_SKILL_ROOT / "scripts" / "generate_infraguard_policies.py").exists()

    def test_parameter_recommendation_reference_exists(self):
        reference = IAC_SKILL_ROOT / "references" / "template-parameter-recommendation.md"
        assert reference.exists()
        content = reference.read_text(encoding="utf-8")
        assert "GetTemplateParameterConstraints" in content
        assert "PreviewStack" in content
        assert "Preview-Validated Parameter Set" in content
        assert "ParametersOrder" in content
        assert "纯 Terraform" in content
        assert "IaCService" in content
        assert "脱敏后的摘要" in content
