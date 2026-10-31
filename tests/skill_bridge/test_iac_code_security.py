from __future__ import annotations

import importlib.util
import io
import runpy
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/iac-code/scripts/iac_code.py"


@pytest.fixture
def bridge():
    spec = importlib.util.spec_from_file_location("iac_code_security_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download_root(bridge) -> str:
    channel = "candidates" if bridge.RUNTIME_TAG.startswith("candidate-") else "releases"
    return "https://{}/github-releases/aliyun/iac-code/skill-runtime/{}/{}/".format(
        bridge.RUNTIME_DOWNLOAD_HOST, channel, bridge.RUNTIME_TAG
    )


@pytest.mark.parametrize(
    "invalid_url",
    [
        None,
        "file:///tmp/runtime.zip",
        "http://ros-public-tools.oss-cn-beijing.aliyuncs.com/runtime.zip",
        "https://127.0.0.1/runtime.zip",
        "https://ros-public-tools.oss-cn-beijing.aliyuncs.com.evil.example/runtime.zip",
        "https://ros-public-tools.oss-cn-beijing.aliyuncs.com@evil.example/runtime.zip",
        "https://user@ros-public-tools.oss-cn-beijing.aliyuncs.com/runtime.zip",
        "https://ros-public-tools.oss-cn-beijing.aliyuncs.com:8443/runtime.zip",
        "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/other/runtime.zip",
        "https://[invalid/runtime.zip",
    ],
)
def test_download_rejects_untrusted_destination_before_open(bridge, monkeypatch, tmp_path, invalid_url) -> None:
    def unexpected_open(*_args):
        pytest.fail("rejected URL must not create a network opener")

    monkeypatch.setattr(bridge.urllib.request, "build_opener", unexpected_open)
    destination = tmp_path / "runtime.part"
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._download(invalid_url, destination, 100)
    assert caught.value.code == "runtime_download_url_rejected"
    assert caught.value.retryable is False
    assert not destination.exists()


@pytest.mark.parametrize("suffix", ["../runtime.zip", "%2e%2e/runtime.zip", "sub/runtime.zip", "runtime.zip?q=1",
                                    "runtime.zip#fragment", "runtime.zip\n", "runtime.zip\\outside", ""])
def test_download_rejects_noncanonical_paths_and_url_components(bridge, suffix) -> None:
    with pytest.raises(bridge.BridgeError, match="not allowed"):
        bridge._validate_runtime_download_url(_download_root(bridge) + suffix)


def test_download_requires_the_pinned_runtime_directory(bridge) -> None:
    with pytest.raises(bridge.BridgeError, match="not allowed"):
        bridge._validate_runtime_download_url(_download_root(bridge).replace(bridge.RUNTIME_TAG, "v999.0.0") + "a.zip")


@pytest.mark.parametrize("runtime_tag", ["v0.12.0", "candidate-20261031T010000Z-aaaaaaaaaaaa"])
def test_download_accepts_only_the_pinned_release_or_candidate(bridge, monkeypatch, tmp_path, runtime_tag) -> None:
    monkeypatch.setattr(bridge, "RUNTIME_TAG", runtime_tag)
    url = _download_root(bridge) + "runtime.zip"
    calls = []

    def fake_build_opener(handler):
        assert isinstance(handler, bridge._RuntimeDownloadRedirectHandler)

        def open_request(request, timeout):
            calls.append((request.full_url, timeout))
            return io.BytesIO(b"verified fixture")

        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(bridge.urllib.request, "build_opener", fake_build_opener)
    destination = tmp_path / "runtime.part"
    bridge._download(url, destination, 100, len(b"verified fixture"))
    assert calls == [(url, 30)]
    assert destination.read_bytes() == b"verified fixture"


@pytest.mark.parametrize("target", ["https://evil.example/runtime.zip", "http://127.0.0.1/runtime.zip",
                                   "file:///tmp/runtime.zip"])
def test_each_redirect_hop_is_checked_before_following(bridge, target) -> None:
    handler = bridge._RuntimeDownloadRedirectHandler()
    original = urllib.request.Request(_download_root(bridge) + "first.zip")
    intermediate = handler.redirect_request(original, None, 302, "Found", {}, _download_root(bridge) + "second.zip")
    assert intermediate.full_url == _download_root(bridge) + "second.zip"
    with pytest.raises(bridge.BridgeError) as caught:
        handler.redirect_request(intermediate, None, 302, "Found", {}, target)
    assert caught.value.code == "runtime_download_url_rejected"


@pytest.mark.parametrize("stream_failure", [True, False])
def test_worker_never_replays_an_unacknowledged_initial_request(bridge, monkeypatch, tmp_path, stream_failure) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "private"))
    job_id = "a" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._atomic_json(job_path, {"jobId": job_id})
    request_path = root / "request.json"
    payload = {"jsonrpc": "2.0", "method": "SendStreamingMessage", "params": {}}
    bridge._atomic_json(request_path, payload)
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})
    sent = []

    def stream(_record, request):
        sent.append(request)
        if stream_failure:
            raise bridge.BridgeError("a2a_transport_failed", "fixture lost acknowledgement", True)
        return iter(())

    monkeypatch.setattr(bridge, "_stream_jsonrpc", stream)
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.worker(job_id, request_path)
    assert caught.value.code == "a2a_request_outcome_unknown"
    assert caught.value.retryable is False
    assert sent == [payload]


def test_worker_recovers_a_known_task_without_replaying_the_write(bridge, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "private"))
    job_id = "b" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._atomic_json(job_path, {"jobId": job_id, "taskId": "task-original"})
    request_path = root / "request.json"
    bridge._atomic_json(request_path, {"method": "SendStreamingMessage"})
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {"port": 1, "token": "fake-token"})
    sent = []

    def stream(_record, payload):
        sent.append(payload["method"])
        if len(sent) == 1:
            raise bridge.BridgeError("a2a_transport_failed", "fixture disconnected", True)
        return iter(())

    statuses = iter(["TASK_STATE_WORKING", "TASK_STATE_COMPLETED"])
    queries = []

    def get_task(*_args, **kwargs):
        queries.append(kwargs["payload"]["method"])
        return {"result": {"id": "task-original", "status": {"state": next(statuses)}}}

    monkeypatch.setattr(bridge, "_stream_jsonrpc", stream)
    monkeypatch.setattr(bridge, "_http_json", get_task)
    monkeypatch.setattr(bridge, "_append_projection", lambda *_args: None)
    assert bridge.worker(job_id, request_path) == 0
    assert sent == ["SendStreamingMessage", "SubscribeToTask"]
    assert queries == ["GetTask", "GetTask"]


def test_agenthub_package_excludes_harness_settings_even_if_present_in_staging(monkeypatch, tmp_path) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "skill-runtime"))
    package = runpy.run_path(str(ROOT / "skill-runtime/package_skill.py"))
    profile = package["skill_profile"]("alibabacloud-iac-code")
    staged = package["render_profile"](profile.name, tmp_path / "staging")
    for harness in (".claude", ".qwen", ".agents"):
        directory = staged / harness
        directory.mkdir()
        (directory / "settings.json").write_text('{"enabledPlugins":{"fake-observer":true}}', encoding="utf-8")
    output = tmp_path / "skill.zip"
    package["deterministic_zip"](staged, profile, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [profile.archive_root + "/" + name for name in profile.output_files]
        assert len(archive.namelist()) == 4
        assert all(b"fake-observer" not in archive.read(name) for name in archive.namelist())
