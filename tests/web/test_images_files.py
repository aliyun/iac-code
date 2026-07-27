import asyncio
import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

VALID_PNG = b"\x89PNG\r\n\x1a\npng-data"


def _manager(tmp_path, *, cwd=None):
    from iac_code.web.session_manager import WebSessionManager

    project = cwd or (tmp_path / "project")
    project.mkdir(parents=True, exist_ok=True)
    return WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)


def _error_message(response) -> str:
    error = response.json()["error"]
    return error["message"] if isinstance(error, dict) else error


def _png_payload(session_id: str) -> dict[str, str]:
    return {
        "sessionId": session_id,
        "mediaType": "image/png",
        "data": base64.b64encode(VALID_PNG).decode("ascii"),
    }


def test_top_level_image_upload_returns_preview_url_and_serves_original_bytes(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        upload = client.post("/api/images", json=_png_payload(session.web_session_id))
        assert upload.status_code == 201
        payload = upload.json()
        preview = client.get(payload["previewUrl"])

    assert set(payload) == {"imageId", "mediaType", "previewUrl"}
    assert payload["mediaType"] == "image/png"
    assert "data" not in payload
    assert "base64" not in json.dumps(payload).lower()
    assert payload["previewUrl"] == "/api/images/{}?sessionId={}".format(payload["imageId"], session.web_session_id)
    assert preview.status_code == 200
    assert preview.content == VALID_PNG
    assert preview.headers["content-type"].startswith("image/png")


def test_session_scoped_image_upload_also_returns_preview_url(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/{}/images".format(session.session_id),
            json={"mediaType": "image/png", "data": base64.b64encode(VALID_PNG).decode("ascii")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["previewUrl"] == "/api/images/{}?sessionId={}".format(payload["imageId"], session.web_session_id)
    assert "data" not in payload


def test_archived_session_rejects_image_upload_from_both_routes(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="archived-image-session")
    session.archived = True
    app = create_app(session_manager=manager)
    scoped_payload = {
        "mediaType": "image/png",
        "data": base64.b64encode(VALID_PNG).decode("ascii"),
    }

    with TestClient(app) as client:
        top_level = client.post("/api/images", json=_png_payload(session.web_session_id))
        session_scoped = client.post(
            "/api/sessions/{}/images".format(session.session_id),
            json=scoped_payload,
        )

    for response in (top_level, session_scoped):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_archived"


@pytest.mark.asyncio
@pytest.mark.parametrize("route_kind", ["top-level", "session-scoped"])
async def test_stale_image_upload_cannot_write_to_recreated_session(tmp_path, monkeypatch, route_kind) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    old_session = manager.create_session(session_id="recreated-image-session")
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    payload = _png_payload(old_session.web_session_id)
    if route_kind == "session-scoped":
        payload.pop("sessionId")
        route = "/api/sessions/{}/images".format(old_session.web_session_id)
    else:
        route = "/api/images"
    body = json.dumps(payload).encode("utf-8")

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        upload_task = asyncio.create_task(
            client.post(route, content=slow_body(), headers={"Content-Type": "application/json"})
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(old_session.web_session_id))
        recreated = manager.create_session(cwd=old_session.cwd, session_id=old_session.session_id)
        release_body.set()
        upload = await asyncio.wait_for(upload_task, timeout=1)

    assert deleted.status_code == 200
    assert recreated is not old_session
    assert upload.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_ids", "session_ref"),
    [
        (("duplicate", "duplicate"), "duplicate"),
        (("duplicate-alpha", "duplicate-beta"), "duplicate-"),
    ],
)
async def test_top_level_image_upload_rejects_session_alias_rebinding(
    tmp_path, monkeypatch, session_ids, session_ref
) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session_a = manager.create_session(cwd=str(tmp_path / "project-a"), session_id=session_ids[0])
    session_b = manager.create_session(cwd=str(tmp_path / "project-b"), session_id=session_ids[1])
    assert manager.get_session(session_ref) is None
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    body = json.dumps(_png_payload(session_ref)).encode("utf-8")

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        upload_task = asyncio.create_task(
            client.post("/api/images", content=slow_body(), headers={"Content-Type": "application/json"})
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(session_a.web_session_id))
        assert manager.get_session(session_ref) is session_b
        release_body.set()
        upload = await asyncio.wait_for(upload_task, timeout=1)

    assert deleted.status_code == 200
    assert upload.status_code == 404


@pytest.mark.asyncio
async def test_top_level_image_upload_ignores_unrelated_prefix_mutation(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    target = manager.create_session(cwd=str(tmp_path / "project-a"), session_id="deploy")
    unrelated = manager.create_session(cwd=str(tmp_path / "project-b"), session_id="deploy-old")
    assert manager.get_session("deploy") is target
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    body = json.dumps(_png_payload("deploy")).encode("utf-8")

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        upload_task = asyncio.create_task(
            client.post("/api/images", content=slow_body(), headers={"Content-Type": "application/json"})
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(unrelated.web_session_id))
        assert manager.get_session("deploy") is target
        release_body.set()
        upload = await asyncio.wait_for(upload_task, timeout=1)

    assert deleted.status_code == 200
    assert upload.status_code == 201


@pytest.mark.parametrize("route_kind", ["top-level", "session-scoped"])
def test_cold_recovered_session_accepts_first_image_upload(tmp_path, monkeypatch, route_kind) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    seed_manager = _manager(tmp_path)
    seeded = seed_manager.create_session(session_id="cold-image-session")
    seed_manager.persist_web_metadata(seeded)

    manager = _manager(tmp_path)
    app = create_app(session_manager=manager)
    if route_kind == "session-scoped":
        route = "/api/sessions/{}/images".format(seeded.web_session_id)
        payload = {
            "mediaType": "image/png",
            "data": base64.b64encode(VALID_PNG).decode("ascii"),
        }
    else:
        route = "/api/images"
        payload = _png_payload(seeded.web_session_id)

    with TestClient(app) as client:
        response = client.post(route, json=payload)

    assert response.status_code == 201


def test_image_preview_uses_web_session_id_to_disambiguate_duplicate_bare_session_ids(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    manager = _manager(tmp_path, cwd=project_a)
    session_a = manager.create_session(session_id="same-session", cwd=str(project_a))
    session_b = manager.create_session(session_id="same-session", cwd=str(project_b))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        upload_a = client.post("/api/images", json=_png_payload(session_a.web_session_id))
        upload_b = client.post("/api/images", json=_png_payload(session_b.web_session_id))
        preview_a = client.get(upload_a.json()["previewUrl"])
        preview_b = client.get(upload_b.json()["previewUrl"])
        bare_session = client.get(
            "/api/images/{}?sessionId={}".format(upload_a.json()["imageId"], session_a.session_id)
        )
        wrong_session = client.get(
            "/api/images/{}?sessionId={}".format(upload_a.json()["imageId"], session_b.web_session_id)
        )

    assert upload_a.status_code == 201
    assert upload_b.status_code == 201
    assert "sessionId={}".format(session_a.web_session_id) in upload_a.json()["previewUrl"]
    assert "sessionId={}".format(session_b.web_session_id) in upload_b.json()["previewUrl"]
    assert preview_a.status_code == 200
    assert preview_a.content == VALID_PNG
    assert preview_b.status_code == 200
    assert preview_b.content == VALID_PNG
    assert bare_session.status_code == 404
    assert wrong_session.status_code == 404


def test_image_preview_rejects_missing_session_and_cwd_mismatch(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    manager = _manager(tmp_path, cwd=project_a)
    session_a = manager.create_session(session_id="session-a", cwd=str(project_a))
    session_b = manager.create_session(session_id="session-b", cwd=str(project_b))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        upload = client.post("/api/images", json=_png_payload(session_a.web_session_id))
        image_id = upload.json()["imageId"]
        wrong_session = client.get("/api/images/{}?sessionId={}".format(image_id, session_b.web_session_id))
        missing_session = client.get("/api/images/{}?sessionId=missing".format(image_id))
        invalid_id = client.get("/api/images/../escape?sessionId={}".format(session_a.web_session_id))

    assert wrong_session.status_code == 404
    assert missing_session.status_code == 404
    assert invalid_id.status_code == 404


def test_image_upload_falls_back_to_in_memory_cache_when_disk_write_fails(tmp_path, monkeypatch) -> None:
    from iac_code.web import images
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    def fail_open(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(images.os, "open", fail_open)

    with TestClient(app) as client:
        upload = client.post("/api/images", json=_png_payload(session.web_session_id))
        assert upload.status_code == 201
        payload = upload.json()
        preview = client.get(payload["previewUrl"])

    assert set(payload) == {"imageId", "mediaType", "previewUrl", "recoveryAvailable", "warning"}
    assert payload["mediaType"] == "image/png"
    assert payload["recoveryAvailable"] is False
    assert "Image was kept in memory" in payload["warning"]
    assert "data" not in payload
    assert "base64" not in json.dumps(payload).lower()
    assert preview.status_code == 200
    assert preview.content == VALID_PNG


def test_image_upload_rejects_encoded_payload_that_is_obviously_too_large_before_decoding(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web import images
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 3)
    decoded = {"called": False}

    def record_decode(*_args, **_kwargs):
        decoded["called"] = True
        return VALID_PNG

    monkeypatch.setattr("iac_code.web.app.base64.b64decode", record_decode)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/images",
            json={"sessionId": session.session_id, "mediaType": "image/png", "data": "A" * 32},
        )

    assert response.status_code == 400
    assert "too large" in _error_message(response)
    assert decoded["called"] is False


def test_image_upload_returns_controlled_error_when_in_memory_fallback_cache_is_full(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web import images
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    monkeypatch.setattr(images, "MAX_IN_MEMORY_FALLBACK_BYTES", len(VALID_PNG) - 1, raising=False)

    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    def fail_open(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(images.os, "open", fail_open)

    with TestClient(app) as client:
        response = client.post("/api/images", json=_png_payload(session.web_session_id))

    assert response.status_code == 507
    assert response.json() == {"error": {"message": "image fallback cache limit exceeded"}}


def test_image_upload_rejects_non_image_mismatch_oversize_and_text_only_model(tmp_path, monkeypatch) -> None:
    from iac_code.web import images
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: True)
    original_max_image_bytes = images.MAX_IMAGE_BYTES
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        non_image = client.post(
            "/api/images",
            json={
                "sessionId": session.session_id,
                "mediaType": "image/png",
                "data": base64.b64encode(b"not an image").decode("ascii"),
            },
        )
        mismatch = client.post(
            "/api/images",
            json={
                "sessionId": session.session_id,
                "mediaType": "image/jpeg",
                "data": base64.b64encode(VALID_PNG).decode("ascii"),
            },
        )
        monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(VALID_PNG) - 1)
        oversize = client.post("/api/images", json=_png_payload(session.session_id))

    assert non_image.status_code == 400
    assert "supported image" in _error_message(non_image)
    assert mismatch.status_code == 400
    assert "does not match media type" in _error_message(mismatch)
    assert oversize.status_code == 400
    assert "too large" in _error_message(oversize)

    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", original_max_image_bytes)
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: "text-only-model")
    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", lambda *args, **kwargs: False)
    text_only_app = create_app(session_manager=manager)
    with TestClient(text_only_app) as client:
        text_only = client.post("/api/images", json=_png_payload(session.session_id))

    assert text_only.status_code == 400
    assert _error_message(text_only) == "Current model text-only-model does not support image input."


def test_image_capability_checks_use_session_model_and_provider_over_global_defaults(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: "global-text-model")
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "global-provider")
    seen: list[tuple[str, str | None]] = []

    def supports(model, *, provider_key=None, **_kwargs):
        seen.append((model, provider_key))
        return (model, provider_key) == ("session-vision-model", "session-provider")

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", supports)
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-vision")
    session.model = "session-vision-model"
    session.provider = "session-provider"
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/images", json=_png_payload(session.web_session_id))

    assert response.status_code == 201
    assert seen == [("session-vision-model", "session-provider")]


def test_message_image_capability_rejects_session_text_model_even_when_global_model_supports_images(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: "global-vision-model")
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "global-provider")
    monkeypatch.setattr(
        "iac_code.services.capabilities.multimodal.is_model_multimodal",
        lambda model, **_kwargs: model == "global-vision-model",
    )
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-text")
    session.model = "session-text-model"
    session.provider = "session-provider"
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/{}/messages".format(session.web_session_id),
            json={"text": "describe", "imageIds": ["image-1"]},
        )

    assert response.status_code == 400
    assert _error_message(response) == "Current model session-text-model does not support image input."


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
async def test_message_rechecks_image_capability_after_waiting_for_model_update(tmp_path, monkeypatch, mode) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    capability_checked = asyncio.Event()

    def supports(model, *, provider_key=None, **_kwargs):
        capability_checked.set()
        return (provider_key, model) == ("openai", "gpt-5.5")

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", supports)
    manager = _manager(tmp_path)
    session = manager.create_session(
        session_id="session-1",
        mode=mode,
        context_id="ctx-1" if mode == "pipeline" else None,
        task_id="task-1" if mode == "pipeline" else None,
    )
    session.provider = "openai"
    session.model = "gpt-5.5"
    store_cached_image(
        "image-1",
        VALID_PNG,
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    runtime_requests = []
    pipeline_calls = []

    class Runtime:
        async def start_turn(self, request):
            runtime_requests.append(request)
            return {"accepted": True, "turnId": "turn-1"}

    class PipelineRunner:
        async def start(self, *_args, **_kwargs):
            pipeline_calls.append(True)
            return type(
                "Result",
                (),
                {
                    "accepted": True,
                    "status_code": 202,
                    "response": {"accepted": True},
                    "events": [],
                    "terminal_outcome": None,
                },
            )()

    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: Runtime(),
        pipeline_action_runner_factory=lambda: PipelineRunner(),
    )

    await session.turn_admission_lock.acquire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        update_task = asyncio.create_task(
            client.put(
                "/api/sessions/{}/model".format(session.web_session_id),
                json={"provider": "dashscope", "model": "qwen3.7-max"},
            )
        )
        await asyncio.sleep(0)
        message_task = asyncio.create_task(
            client.post(
                "/api/sessions/{}/messages".format(session.web_session_id),
                json={"text": "describe", "imageIds": ["image-1"]},
            )
        )
        await asyncio.wait_for(capability_checked.wait(), timeout=1)
        session.turn_admission_lock.release()
        update_response, message_response = await asyncio.gather(update_task, message_task)

    assert update_response.status_code == 200
    assert message_response.status_code == 400
    assert _error_message(message_response) == "Current model qwen3.7-max does not support image input."
    assert runtime_requests == []
    assert pipeline_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
async def test_message_freezes_checked_model_selection_for_background_turn(tmp_path, monkeypatch, mode) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "iac_code.services.capabilities.multimodal.is_model_multimodal",
        lambda model, *, provider_key=None, **_kwargs: (provider_key, model) == ("openai", "gpt-5.5"),
    )
    manager = _manager(tmp_path)
    session = manager.create_session(
        session_id="session-1",
        mode=mode,
        context_id="ctx-1" if mode == "pipeline" else None,
        task_id="task-1" if mode == "pipeline" else None,
    )
    session.provider = "openai"
    session.model = "gpt-5.5"
    session.effort = "high"
    store_cached_image(
        "image-1",
        VALID_PNG,
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    background_started = asyncio.Event()
    release_background = asyncio.Event()
    selections = []

    class Runtime:
        async def start_turn(self, request):
            selections.append(request.model_selection)
            background_started.set()
            await release_background.wait()
            return {"accepted": True, "turnId": "turn-1"}

    class PipelineRunner:
        async def start(self, *_args, **kwargs):
            selections.append(kwargs.get("model_selection"))
            background_started.set()
            await release_background.wait()
            return type(
                "Result",
                (),
                {
                    "accepted": True,
                    "status_code": 202,
                    "response": {"accepted": True},
                    "events": [],
                    "terminal_outcome": None,
                },
            )()

    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: Runtime(),
        pipeline_action_runner_factory=lambda: PipelineRunner(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        message_response = await client.post(
            "/api/sessions/{}/messages".format(session.web_session_id),
            json={"text": "describe", "imageIds": ["image-1"]},
        )
        await asyncio.wait_for(background_started.wait(), timeout=1)
        update_response = await client.put(
            "/api/sessions/{}/model".format(session.web_session_id),
            json={"provider": "dashscope", "model": "qwen3.7-max"},
        )
        release_background.set()

    assert message_response.status_code == 202
    assert update_response.status_code == 200
    assert len(selections) == 1
    assert selections[0].provider == "openai"
    assert selections[0].model == "gpt-5.5"
    assert selections[0].effort == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
async def test_partner_source_is_resolved_before_image_admission_and_background_turn(
    tmp_path, monkeypatch, mode
) -> None:
    from iac_code.services.qwenpaw_source import QwenPawConfig
    from iac_code.web.app import create_app
    from iac_code.web.images import store_cached_image

    partner = QwenPawConfig(
        model="partner-vision-model",
        provider_key="dashscope",
        api_key="fake-partner-key",
        base_url="https://partner.invalid/v1",
    )
    monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: None)
    monkeypatch.setattr("iac_code.config.load_saved_model", lambda: None)
    monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
    monkeypatch.setattr("iac_code.services.qwenpaw_source.load_from_qwenpaw", lambda: partner)
    capability_checks = []

    def supports_images(model, *, provider_key=None, base_url=None, api_key=None, **_kwargs):
        capability_checks.append((provider_key, model, base_url, api_key))
        return (provider_key, model, base_url, api_key) == (
            partner.provider_key,
            partner.model,
            partner.base_url,
            partner.api_key,
        )

    monkeypatch.setattr("iac_code.services.capabilities.multimodal.is_model_multimodal", supports_images)
    manager = _manager(tmp_path)
    session = manager.create_session(
        session_id="session-1",
        mode=mode,
        context_id="ctx-1" if mode == "pipeline" else None,
        task_id="task-1" if mode == "pipeline" else None,
    )
    store_cached_image(
        "image-1",
        VALID_PNG,
        media_type="image/png",
        cwd=session.cwd,
        session_id=session.session_id,
    )
    selections = []

    class Runtime:
        async def start_turn(self, request):
            selections.append(request.model_selection)
            return {"accepted": True, "turnId": request.turn_id}

    class PipelineRunner:
        async def start(self, *_args, **kwargs):
            selections.append(kwargs.get("model_selection"))
            return type(
                "Result",
                (),
                {
                    "accepted": True,
                    "status_code": 202,
                    "response": {"accepted": True},
                    "events": [],
                    "terminal_outcome": None,
                },
            )()

    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: Runtime(),
        pipeline_action_runner_factory=lambda: PipelineRunner(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/api/sessions/{}/messages".format(session.web_session_id),
            json={"text": "describe", "imageIds": ["image-1"]},
        )
        active = session.active_turn_task
        if isinstance(active, asyncio.Task):
            await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), timeout=1)

    assert response.status_code == 202
    assert capability_checks == [
        (partner.provider_key, partner.model, partner.base_url, partner.api_key),
        (partner.provider_key, partner.model, partner.base_url, partner.api_key),
    ]
    assert len(selections) == 1
    assert selections[0].provider == partner.provider_key
    assert selections[0].model == partner.model
    assert selections[0].provider_base_url == partner.base_url
    assert selections[0].provider_api_key == partner.api_key
    assert selections[0].provider_config_frozen is True


