from pathlib import Path

from iac_code.skills.bundled import register_bundled_skill

SKILL_DIR = Path(__file__).parent


def register_iac_aliyun_skill() -> None:
    register_bundled_skill(
        name="iac-aliyun",
        description="阿里云 IaC 模板生成、解释、完善与部署",
        prompt=(SKILL_DIR / "SKILL.md").read_text(),
        when_to_use="当用户涉及云资源创建、模板生成、模板解释、部署等 IaC 相关操作时",
        user_invocable=False,
        skill_root=str(SKILL_DIR),
    )
