from pathlib import Path

import pytest

from iac_code.agent.message import Message
from iac_code.commands.registry import PromptCommand
from iac_code.skills.auto_trigger import find_auto_triggered_skills, has_skill_tag
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource


def _command(tmp_path: Path, *, source: SkillSource = SkillSource.BUNDLED) -> PromptCommand:
    script = tmp_path / "auto_trigger.py"
    script.write_text(
        "ENABLE_AUTO_TRIGGER = True\ndef should_trigger(prompt):\n    return 'match me' in prompt\n",
        encoding="utf-8",
    )
    fm = SkillFrontmatter(description="demo", auto_trigger={"script": "auto_trigger.py"})
    skill = SkillDefinition(
        name="demo",
        description="demo",
        frontmatter=fm,
        content="Demo prompt",
        source=source,
        skill_root=str(tmp_path),
    )
    return PromptCommand(name="demo", description="demo", skill=skill, source=source)


def _mismatched_command(tmp_path: Path) -> PromptCommand:
    command = _command(tmp_path)
    return PromptCommand(
        name=command.name,
        description=command.description,
        skill=command.skill,
        source=SkillSource.PROJECT,
    )


def _bundled_command_with_project_skill(tmp_path: Path) -> PromptCommand:
    command = _command(tmp_path, source=SkillSource.PROJECT)
    return PromptCommand(
        name=command.name,
        description=command.description,
        skill=command.skill,
        source=SkillSource.BUNDLED,
    )


def test_has_skill_tag_detects_loaded_skill():
    assert has_skill_tag("<skill-name>iac-aliyun</skill-name>", "iac-aliyun")
    assert not has_skill_tag("<skill-name>other</skill-name>", "iac-aliyun")


def test_find_auto_triggered_skills_loads_bundled_script(tmp_path):
    matches = find_auto_triggered_skills("please match me", [_command(tmp_path)], loaded_skill_names=set())
    assert [cmd.name for cmd in matches] == ["demo"]


def test_find_auto_triggered_skills_ignores_project_scripts(tmp_path):
    matches = find_auto_triggered_skills(
        "please match me",
        [_command(tmp_path, source=SkillSource.PROJECT)],
        loaded_skill_names=set(),
    )
    assert matches == []


def test_find_auto_triggered_skills_ignores_project_command_even_with_bundled_skill(tmp_path):
    matches = find_auto_triggered_skills(
        "please match me",
        [_mismatched_command(tmp_path)],
        loaded_skill_names=set(),
    )
    assert matches == []


def test_find_auto_triggered_skills_ignores_bundled_command_with_project_skill(tmp_path):
    matches = find_auto_triggered_skills(
        "please match me",
        [_bundled_command_with_project_skill(tmp_path)],
        loaded_skill_names=set(),
    )
    assert matches == []


def test_find_auto_triggered_skills_respects_loaded_set(tmp_path):
    matches = find_auto_triggered_skills("please match me", [_command(tmp_path)], loaded_skill_names={"demo"})
    assert matches == []


def test_find_auto_triggered_skills_marks_context_skill_tag_as_loaded(tmp_path):
    loaded_skill_names: set[str] = set()

    matches = find_auto_triggered_skills(
        "please match me",
        [_command(tmp_path)],
        loaded_skill_names=loaded_skill_names,
        context_messages=[Message(role="user", content="<skill-name>demo</skill-name>\n\nDemo prompt")],
    )

    assert matches == []
    assert loaded_skill_names == {"demo"}


def test_find_auto_triggered_skills_respects_script_switch(tmp_path):
    command = _command(tmp_path)
    (tmp_path / "auto_trigger.py").write_text(
        "ENABLE_AUTO_TRIGGER = False\ndef should_trigger(prompt):\n    return True\n",
        encoding="utf-8",
    )
    matches = find_auto_triggered_skills("please match me", [command], loaded_skill_names=set())
    assert matches == []


def test_find_auto_triggered_skills_rejects_script_path_escape(tmp_path):
    outside_script = tmp_path / "outside.py"
    outside_script.write_text(
        "ENABLE_AUTO_TRIGGER = True\ndef should_trigger(prompt):\n    return True\n",
        encoding="utf-8",
    )
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    fm = SkillFrontmatter(description="demo", auto_trigger={"script": "../outside.py"})
    skill = SkillDefinition(
        name="demo",
        description="demo",
        frontmatter=fm,
        content="Demo prompt",
        source=SkillSource.BUNDLED,
        skill_root=str(skill_root),
    )
    command = PromptCommand(name="demo", description="demo", skill=skill, source=SkillSource.BUNDLED)

    matches = find_auto_triggered_skills("please match me", [command], loaded_skill_names=set())

    assert matches == []


@pytest.mark.asyncio
async def test_process_auto_triggered_skills_returns_processed_results(tmp_path):
    from iac_code.skills.auto_trigger import process_auto_triggered_skills

    results = await process_auto_triggered_skills("please match me", [_command(tmp_path)], loaded_skill_names=set())
    assert len(results) == 1
    assert results[0].skill_name == "demo"
    assert "<skill-name>demo</skill-name>" in results[0].new_messages[0]["content"]


def test_iac_aliyun_trigger_matches_clear_terraform_prompt():
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger("生成 terraform 模板，在阿里云上创建 VPC、VSwitch、ECS 和安全组")


def test_iac_aliyun_trigger_matches_issue_53_terraform_prompt_variant():
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger("生成terraform模版，在阿里云上，region为cn-beijing.")


def test_iac_aliyun_trigger_matches_ros_template_prompt():
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger("解释这个 ROS 模板里的 ALIYUN::ECS::Instance")


def test_iac_aliyun_trigger_matches_alicloud_provider_prompt():
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger('用 provider "alicloud" 写一个 ECS 安全组模板')


@pytest.mark.parametrize(
    "prompt",
    [
        "部署这个阿里云 ROS 模板为资源栈",
        "把阿里云资源栈模板部署到华东 1",
    ],
)
def test_iac_aliyun_trigger_matches_chinese_iac_deployment(prompt):
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Create an Alibaba Cloud ROS template for an ECS instance",
        "生成一个阿里云 ROS 模板，用于创建 ECS 实例",
        "Genera una plantilla ROS de Alibaba Cloud para una instancia ECS",
        "Crée un modèle ROS Alibaba Cloud pour une instance ECS",
        "Erstelle eine Alibaba Cloud ROS-Vorlage für eine ECS-Instanz",
        "Alibaba Cloud の ECS インスタンス用の ROS テンプレートを生成して",
        "Gere um modelo ROS do Alibaba Cloud para uma instância ECS",
    ],
)
def test_iac_aliyun_trigger_matches_supported_languages(prompt):
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert should_trigger(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Terraform 是什么？",
        "帮我写 AWS Terraform 创建 S3",
        "阿里云 ECS 价格怎么样？",
        "阿里云 ECS 部署失败了，帮我排查 SSH 登录",
        "ROS 机器人导航怎么做？",
    ],
)
def test_iac_aliyun_trigger_rejects_non_iac_prompts(prompt):
    from iac_code.skills.bundled.iac_aliyun.auto_trigger import should_trigger

    assert not should_trigger(prompt)