@pytest.mark.asyncio
async def test_delete_session_model_waits_for_turn_admission_and_rechecks_identity(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = _manager(tmp_path)
    session = manager.create_session(session_id="session-1")
    session.provider = "openai"
    session.model = "gpt-5.5"
    app = create_app(session_manager=manager)

    await session.turn_admission_lock.acquire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        delete_task = asyncio.create_task(client.delete("/api/sessions/{}/model".format(session.web_session_id)))
        await asyncio.sleep(0)
        assert not delete_task.done()
        manager._sessions.pop((session.cwd, session.session_id))
        session.turn_admission_lock.release()
        response = await delete_task

    assert response.status_code == 404
    assert session.provider == "openai"
    assert session.model == "gpt-5.5"


def test_file_search_returns_scoped_context_with_python_fallback(tmp_path, monkeypatch) -> None:
    from iac_code.web import files
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    (project / "nested" / "template.yaml").write_text("before\nkind: target\ncontext after\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("target should stay hidden\n", encoding="utf-8")
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)
    if hasattr(files, "shutil"):
        monkeypatch.setattr(files.shutil, "which", lambda _name: None)

    with TestClient(app) as client:
        response = client.get(
            "/api/files/search",
            params={"sessionId": session.session_id, "q": "target", "context": "1", "limit": "5"},
        )
        missing = client.get("/api/files/search", params={"sessionId": "missing", "q": "target"})

    assert response.status_code == 200
    assert missing.status_code == 404
    assert response.json() == {
        "results": [
            {
                "path": "nested/template.yaml",
                "lineNumber": 2,
                "column": 7,
                "text": "kind: target",
                "before": [{"lineNumber": 1, "text": "before"}],
                "after": [{"lineNumber": 3, "text": "context after"}],
            }
        ]
    }


def test_file_search_skips_large_files_and_keeps_searching_normal_files(tmp_path, monkeypatch) -> None:
    from iac_code.web import files
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    project.mkdir()
    (project / "large.txt").write_text("x" * 80 + "\nneedle in large file\n", encoding="utf-8")
    (project / "normal.txt").write_text("needle in normal file\n", encoding="utf-8")
    monkeypatch.setattr(files, "MAX_SEARCHABLE_FILE_BYTES", 32, raising=False)
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(
            "/api/files/search",
            params={"sessionId": session.web_session_id, "q": "needle", "limit": "10"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "path": "normal.txt",
                "lineNumber": 1,
                "column": 1,
                "text": "needle in normal file",
                "before": [],
                "after": [],
            }
        ]
    }


