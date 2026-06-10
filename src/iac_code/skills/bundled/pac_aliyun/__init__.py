from pathlib import Path

from iac_code.skills.bundled import register_bundled_skill

SKILL_DIR = Path(__file__).parent


def register_pac_aliyun_skill() -> None:
    register_bundled_skill(
        name="pac-aliyun",
        description="阿里云 Alibaba Cloud Policy as Code / InfraGuard 合规策略生成、校验与策略库查询",
        prompt=(SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"),
        when_to_use=(
            "当用户请求阿里云/Alibaba Cloud/Alicloud 的 Policy as Code、PAC、InfraGuard、Rego "
            "合规策略生成、策略查询、策略更新或模板合规校验时，必须先调用 skill 工具加载 pac-aliyun。"
        ),
        user_invocable=False,
        skill_root=str(SKILL_DIR),
        auto_trigger={"script": "auto_trigger.py", "supersedes": "iac-aliyun"},
    )
