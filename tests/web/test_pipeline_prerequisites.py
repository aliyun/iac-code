import json
import types

import pytest
from starlette.testclient import TestClient

from iac_code.pipeline.engine.prerequisites import (
    PrerequisiteDecision,
    PrerequisiteProgress,
    PrerequisiteResolution,
)
from iac_code.web import pipeline_prerequisites as pp


def _decision(status: str, message: str = "") -> PrerequisiteResolution:
    return PrerequisiteResolution(
        feature_flags={"enable_reviewing": status == "available"},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status=status,
                required_flags=["enable_reviewing"],
                message=message,
            )
        },
    )


@pytest.mark.asyncio
async def test_inspect_reports_satisfied_when_available(monkeypatch):
    monkeypatch.setattr(pp, "inspect_prerequisites", lambda *a, **k: _decision("available"))

    payload = await pp.inspect_review_step_prerequisite()

    assert payload["name"] == "infraguard"
    assert payload["satisfied"] is True
    assert payload["status"] == "available"
    assert payload["installable"] is False


@pytest.mark.asyncio
async def test_inspect_reports_installable_when_missing(monkeypatch):
    monkeypatch.setattr(pp, "inspect_prerequisites", lambda *a, **k: _decision("disabled_feature"))

    payload = await pp.inspect_review_step_prerequisite()

    assert payload["satisfied"] is False
    assert payload["status"] == "disabled_feature"
    # The bundled selling pipeline declares on_missing.web=prompt_install + installers.
    assert payload["installable"] is True


@pytest.mark.asyncio
async def test_stream_install_yields_progress_then_ok_result(monkeypatch):
    def fake_prepare(prerequisites, *, feature_flags, surface, choose_installer, progress_handler):
        assert surface == "web"
        assert feature_flags["enable_reviewing"] is True
        # The web chooser must pick the first installer non-interactively.
        progress_handler(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="direct-binary",
                phase="download",
                status="output",
                message="downloading",
                downloaded_bytes=512,
                total_bytes=1024,
            )
        )
        progress_handler(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="direct-binary",
                phase="post_install",
                status="output",
                message="policy update",
            )
        )
        return _decision("available")

    monkeypatch.setattr(pp, "prepare_prerequisites", fake_prepare)

    events = [event async for event in pp.stream_install_review_step_prerequisite()]

    phases = [e["phase"] for e in events]
    assert phases == ["download", "post_install", "result"]
    assert events[0]["downloaded_bytes"] == 512
    assert events[0]["total_bytes"] == 1024
    assert events[-1] == {
        "phase": "result",
        "status": "ok",
        "satisfied": True,
        "prerequisite_status": "available",
        "message": "",
    }
    # Lock released after the stream completes.
    assert pp.install_in_progress() is False


@pytest.mark.asyncio
async def test_stream_install_reports_error_result_when_install_fails(monkeypatch):
    def fake_prepare(prerequisites, *, feature_flags, surface, choose_installer, progress_handler):
        return _decision("install_failed", message="boom")

    monkeypatch.setattr(pp, "prepare_prerequisites", fake_prepare)

    events = [event async for event in pp.stream_install_review_step_prerequisite()]

    assert events[-1]["phase"] == "result"
    assert events[-1]["status"] == "error"
    assert events[-1]["satisfied"] is False
    assert events[-1]["message"] == "boom"


@pytest.mark.asyncio
async def test_install_in_progress_reflects_lock_state():
    assert pp.install_in_progress() is False
    async with pp._install_lock:
        assert pp.install_in_progress() is True
    assert pp.install_in_progress() is False


def test_get_prerequisite_endpoint_returns_detection_json(monkeypatch):
    from iac_code.web.app import create_app

    monkeypatch.setattr(pp, "inspect_prerequisites", lambda *a, **k: _decision("disabled_feature"))

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/settings/pipeline-review-step/prerequisite")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "infraguard"
    assert body["satisfied"] is False
    assert body["installable"] is True


def test_install_endpoint_streams_ndjson_events(monkeypatch):
    from iac_code.web.app import create_app

    def fake_prepare(prerequisites, *, feature_flags, surface, choose_installer, progress_handler):
        progress_handler(
            PrerequisiteProgress(
                name="infraguard",
                installer_id="direct-binary",
                phase="download",
                status="output",
                message="downloading",
                downloaded_bytes=256,
                total_bytes=1024,
            )
        )
        return _decision("available")

    monkeypatch.setattr(pp, "prepare_prerequisites", fake_prepare)

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/settings/pipeline-review-step/install")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert [e["phase"] for e in events] == ["download", "result"]
    assert events[-1]["status"] == "ok"
    assert events[-1]["satisfied"] is True


def test_install_endpoint_returns_409_when_install_in_progress(monkeypatch):
    from iac_code.web.app import create_app

    # install_in_progress() reads the module-global lock at call time.
    monkeypatch.setattr(pp, "_install_lock", types.SimpleNamespace(locked=lambda: True))

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/settings/pipeline-review-step/install")

    assert response.status_code == 409
