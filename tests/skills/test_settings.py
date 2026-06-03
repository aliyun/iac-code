"""Tests for skill settings persistence."""

from __future__ import annotations

import yaml

from iac_code.skills.settings import load_disabled_skills, save_disabled_skills


def test_load_disabled_skills_missing_file(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    monkeypatch.setattr("iac_code.skills.settings.get_settings_path", lambda: settings)

    assert load_disabled_skills() == set()


def test_load_disabled_skills_ignores_invalid_yaml(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text("[", encoding="utf-8")
    monkeypatch.setattr("iac_code.skills.settings.get_settings_path", lambda: settings)

    assert load_disabled_skills() == set()


def test_load_disabled_skills_ignores_non_list(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text(yaml.safe_dump({"disabled_skills": "demo"}), encoding="utf-8")
    monkeypatch.setattr("iac_code.skills.settings.get_settings_path", lambda: settings)

    assert load_disabled_skills() == set()


def test_load_disabled_skills_normalizes_strings(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text(yaml.safe_dump({"disabled_skills": [" Demo ", "", 7, "Other"]}), encoding="utf-8")
    monkeypatch.setattr("iac_code.skills.settings.get_settings_path", lambda: settings)

    assert load_disabled_skills() == {"demo", "other"}


def test_save_disabled_skills_preserves_other_settings_and_excludes_locked(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text(yaml.safe_dump({"activeProvider": "dashscope"}), encoding="utf-8")
    monkeypatch.setattr("iac_code.skills.settings.get_settings_path", lambda: settings)

    save_disabled_skills({"beta", "alpha", "iac-aliyun"}, locked_skill_names={"iac-aliyun"})

    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert data["activeProvider"] == "dashscope"
    assert data["disabled_skills"] == ["alpha", "beta"]
