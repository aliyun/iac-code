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
        assert pac_skill.auto_trigger == {"script": "auto_trigger.py", "supersedes": "iac-aliyun"}

    def test_pac_aliyun_skill_hosts_infraguard_policy_generation(self):
        init_bundled_skills()
        skills = get_bundled_skills()
        pac_skill = next(s for s in skills if s.name == "pac-aliyun")
        assert "InfraGuard" in pac_skill.content
        assert "references/infraguard-policy-generation.md" in pac_skill.content
        assert "infraguard policy update" in pac_skill.content
        assert "infraguard update --check" in pac_skill.content
        assert (
            "GOPROXY=https://mirrors.aliyun.com/goproxy/,direct "
            "go install github.com/aliyun/infraguard/cmd/infraguard@latest"
        ) in pac_skill.content
        assert "infraguard update" in pac_skill.content

    def test_pac_aliyun_reference_requires_lazy_update_before_pac_work(self):
        reference = PAC_SKILL_ROOT / "references" / "infraguard-policy-generation.md"
        assert reference.exists()
        content = reference.read_text(encoding="utf-8")
        assert "Lazy InfraGuard Sync" in content
        assert "Run this sync before any PAC implementation, generation, validation, or catalog lookup" in content
        assert "infraguard update --check" in content
        assert "infraguard policy update" in content
        assert "infraguard policy list" in content
        assert "infraguard policy validate" in content
        assert "0.10.1" in content

    def test_pac_aliyun_reference_installs_through_aliyun_goproxy(self):
        reference = PAC_SKILL_ROOT / "references" / "infraguard-policy-generation.md"
        content = reference.read_text(encoding="utf-8")
        install_command = (
            "GOPROXY=https://mirrors.aliyun.com/goproxy/,direct "
            "go install github.com/aliyun/infraguard/cmd/infraguard@latest"
        )
        assert install_command in content
        assert "\ngo install github.com/aliyun/infraguard/cmd/infraguard@latest" not in content

    def test_pac_aliyun_reference_documents_cli_upgrade_strategy(self):
        reference = PAC_SKILL_ROOT / "references" / "infraguard-policy-generation.md"
        content = reference.read_text(encoding="utf-8")
        assert "If the local version is not latest" in content
        assert "lower than `0.10.1`" in content
        assert "reinstalling with the Alibaba Cloud Go proxy command" in content
        assert "version is `0.10.1` or newer" in content
        assert "infraguard update" in content

    def test_pac_aliyun_reference_documents_supported_scan_packs(self):
        reference = PAC_SKILL_ROOT / "references" / "infraguard-policy-generation.md"
        content = reference.read_text(encoding="utf-8")
        expected_packs = {
            "最佳实践": "pack:aliyun:best-practice",
            "合规性": "pack:aliyun:compliance",
            "成本优化": "pack:aliyun:cost-optimization",
            "弹性能力": "pack:aliyun:elasticity",
            "高可用": "pack:aliyun:high-availability",
            "网络架构": "pack:aliyun:network-architecture",
            "可运维性": "pack:aliyun:operations",
            "安全性": "pack:aliyun:security",
        }
        assert "Supported Scan Dimensions" in content
        for dimension, pack in expected_packs.items():
            assert dimension in content
            assert pack in content
        assert "one `-p` flag for each matching pack" in content
        assert "quick-start-compliance-pack" not in content

    def test_pac_aliyun_assets_do_not_embed_infraguard_policy_catalog(self):
        assets = _pac_aliyun_asset_text()
        assert "package infraguard.rules" not in assets
        assert "package infraguard.packs" not in assets
        assert "rule_meta :=" not in assets
        assert "pack_meta :=" not in assets
        assert "helpers.resources_by_type" not in assets
        assert not list(PAC_SKILL_ROOT.rglob("*.rego"))
