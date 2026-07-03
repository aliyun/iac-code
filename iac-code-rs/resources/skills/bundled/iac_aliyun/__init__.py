from pathlib import Path

from iac_code.skills.bundled import register_bundled_skill

SKILL_DIR = Path(__file__).parent


def register_iac_aliyun_skill() -> None:
    register_bundled_skill(
        name="iac-aliyun",
        description="阿里云 Alibaba Cloud ROS/Terraform IaC 模板生成、解释、完善、校验、询价与部署",
        prompt=(SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"),
        when_to_use=(
            "当用户请求阿里云/Alibaba Cloud/Alicloud 的 ROS 模板、资源栈、Terraform alicloud provider "
            "模板生成、解释、完善、校验、询价、部署、更新或删除时，必须先调用 skill 工具加载 iac-aliyun。"
        ),
        user_invocable=False,
        skill_root=str(SKILL_DIR),
        auto_trigger={"script": "auto_trigger.py"},
    )
