from pathlib import Path

from iac_code.skills.bundled import _bundled_skills, get_bundled_skills, init_bundled_skills

PAC_SKILL_ROOT = Path("src/iac_code/skills/bundled/pac_aliyun")


def _pac_aliyun_asset_text() -> str:
    parts = []
    for path in sorted(PAC_SKILL_ROOT.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".py", ".rego"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestPacAliyunSkill:
    def setup_method(self):
        _bundled_skills.clear()

    def test_pac_aliyun_skill_registered(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        pac_skills = [s for s in skills if s.name == "pac-aliyun"]
        assert len(pac_skills) == 1

    def test_pac_aliyun_skill_not_user_invocable(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        pac_skill = next(s for s in skills if s.name == "pac-aliyun")
        assert pac_skill.is_user_invocable is False

    def test_pac_aliyun_skill_has_auto_trigger_metadata(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        pac_skill = next(s for s in skills if s.name == "pac-aliyun")
        assert pac_skill.auto_trigger == {"script": "auto_trigger.py"}

    def test_pac_aliyun_skill_hosts_infraguard_policy_generation(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        pac_skill = next(s for s in skills if s.name == "pac-aliyun")
        assert "InfraGuard" in pac_skill.content
        assert "references/infraguard-policy-generation.md" in pac_skill.content
        assert "infraguard policy update" in pac_skill.content
        assert "go install github.com/aliyun/infraguard/cmd/infraguard@latest" in pac_skill.content

    def test_pac_aliyun_reference_requires_lazy_update_before_pac_work(self):
        reference = PAC_SKILL_ROOT / "references" / "infraguard-policy-generation.md"
        assert reference.exists()
        content = reference.read_text(encoding="utf-8")
        assert "Lazy InfraGuard Sync" in content
        assert "Run this sync before any PAC implementation, generation, validation, or catalog lookup" in content
        assert "infraguard policy update" in content
        assert "infraguard policy list" in content
        assert "infraguard policy validate" in content

    def test_pac_aliyun_assets_do_not_embed_infraguard_policy_catalog(self):
        assets = _pac_aliyun_asset_text()
        assert "package infraguard.rules" not in assets
        assert "package infraguard.packs" not in assets
        assert "rule_meta :=" not in assets
        assert "pack_meta :=" not in assets
        assert "helpers.resources_by_type" not in assets
        assert not list(PAC_SKILL_ROOT.rglob("*.rego"))