def test_quick_open_returns_file_candidates_and_rejects_traversal_queries(tmp_path) -> None:
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    target = project / "nested" / "main.tf"
    target.write_text("resource\n", encoding="utf-8")
    (project / ".venv").mkdir()
    (project / ".venv" / "main.py").write_text("hidden\n", encoding="utf-8")
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(
            "/api/files/quick-open",
            params={"sessionId": session.session_id, "q": "main", "limit": "10"},
        )
        traversal = client.get(
            "/api/files/quick-open",
            params={"sessionId": session.session_id, "q": "../main", "limit": "10"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {
                "path": "nested/main.tf",
                "name": "main.tf",
                "kind": "file",
                "size": target.stat().st_size,
            }
        ]
    }
    assert traversal.status_code == 400
    assert "query is invalid" in _error_message(traversal)


def test_history_search_reads_input_history_without_leaking_unmatched_entries(tmp_path, monkeypatch) -> None:
    from iac_code.config import get_history_path
    from iac_code.ui.core.input_history import InputHistory
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            [
                InputHistory._encode_entry("deploy ecs stack"),
                InputHistory._encode_entry("unmatched secret value"),
                "legacy deploy vpc",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manager = _manager(tmp_path)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/history/search", params={"q": "deploy", "limit": "10"})

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "entries": [
            {"index": 3, "text": "legacy deploy vpc"},
            {"index": 1, "text": "deploy ecs stack"},
        ]
    }
    assert "unmatched secret value" not in json.dumps(payload)


