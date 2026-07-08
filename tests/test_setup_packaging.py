from __future__ import annotations

import importlib.util
from pathlib import Path

import setuptools

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_setup_module(monkeypatch):
    setup_kwargs = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: setup_kwargs.update(kwargs))
    spec = importlib.util.spec_from_file_location("iac_code_setup_for_test", PROJECT_ROOT / "setup.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._TEST_SETUP_KWARGS = setup_kwargs
    return module


def _assert_expanded_selling_references(package_root: Path, skill_names: tuple[str, ...]) -> None:
    for skill_name in skill_names:
        references = package_root / "pipeline" / "selling" / "skills" / skill_name / "references"
        assert references.is_dir()
        assert not references.is_symlink()
        assert (references / "ros-template.md").is_file()
        assert (references / "template-parameters.md").is_file()
        assert (references / "cloud-products" / "ecs.md").is_file()
        recommendation = (references / "template-parameter-recommendation.md").read_text(encoding="utf-8")
        assert "ros_estimate_template_cost" in recommendation
        assert 'action="GetTemplateEstimateCost"' not in recommendation


def test_selling_skill_references_are_expanded_for_installed_artifacts(monkeypatch, tmp_path):
    setup_module = _load_setup_module(monkeypatch)
    build_lib = tmp_path / "build_lib"

    setup_module._copy_selling_skill_references(str(build_lib))

    _assert_expanded_selling_references(build_lib / "iac_code", setup_module.SELLING_IAC_ALIYUN_SKILLS)


def test_selling_skill_references_are_expanded_for_sdist_release_tree(monkeypatch, tmp_path):
    setup_module = _load_setup_module(monkeypatch)
    release_tree = tmp_path / "iac_code-0.6.0"

    setup_module._copy_selling_skill_references_to_sdist_release_tree(str(release_tree))

    _assert_expanded_selling_references(release_tree / "src" / "iac_code", setup_module.SELLING_IAC_ALIYUN_SKILLS)


def test_selling_skill_references_expand_windows_symlink_placeholder_files(monkeypatch, tmp_path):
    setup_module = _load_setup_module(monkeypatch)
    source_root = tmp_path / "src" / "iac_code"
    selling_refs = source_root / "pipeline" / "selling" / "references"
    bundled_refs = source_root / "skills" / "bundled" / "iac_aliyun" / "references"
    cloud_products = bundled_refs / "cloud-products"
    selling_refs.mkdir(parents=True)
    cloud_products.mkdir(parents=True)
    (bundled_refs / "ros-template.md").write_text("real ros template reference", encoding="utf-8")
    (bundled_refs / "template-parameters.md").write_text("real parameter reference", encoding="utf-8")
    (cloud_products / "ecs.md").write_text("real ecs reference", encoding="utf-8")
    (selling_refs / "template-parameter-recommendation.md").write_text(
        "pipeline ros_estimate_template_cost recommendation",
        encoding="utf-8",
    )
    (selling_refs / "ros-template.md").write_text(
        "../../../skills/bundled/iac_aliyun/references/ros-template.md",
        encoding="utf-8",
    )
    (selling_refs / "template-parameters.md").write_text(
        "../../../skills/bundled/iac_aliyun/references/template-parameters.md",
        encoding="utf-8",
    )
    (selling_refs / "cloud-products").write_text(
        "../../../skills/bundled/iac_aliyun/references/cloud-products",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_module, "SELLING_REFERENCES_DIR", selling_refs)
    build_lib = tmp_path / "build_lib"

    setup_module._copy_selling_skill_references(str(build_lib))

    package_root = build_lib / "iac_code"
    _assert_expanded_selling_references(package_root, setup_module.SELLING_IAC_ALIYUN_SKILLS)
    references = (
        package_root / "pipeline" / "selling" / "skills" / setup_module.SELLING_IAC_ALIYUN_SKILLS[0] / "references"
    )
    assert (references / "ros-template.md").read_text(encoding="utf-8") == "real ros template reference"
    assert (references / "cloud-products").is_dir()


def test_selling_pipeline_python_runtime_files_are_discovered_for_installed_artifacts():
    packages = set(setuptools.find_namespace_packages(where=str(PROJECT_ROOT / "src")))

    assert "iac_code.pipeline.selling.hooks" in packages
    assert "iac_code.pipeline.selling.tools" in packages


def test_legacy_setup_build_keeps_babel_install_fallback():
    setup_py = (PROJECT_ROOT / "setup.py").read_text(encoding="utf-8")

    assert 'pip", "install", "babel' in setup_py
    assert "ensurepip" in setup_py
    assert "apt-get" in setup_py
    assert "get-pip.py" in setup_py


def test_legacy_setup_declares_package_metadata(monkeypatch):
    setup_module = _load_setup_module(monkeypatch)
    kwargs = setup_module._TEST_SETUP_KWARGS

    assert kwargs["name"] == "iac_code"
    assert kwargs["version"] == "0.9.0"
    assert kwargs["package_dir"] == {"": "src"}
    assert "iac_code.pipeline.selling.tools" in kwargs["packages"]
    assert "iac_code.pipeline.selling.hooks" in kwargs["packages"]
    assert kwargs["entry_points"] == {"console_scripts": ["iac-code=iac_code.cli.main:app"]}
    assert kwargs["install_requires"]
    assert "a2a" in kwargs["extras_require"]
