"""打包与跨平台（设计文档 §18.7）。

新 pipeline 的 YAML、prompt、skill、hook 和 tool 必须能进入 wheel/sdist；部署 skill
使用 pipeline-local 副本，只有共享 reference 使用 symlink，打包阶段再物化为实际文件。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import setuptools

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PIPELINE_DIR = PROJECT_ROOT / "src" / "iac_code" / "pipeline" / "selling_solution_first"
SELLING_REFERENCES = PROJECT_ROOT / "src" / "iac_code" / "pipeline" / "selling" / "references"
SELLING_DEPLOYING_SKILL = PROJECT_ROOT / "src" / "iac_code" / "pipeline" / "selling" / "skills" / "iac-aliyun-deploying"
BUNDLED_REFERENCES = PROJECT_ROOT / "src" / "iac_code" / "skills" / "bundled" / "iac_aliyun" / "references"
SKILLS_WITH_REFERENCES = ("iac-aliyun-deploying", "iac-aliyun-materialize-selected-candidate")
# canonical 来源：只有 pipeline 自己的参数推荐规则来自 selling/references，其余来自 bundled 技能。
CANONICAL_ROOTS = {
    "template-parameter-recommendation.md": SELLING_REFERENCES,
}
REFERENCE_LINK_PATTERN = re.compile(r"\((references/[^)\s]*)\)")


def _reference_target(path: Path) -> Path | None:
    """Resolve a reference directory or a Windows core.symlinks=false placeholder."""
    if path.is_dir():
        return path.resolve()
    if not path.is_file():
        return None
    try:
        raw_target = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not raw_target or "\n" in raw_target or "\r" in raw_target:
        return None
    target = Path(raw_target)
    if not target.is_absolute():
        target = path.parent / target
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _is_reference_placeholder(path: Path) -> bool:
    return (
        path.name == "references" and path.is_file() and not path.is_symlink() and _reference_target(path) is not None
    )


def _pipeline_files() -> list[Path]:
    return [
        path
        for path in PIPELINE_DIR.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and not _is_reference_placeholder(path)
    ]


def _canonical_for(relative: Path) -> Path:
    root = CANONICAL_ROOTS.get(relative.parts[0], BUNDLED_REFERENCES)
    return root / relative


class TestRuntimeFilesArePackaged:
    def test_hook_and_tool_modules_are_discovered_as_packages(self):
        packages = set(setuptools.find_namespace_packages(where=str(PROJECT_ROOT / "src")))

        assert "iac_code.pipeline.selling_solution_first.hooks" in packages
        assert "iac_code.pipeline.selling_solution_first.tools" in packages
        # 与原 selling 使用同一套发现方式，不引入独立打包规则。
        assert "iac_code.pipeline.selling.hooks" in packages

    def test_pyproject_discovers_packages_under_src(self):
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        assert data["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]

    def test_every_resource_file_matches_a_declared_package_data_pattern(self):
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = data["tool"]["setuptools"]["package-data"]["iac_code"]
        suffixes = {pattern.rsplit("*", 1)[-1] for pattern in patterns if pattern.startswith("**/*.")}

        resources = [path for path in _pipeline_files() if path.suffix != ".py"]
        assert resources, "pipeline resources are missing from the source tree"
        for path in resources:
            assert path.suffix in suffixes, path.relative_to(PROJECT_ROOT)

    def test_pipeline_definition_prompts_hooks_and_tools_all_exist(self):
        relative = {path.relative_to(PIPELINE_DIR).as_posix() for path in _pipeline_files()}

        assert "pipeline.yaml" in relative
        assert {
            "prompts/solution_planning_and_selection.md",
            "prompts/materialize_selected_candidate.md",
            "prompts/deploying.md",
            "hooks/solution_planning_and_selection.py",
            "hooks/materialize_selected_candidate.py",
            "hooks/deploying.py",
            "tools/confirmed_ros_deploy_tool.py",
            "tools/candidate_planning_records.py",
            "tools/reused_selling_tools.py",
            "tools/show_architecture_plan_tool.py",
            "tools/show_candidate_detail_tool.py",
        } <= relative


class TestSkillReferences:
    def test_source_uses_a_local_deploying_skill_and_canonical_references(self):
        skills = PIPELINE_DIR / "skills"
        deploying = skills / "iac-aliyun-deploying"
        materialize_references = skills / "iac-aliyun-materialize-selected-candidate" / "references"

        assert deploying.is_dir()
        assert not deploying.is_symlink()
        assert deploying.resolve() != SELLING_DEPLOYING_SKILL.resolve()
        assert (deploying / "SKILL.md").is_file()
        assert (deploying / "SKILL.md").read_bytes() != (SELLING_DEPLOYING_SKILL / "SKILL.md").read_bytes()
        assert _reference_target(materialize_references) == SELLING_REFERENCES.resolve()
        assert _reference_target(deploying / "references") == SELLING_REFERENCES.resolve()

    @pytest.mark.parametrize("skill_name", SKILLS_WITH_REFERENCES)
    def test_references_are_byte_identical_to_the_canonical_content(self, skill_name):
        references = _reference_target(PIPELINE_DIR / "skills" / skill_name / "references")
        assert references is not None

        relative_files = sorted(
            path.relative_to(BUNDLED_REFERENCES) for path in BUNDLED_REFERENCES.rglob("*") if path.is_file()
        )
        for relative in relative_files:
            path = references / relative
            canonical = _canonical_for(relative)
            assert path.is_file(), path
            assert canonical.is_file(), canonical
            assert path.read_bytes() == canonical.read_bytes(), path
        assert len(relative_files) >= 11

    def test_both_skills_resolve_the_same_reference_tree(self):
        first, second = (
            _reference_target(PIPELINE_DIR / "skills" / name / "references") for name in SKILLS_WITH_REFERENCES
        )

        assert first == SELLING_REFERENCES.resolve()
        assert second == SELLING_REFERENCES.resolve()

    @pytest.mark.parametrize("skill_name", SKILLS_WITH_REFERENCES)
    def test_every_reference_link_in_the_skill_resolves(self, skill_name):
        skill_dir = PIPELINE_DIR / "skills" / skill_name
        references = _reference_target(skill_dir / "references")
        assert references is not None
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

        links = {match.group(1) for match in REFERENCE_LINK_PATTERN.finditer(text)}
        assert links
        for link in links:
            relative = Path(link).relative_to("references")
            assert (references / relative).exists(), f"{skill_name}: {link}"

    def test_step_one_skill_does_not_reference_template_references(self):
        skill_dir = PIPELINE_DIR / "skills" / "iac-aliyun-solution-first"

        # Step 1 不生成模板，因此不引用 references/，也不需要复制一份参考目录。
        assert not (skill_dir / "references").exists()
        assert "references/" not in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


class TestEncoding:
    def test_all_pipeline_files_are_utf8_readable(self):
        for path in _pipeline_files():
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:  # pragma: no cover - failure detail
                pytest.fail(f"{path.relative_to(PROJECT_ROOT)} is not valid UTF-8: {exc}")

    def test_pipeline_files_do_not_use_a_utf8_bom_or_crlf(self):
        for path in _pipeline_files():
            raw = path.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), path
            assert b"\r\n" not in raw, path