def test_history_search_with_session_includes_visible_user_turns_only(tmp_path, monkeypatch) -> None:
    from iac_code.agent.message import Message, create_recalled_memory_message
    from iac_code.config import get_history_path
    from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
    from iac_code.ui.core.input_history import InputHistory
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(InputHistory._encode_entry("global deploy prompt") + "\n", encoding="utf-8")
    project = tmp_path / "project"
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    manager.storage.append(session.cwd, session.session_id, Message(role="user", content="session deploy prompt"))
    manager.storage.append(session.cwd, session.session_id, Message(role="assistant", content="assistant deploy reply"))
    manager.storage.append(
        session.cwd,
        session.session_id,
        create_recalled_memory_message("hidden deploy memory", ["memory.md"]),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="user",
            content="hidden deploy cleanup",
            metadata={"type": CLEANUP_PROMPT_METADATA_TYPE},
        ),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(role="user", content="hidden deploy skill", metadata={"type": "internal-skill-context"}),
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(
            "/api/history/search",
            params={"sessionId": session.web_session_id, "q": "deploy", "limit": "10"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "entries": [
            {"index": 1, "text": "global deploy prompt"},
            {
                "index": 1,
                "text": "session deploy prompt",
                "source": "session",
                "sessionId": session.web_session_id,
            },
        ]
    }
    assert "assistant deploy reply" not in json.dumps(payload)
    assert "hidden deploy memory" not in json.dumps(payload)
    assert "hidden deploy cleanup" not in json.dumps(payload)
    assert "hidden deploy skill" not in json.dumps(payload)


def test_history_search_uses_web_session_id_to_scope_duplicate_bare_session_ids(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    manager = _manager(tmp_path, cwd=project_a)
    session_a = manager.create_session(session_id="same-session", cwd=str(project_a))
    session_b = manager.create_session(session_id="same-session", cwd=str(project_b))
    manager.storage.append(session_a.cwd, session_a.session_id, Message(role="user", content="alpha scoped prompt"))
    manager.storage.append(session_b.cwd, session_b.session_id, Message(role="user", content="beta scoped prompt"))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        bare = client.get("/api/history/search", params={"sessionId": "same-session", "q": "scoped", "limit": "10"})
        scoped_a = client.get(
            "/api/history/search",
            params={"sessionId": session_a.web_session_id, "q": "scoped", "limit": "10"},
        )
        scoped_b = client.get(
            "/api/history/search",
            params={"sessionId": session_b.web_session_id, "q": "scoped", "limit": "10"},
        )

    assert bare.status_code == 404
    assert scoped_a.status_code == 200
    assert scoped_a.json() == {
        "entries": [
            {"index": 1, "text": "alpha scoped prompt", "source": "session", "sessionId": session_a.web_session_id}
        ]
    }
    assert scoped_b.status_code == 200
    assert scoped_b.json() == {
        "entries": [
            {"index": 1, "text": "beta scoped prompt", "source": "session", "sessionId": session_b.web_session_id}
        ]
    }


def test_transcript_route_finds_visible_turn_and_filters_hidden_rows(tmp_path) -> None:
    from iac_code.agent.message import Message, create_recalled_memory_message
    from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(role="user", content="visible request", metadata={"turnId": "turn-1", "messageId": "msg-user"}),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        create_recalled_memory_message("hidden target memory", ["memory.md"]),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="user",
            content="hidden cleanup target",
            metadata={"type": CLEANUP_PROMPT_METADATA_TYPE, "turnId": "turn-1", "messageId": "msg-hidden"},
        ),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(role="assistant", content="visible response", metadata={"turnId": "turn-1", "messageId": "msg-reply"}),
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        by_turn = client.get("/api/transcript/turn-1", params={"sessionId": session.session_id})
        by_message = client.get("/api/transcript/msg-reply", params={"sessionId": session.session_id})
        missing = client.get("/api/transcript/missing", params={"sessionId": session.session_id})

    assert by_turn.status_code == 200
    assert by_turn.json() == {
        "turnId": "turn-1",
        "messages": [
            {"role": "user", "content": "visible request", "turnId": "turn-1", "messageId": "msg-user"},
            {"role": "assistant", "content": "visible response", "turnId": "turn-1", "messageId": "msg-reply"},
        ],
    }
    assert by_message.status_code == 200
    assert by_message.json() == by_turn.json()
    assert "hidden target memory" not in json.dumps(by_turn.json())
    assert "hidden cleanup target" not in json.dumps(by_turn.json())
    assert missing.status_code == 404


def test_transcript_route_does_not_bind_hidden_duplicate_metadata_to_visible_row(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
    from iac_code.web.app import create_app

    project = tmp_path / "project"
    manager = _manager(tmp_path, cwd=project)
    session = manager.create_session(session_id="session-1")
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="user",
            content="duplicate content",
            metadata={"type": CLEANUP_PROMPT_METADATA_TYPE, "turnId": "hidden-turn", "messageId": "hidden-msg"},
        ),
    )
    manager.storage.append(
        session.cwd,
        session.session_id,
        Message(
            role="user",
            content="duplicate content",
            metadata={"turnId": "visible-turn", "messageId": "visible-msg"},
        ),
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        hidden = client.get("/api/transcript/hidden-msg", params={"sessionId": session.web_session_id})
        visible = client.get("/api/transcript/visible-msg", params={"sessionId": session.web_session_id})

    assert hidden.status_code == 404
    assert visible.status_code == 200
    assert visible.json() == {
        "turnId": "visible-turn",
        "messages": [
            {"role": "user", "content": "duplicate content", "turnId": "visible-turn", "messageId": "visible-msg"}
        ],
    }
