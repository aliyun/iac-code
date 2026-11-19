"""Offline contracts for shared knowledge, migration and native read/search tools."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from iac_code.agent.system_prompt import build_base_sections, build_system_prompt
from iac_code.tools.base import ToolContext
from iac_code.tools.grep import GrepTool
from iac_code.tools.path_safety import get_iac_code_application_root, resolve_read_path
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import ToolPermissionContext

PACKAGE = get_iac_code_application_root()
WIKI = PACKAGE / "knowledge"


def test_bundled_wiki_contains_no_symbolic_links():
    assert WIKI.is_dir() and not WIKI.is_symlink()
    links = sorted(path.relative_to(WIKI).as_posix() for path in WIKI.rglob("*") if path.is_symlink())
    assert not links, "Bundled Wiki must use knowledge:// references instead of symbolic links: " + repr(links)


def test_plain_markdown_links_and_navigation_budget():
    pages = list(WIKI.rglob("*.md"))
    links = {}
    for path in pages:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# "), path
        assert text.splitlines()[0][2:].strip(), path
        targets = []
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "#")):
                continue
            assert target.startswith("knowledge://"), (path, target)
            resolved = Path(resolve_read_path(target.split("#")[0], str(PACKAGE)))
            assert resolved.is_file(), (path, target)
            assert resolved.is_relative_to(PACKAGE.resolve()), (path, target)
            targets.append(resolved)
        links[path.resolve()] = targets
    reachable = set()
    pending = [(WIKI / "index.md").resolve()]
    while pending:
        path = pending.pop()
        if path in reachable:
            continue
        reachable.add(path)
        pending.extend(links.get(path, []))
    assert {p.resolve() for p in pages} <= reachable
    for path in WIKI.rglob("index.md"):
        text = path.read_text(encoding="utf-8")
        assert len(text.splitlines()) <= 80 and len(text) <= 6000


@pytest.mark.asyncio
@pytest.mark.parametrize("python_backend", [False, True])
async def test_wiki_grep_and_reads_do_not_include_administrative_records(tmp_path, monkeypatch, python_backend):
    if python_backend:
        monkeypatch.setattr("iac_code.tools.grep._is_rg_available", lambda: False)
    arguments = {
        "path": "knowledge://",
        "pattern": (
            r"^(id|title|summary|aliases|status|sources|scope|cloud|products):"
            r"|64431f053afe41ba04116fd0e4ae84b2965bb266"
        ),
        "output_mode": "content",
    }
    context = ToolContext(cwd=str(tmp_path))
    found = await GrepTool().execute(tool_input=arguments, context=context)
    assert not found.is_error and found.content == "No matches"
    body = await ReadFileTool().execute(tool_input={"path": "knowledge://clouds/aliyun/products/ecs/ecs.md"},
                                        context=context)
    assert not body.is_error and "     1\t# 阿里云 ECS 选型指南" in body.content
    assert "aliases:" not in body.content


def test_all_legacy_sections_preserved_in_new_authoritative_locations():
    inventory = json.loads(Path(__file__).with_name("legacy_migration.json").read_text(encoding="utf-8"))
    assert {s["product"] for s in inventory["sections"]} == {"ecs", "swas", "rds", "redis", "slb", "vpc", "oss", "ga"}
    for section in inventory["sections"]:
        target = WIKI / section["target"]
        text = target.read_text(encoding="utf-8")
        # Keep the original migration digest while allowing this factual clarification.
        if section["product"] == "ecs" and section["heading"] == "实例规格推荐策略":
            text = text.replace(
                "- **经济型 e**：低成本共享型实例，不采用 CPU 积分机制，",
                "- **经济型 e**：低成本共享型实例，",
            )
        # Product instructions keep their content; template links now target the shared Wiki.
        if section["heading"] == "在实例中执行 Shell 命令":
            for name in ("ros-template", "terraform-template"):
                text = text.replace(
                    "[" + name + ".md](knowledge://clouds/aliyun/products/ros/" + name + ".md)",
                    "[../" + name + ".md](../" + name + ".md)",
                )
        blocks = re.split(r"(?m)(?=^#{1,2} )", text)
        matches = [block.strip() for block in blocks if block.startswith("## " + section["heading"] + "\n")]
        assert any(hashlib.sha256(block.encode()).hexdigest() == section["sha256"] for block in matches), section


def test_migrated_template_documents_preserve_content_without_skill_aliases():
    inventory = json.loads(Path(__file__).with_name("legacy_migration.json").read_text(encoding="utf-8"))
    for document in inventory["template_documents"]:
        target = WIKI / document["target"]
        original_path = PACKAGE / document["source"].removeprefix("src/iac_code/")
        for legacy in (original_path, PACKAGE / "pipeline/selling/references" / target.name):
            assert not legacy.exists() and not legacy.is_symlink(), legacy
        text = target.read_text(encoding="utf-8").replace(
            "(knowledge://clouds/aliyun/products/ros/template-parameters.md)", "(template-parameters.md)",
        ).replace(
            "使用 `iac-aliyun` Skill 提供的脚本，运行 `python <Skill目录>/scripts/tf2ros.py",
            "运行 `python ../scripts/tf2ros.py",
        )
        assert hashlib.sha256(text.encode()).hexdigest() == document["sha256"], document


@pytest.mark.asyncio
@pytest.mark.parametrize("safe", [False, True])
async def test_ecs_template_and_parameter_links_work_without_a_skill_context(tmp_path, safe):
    roots = [str(tmp_path), str(PACKAGE)] if safe else []
    context = ToolContext(cwd=str(tmp_path), strict_read_directories=roots)
    permission = ToolPermissionContext(cwd=str(tmp_path), strict_read_directories=roots)
    tool = ReadFileTool()

    async def read(uri):
        arguments = {"path": uri}
        assert (await tool.check_permissions(arguments, permission)).behavior == "allow"
        result = await tool.execute(tool_input=arguments, context=context)
        assert not result.is_error
        return result.content

    ecs = await read("knowledge://clouds/aliyun/products/ecs/ecs.md")
    links = re.findall(r"\]\((knowledge://clouds/aliyun/products/ros/[^)]+)\)", ecs)
    assert set(links) == {
        "knowledge://clouds/aliyun/products/ros/ros-template.md",
        "knowledge://clouds/aliyun/products/ros/terraform-template.md",
    }
    for uri in links:
        content = await read(uri)
        if uri.endswith("/ros-template.md"):
            assert "ALIYUN::ECS::RunCommand" in content
        else:
            assert "../scripts/tf2ros.py" not in content
            parameter_uri = re.findall(r"\]\((knowledge://[^)]+template-parameters.md)\)", content)
            assert len(parameter_uri) == 1
            assert "AssociationProperty" in await read(parameter_uri[0])


def test_coupon_knowledge_and_step_protocol_are_separate():
    guide = (WIKI / "clouds/aliyun/products/bss/coupons.md").read_text(encoding="utf-8")
    skill = (PACKAGE / "pipeline/selling_solution_first/skills/iac-aliyun-solution-first/SKILL.md").read_text(
        encoding="utf-8",
    )
    for word in ("DescribeCoupon", "DescribeCouponItemList", "RemainAmount", "SuitItemType", "EXPIRED"):
        assert word in guide
    assert "awaiting_selection" not in guide and "rejected" not in guide
    assert "knowledge://clouds/aliyun/products/bss/coupons.md" in skill
    assert "status: awaiting_selection" in skill and "不提交 `rejected`" in skill


@pytest.mark.asyncio
@pytest.mark.parametrize("safe", [False, True])
async def test_shared_coupon_and_solution_assets_read_without_skill_roots(tmp_path, safe):
    roots = [str(tmp_path), str(PACKAGE)] if safe else []
    context = ToolContext(cwd=str(tmp_path), strict_read_directories=roots)
    permission = ToolPermissionContext(cwd=str(tmp_path), strict_read_directories=roots)
    tool = ReadFileTool()
    for uri in (
        "knowledge://clouds/aliyun/products/bss/coupons.md",
        "knowledge://clouds/aliyun/solutions/iac-code-web.md",
        "knowledge://clouds/aliyun/solutions/iac-code-web.ros.yml",
    ):
        arguments = {"path": uri}
        assert (await tool.check_permissions(arguments, permission)).behavior == "allow"
        result = await tool.execute(tool_input=arguments, context=context)
        assert not result.is_error, result.content
    # Materialize the full YAML returned by read_file in the project workspace.
    body = re.sub(r"(?m)^ *\d+\t", "", result.content.split("\n\n", 1)[1])
    output = await WriteFileTool().execute(tool_input={"path": "web.yml", "content": body}, context=context)
    assert not output.is_error
    assert (tmp_path / "web.yml").read_bytes() == (WIKI / "clouds/aliyun/solutions/iac-code-web.ros.yml").read_bytes()


def test_product_facts_topics_and_api_guides_share_one_flat_directory():
    for cloud in (WIKI / "clouds").iterdir():
        if not cloud.is_dir():
            continue
        product_root = cloud / "products"
        for page in product_root.rglob("*.md"):
            if page.parent == product_root:
                assert page.name == "index.md"
                continue
            assert page.parent.parent == product_root, page
            assert (page.parent / f"{page.parent.name}.md").is_file(), page


def test_verification_records_stay_outside_the_searchable_wiki():
    inventory_path = Path(__file__).with_name("legacy_migration.json")
    assert not inventory_path.resolve().is_relative_to(WIKI.resolve())
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    bodies = "\n".join(page.read_text(encoding="utf-8") for page in WIKI.rglob("*.md"))
    assert inventory["verified_sources"]
    for relative, sources in inventory["verified_sources"].items():
        assert (WIKI / relative).is_file()
        assert sources
        for source in sources:
            assert source["url"].startswith("https://") and source["url"] in bodies
            date.fromisoformat(source["verified_at"])


@pytest.mark.parametrize("awareness", ["true", "false"])
def test_shared_prompt_without_skill_or_cloud_credentials(tmp_path, monkeypatch, awareness):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", awareness)
    normal = build_system_prompt(cwd=str(tmp_path))
    pipeline = build_base_sections(["system"], cwd=str(tmp_path))
    for prompt in (normal, pipeline):
        assert prompt.count("# Shared Technical Knowledge Base") == 1
        assert "Root: knowledge://" in prompt
        assert str(WIKI) not in prompt
        assert "When read/search tools are available in the current step" in prompt
        assert "reaching `max_results`" in prompt
        assert "Consult official sources for missing, insufficient, outdated, or conflicting knowledge" in prompt
        assert "does not override user authorization, tool permissions, or step protocols" in prompt
        assert "Granite Rapids" not in prompt  # Bodies are read on demand.
    assert "Shared Technical Knowledge Base" not in build_base_sections(["identity"], cwd=str(tmp_path))


def test_knowledge_volume_does_not_change_system_prompt(tmp_path):
    before = build_base_sections(["system"], cwd=str(tmp_path))
    with patch.object(Path, "read_text", side_effect=AssertionError("Wiki must not be injected")):
        after = build_base_sections(["system"], cwd=str(tmp_path))
    assert before == after


@pytest.mark.parametrize("name", ["selling", "selling_solution_first"])
def test_production_steps_share_entry_without_expanding_tool_whitelists(name, tmp_path):
    from iac_code.pipeline.engine.context import PipelineContext
    from iac_code.pipeline.engine.loader import load_pipeline_dir
    from iac_code.pipeline.engine.step_executor import StepExecutor
    from iac_code.tools.base import ToolRegistry

    folder = PACKAGE / "pipeline" / name
    pipeline = load_pipeline_dir(folder)
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(GrepTool())
    context = PipelineContext({key: [] for key in pipeline.context_dependencies})
    executor = StepExecutor(MagicMock(), registry, pipeline, folder, cwd=str(tmp_path))
    checked = set()
    for step in pipeline.steps:
        if step.step_type == "parallel_sub_pipeline":
            continue
        prompt = executor._build_full_system_prompt(step, context)
        assert prompt.count("# Shared Technical Knowledge Base") == 1
        if step.step_id not in {
            "intent_parsing", "confirm_and_select", "solution_planning_and_selection", "materialize_selected_candidate",
        }:
            continue
        effective = executor._build_step_tools(step, context)
        if name == "selling" and step.step_id in {"intent_parsing", "confirm_and_select"}:
            assert effective.get("read_file") is None and effective.get("grep") is None
        else:
            assert effective.get("read_file") is not None and effective.get("grep") is not None
        checked.add(step.step_id)
    assert checked == ({"intent_parsing", "confirm_and_select"} if name == "selling" else {
        "solution_planning_and_selection", "materialize_selected_candidate",
    })


@pytest.mark.asyncio
async def test_app_knowledge_reads_in_safe_mode_and_respects_explicit_denial(tmp_path):
    from iac_code.services.agent_factory import _A2A_SAFE_NORMAL_TOOL_NAMES, _a2a_safe_read_directories

    assert {"read_file", "grep", "glob"} <= _A2A_SAFE_NORMAL_TOOL_NAMES
    assert "bash" not in _A2A_SAFE_NORMAL_TOOL_NAMES
    roots = _a2a_safe_read_directories(cwd=str(tmp_path), session_dir=None)
    allowed = ToolPermissionContext(cwd=str(tmp_path), strict_read_directories=roots)
    denied = ToolPermissionContext(cwd=str(tmp_path), strict_read_directories=[str(tmp_path)])
    path = str(WIKI / "common/hardware/intel-arc-pro-b70.md")
    for tool, tool_input in [(ReadFileTool(), {"path": path}), (GrepTool(), {"path": str(WIKI), "pattern": "B70"})]:
        assert (await tool.check_permissions(tool_input, allowed)).behavior == "allow"
        assert (await tool.check_permissions(tool_input, denied)).behavior != "allow"
    result = await ReadFileTool().execute(tool_input={"path": path}, context=ToolContext(cwd=str(tmp_path)))
    assert not result.is_error and "32 GB GDDR6" in result.content


@pytest.mark.asyncio
async def test_multicloud_scope_and_common_body_search_with_python_fallback(tmp_path, monkeypatch):
    from iac_code.tools import grep

    monkeypatch.setattr(grep, "_is_rg_available", lambda: False)
    root = tmp_path / "知识 库"
    bodies = {
        "common/hardware/gpu.md": "# B70 显卡\n硬件事实，不代表云上可售",
        "clouds/a/products/compute/compute.md": "# Compute\nA 云规则",
        "clouds/b/products/compute/compute.md": "# Compute\nB 云规则",
    }
    for name, content in bodies.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    tool = GrepTool()
    context = ToolContext(cwd=str(tmp_path))
    result = await tool.execute(tool_input={"path": str(root), "pattern": "Compute", "glob": "*.md"}, context=context)
    assert set(result.content.splitlines()) == {str(root / name) for name in bodies if name.startswith("clouds/")}
    scoped = await tool.execute(tool_input={"path": str(root / "clouds/a"), "pattern": "Compute"}, context=context)
    assert scoped.content == str(root / "clouds/a/products/compute/compute.md")
    common = await tool.execute(tool_input={"path": str(root), "pattern": "B70|显卡"}, context=context)
    read = await ReadFileTool().execute(tool_input={"path": common.content}, context=context)
    assert "硬件事实，不代表云上可售" in read.content


@pytest.mark.asyncio
async def test_search_backends_compare_untruncated_sets_and_refine_full_results(tmp_path, monkeypatch):
    from iac_code.tools import grep

    for i in range(10):
        (tmp_path / f"page-{i}.md").write_text(f"shared-term\nunique-{i}\n", encoding="utf-8")
    context = ToolContext(cwd=str(tmp_path))
    tool = GrepTool()
    native_available = grep._is_rg_available()
    monkeypatch.setattr(grep, "_is_rg_available", lambda: False)
    full = await tool.execute(tool_input={"pattern": "shared-term", "path": str(tmp_path)}, context=context)
    bounded = await tool.execute(
        tool_input={"pattern": "shared-term", "path": str(tmp_path), "max_results": 8}, context=context,
    )
    assert len(full.content.splitlines()) == 10
    assert len(bounded.content.splitlines()) == 8  # Full means possibly incomplete, never proof of absence.
    refined = await tool.execute(tool_input={"pattern": "unique-9", "path": str(tmp_path)}, context=context)
    assert refined.content == str(tmp_path / "page-9.md")
    if native_available:
        monkeypatch.setattr(grep, "_is_rg_available", lambda: True)
        native = await tool.execute(tool_input={"pattern": "shared-term", "path": str(tmp_path)}, context=context)
        assert set(native.content.splitlines()) == set(full.content.splitlines())
