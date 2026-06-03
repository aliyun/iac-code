from __future__ import annotations

import json

import pytest

from iac_code.services.session_metadata import (
    SESSION_METADATA_FILENAME,
    SESSION_NAME_PATTERN,
    SessionMetadata,
    normalize_session_name,
    read_session_metadata,
    validate_session_name,
    write_session_metadata,
)


@pytest.mark.parametrize("name", ["deploy", "deploy-prod", "prod_1", "release.2026", "A" * 200])
def test_validate_session_name_accepts_slug_names(name: str) -> None:
    assert validate_session_name(name) == name


@pytest.mark.parametrize("name", ["", " ", "deploy prod", "中文", "-bad", ".bad", "_bad", "A" * 201])
def test_validate_session_name_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_session_name(name)


def test_normalize_session_name_strips_then_validates() -> None:
    assert normalize_session_name(" deploy-prod ") == "deploy-prod"


def test_session_name_pattern_is_exported() -> None:
    assert SESSION_NAME_PATTERN.pattern == r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"


@pytest.mark.parametrize("schema_version", ["1", {}])
def test_metadata_from_dict_defaults_non_int_schema_version(schema_version: object) -> None:
    metadata = SessionMetadata.from_dict({"session_id": "abc123", "schema_version": schema_version})

    assert metadata is not None
    assert metadata.schema_version == 1


def test_metadata_from_dict_defaults_bool_schema_version() -> None:
    metadata = SessionMetadata.from_dict({"session_id": "abc123", "schema_version": True})

    assert metadata is not None
    assert type(metadata.schema_version) is int
    assert metadata.schema_version == 1


def test_metadata_round_trip(tmp_path) -> None:
    session_dir = tmp_path / "abc123"
    metadata = SessionMetadata(
        session_id="abc123",
        name="deploy-prod",
        cwd="/project",
        git_branch="main",
        created_at="2026-06-02T12:00:00Z",
        updated_at="2026-06-02T12:01:00Z",
    )

    write_session_metadata(session_dir, metadata)

    raw = json.loads((session_dir / SESSION_METADATA_FILENAME).read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["name"] == "deploy-prod"
    assert read_session_metadata(session_dir) == metadata


def test_read_session_metadata_ignores_corrupt_json(tmp_path) -> None:
    session_dir = tmp_path / "abc123"
    session_dir.mkdir()
    (session_dir / SESSION_METADATA_FILENAME).write_text("{not-json", encoding="utf-8")

    assert read_session_metadata(session_dir) is None
