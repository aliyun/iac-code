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


def test_selling_skill_references_are_expanded_for_installed_artifacts(monkeypatch, tmp_path):
    setup_module = _load_setup_module(monkeypatch)
    build_lib = tmp_path / "build_lib"

    setup_module._copy_selling_skill_references(str(build_lib))

    for skill_name in setup_module.SELLING_IAC_ALIYUN_SKILLS:
        references = build_lib / "iac_code" / "pipeline" / "selling" / "skills" / skill_name / "references"
        assert references.is_dir()
        assert not references.is_symlink()
        assert (references / "ros-template.md").is_file()
        assert (references / "template-parameters.md").is_file()
        assert (references / "cloud-products" / "ecs.md").is_file()


def test_selling_skill_references_are_expanded_for_sdist_release_tree(monkeypatch, tmp_path):
    setup_module = _load_setup_module(monkeypatch)
    release_tree = tmp_path / "iac_code-0.6.0"

    setup_module._copy_selling_skill_references_to_sdist_release_tree(str(release_tree))

    for skill_name in setup_module.SELLING_IAC_ALIYUN_SKILLS:
        references = release_tree / "src" / "iac_code" / "pipeline" / "selling" / "skills" / skill_name / "references"
        assert references.is_dir()
        assert not references.is_symlink()
        assert (references / "ros-template.md").is_file()
        assert (references / "template-parameters.md").is_file()
        assert (references / "cloud-products" / "ecs.md").is_file()


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
    assert kwargs["version"] == "0.6.0"
    assert kwargs["package_dir"] == {"": "src"}
    assert "iac_code.pipeline.selling.tools" in kwargs["packages"]
    assert "iac_code.pipeline.selling.hooks" in kwargs["packages"]
    assert kwargs["entry_points"] == {"console_scripts": ["iac-code=iac_code.cli.main:app"]}
    assert kwargs["install_requires"]
    assert "a2a" in kwargs["extras_require"]
