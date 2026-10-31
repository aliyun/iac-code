from __future__ import annotations

import importlib.util
import io
import json
import runpy
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/iac-code/scripts/iac_code.py"


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("iac_code_security_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    installed = tmp_path / "installed-skill"
    installed.mkdir()
    monkeypatch.setattr(module, "SKILL_ROOT", installed)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "private"))
    monkeypatch.delenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", raising=False)
    monkeypatch.delenv("IAC_CODE_SKILL_CHANNEL", raising=False)
    return module


def _approval(bridge):
    return {
        "host": bridge.RUNTIME_DOWNLOAD_HOST,
        "runtimeTag": bridge.RUNTIME_TAG,
        "manifestSha256": bridge.MANIFEST_SHA256,
        "approvedBy": "offline-test-operator",
        "approvalReference": "offline-test-approval-record",
        "expiresAt": "2099-01-01T00:00:00Z",
    }


def _write_approval(bridge, value):
    (bridge.SKILL_ROOT / "config.json").write_text(json.dumps({"runtimeDownloadApproval": value}), encoding="utf-8")


def test_installer_environment_works_before_the_skill_directory_is_created(bridge, monkeypatch) -> None:
    bridge.SKILL_ROOT.rmdir()
    monkeypatch.setenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", json.dumps(_approval(bridge)))
    monkeypatch.setenv("IAC_CODE_SKILL_CHANNEL", "agenthub-e2e")
    config = bridge._skill_config()
    assert config["runtimeDownloadApproval"] == _approval(bridge)
    assert config["channel"] == "skill/agenthub-e2e"
    assert config["pipelineName"] == bridge.DEFAULT_PIPELINE_NAME
    bridge._require_runtime_download_approval()
    assert not bridge.SKILL_ROOT.exists()


@pytest.mark.parametrize(
    "record",
    ["not-json", "{}", "true", '"external-record"', "null", "x" * (16 * 1024 + 1), "\udcff"],
    ids=["invalid-json", "missing-fields", "boolean", "string", "null", "too-large", "invalid-utf8"],
)
def test_invalid_installer_environment_denies_network(bridge, monkeypatch, tmp_path, record) -> None:
    monkeypatch.setenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", record)
    monkeypatch.setattr(
        bridge.urllib.request, "build_opener", lambda *_: pytest.fail("invalid approval reached network")
    )
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._download(_download_root(bridge) + "runtime.zip", tmp_path / "runtime.part", 100)
    assert caught.value.code == "skill_configuration_invalid"
    assert record not in caught.value.message


def test_installer_environment_cannot_override_invalid_local_approval(bridge, monkeypatch) -> None:
    local = _approval(bridge)
    local["runtimeTag"] = "v999.0.0"
    _write_approval(bridge, local)
    monkeypatch.setenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", json.dumps(_approval(bridge)))
    with pytest.raises(bridge.BridgeError, match="does not match this package"):
        bridge._skill_config()


def test_explicit_local_installation_policy_keeps_precedence(bridge, monkeypatch) -> None:
    local = _approval(bridge)
    local["approvalReference"] = "original-installation-approval"
    _write_approval(bridge, local)
    path = bridge.SKILL_ROOT / "config.json"
    config = json.loads(path.read_text())
    config["channel"] = "operator-channel"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", "invalid-environment-value")
    monkeypatch.setenv("IAC_CODE_SKILL_CHANNEL", "agenthub-e2e")
    actual = bridge._skill_config()
    assert actual["runtimeDownloadApproval"] == local
    assert actual["channel"] == "skill/operator-channel"


def test_explicit_null_local_policy_is_not_overridden_by_environment(bridge, monkeypatch) -> None:
    path = bridge.SKILL_ROOT / "config.json"
    path.write_text(json.dumps({"runtimeDownloadApproval": None, "channel": None}))
    monkeypatch.setenv("IAC_CODE_RUNTIME_DOWNLOAD_APPROVAL", json.dumps(_approval(bridge)))
    monkeypatch.setenv("IAC_CODE_SKILL_CHANNEL", "agenthub-e2e")
    assert bridge._skill_config()["channel"] is None
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._require_runtime_download_approval()
    assert caught.value.code == "runtime_download_approval_required"


def test_runtime_installation_denies_network_without_approval(bridge, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bridge, "detect_host", lambda: {"os": "darwin", "arch": "arm64", "nativeAbi": "macos"})
    monkeypatch.setattr(bridge.urllib.request, "build_opener", lambda *_: pytest.fail("unapproved network access"))
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.ensure_runtime()
    assert caught.value.code == "runtime_download_approval_required"
    assert caught.value.retryable is False
    assert caught.value.payload()["error"]["details"]["requiredApproval"]["host"] == bridge.RUNTIME_DOWNLOAD_HOST
    assert not bridge._bridge_root().exists()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("field,value", [
    ("host", "other.example"), ("runtimeTag", "v999.0.0"), ("manifestSha256", "f" * 64),
    ("approvedBy", ""), ("approvalReference", "  "), ("approvalReference", "record\nforged"),
])
def test_runtime_approval_rejects_wrong_scope_or_missing_audit_record(
    bridge, monkeypatch, tmp_path, field, value
) -> None:
    approval = _approval(bridge)
    approval[field] = value
    _write_approval(bridge, approval)
    monkeypatch.setattr(
        bridge.urllib.request, "build_opener", lambda *_: pytest.fail("invalid approval reached network")
    )
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._download(_download_root(bridge) + "runtime.zip", tmp_path / "runtime.part", 100)
    assert caught.value.code == "skill_configuration_invalid"


@pytest.mark.parametrize("approval", [True, {}, {"approvedBy": "operator"}])
def test_runtime_approval_requires_a_complete_scoped_object(bridge, approval) -> None:
    _write_approval(bridge, approval)
    with pytest.raises(bridge.BridgeError, match="exactly the pinned scope"):
        bridge._skill_config()


@pytest.mark.parametrize("expiry", ["2000-01-01T00:00:00Z", "2099-02-31T00:00:00Z", "2099-01-01", None])
def test_expired_or_invalid_approval_cannot_reach_network(bridge, monkeypatch, tmp_path, expiry) -> None:
    approval = _approval(bridge)
    approval["expiresAt"] = expiry
    _write_approval(bridge, approval)
    monkeypatch.setattr(bridge.urllib.request, "build_opener", lambda *_: pytest.fail("approval reached network"))
    with pytest.raises(bridge.BridgeError):
        bridge._download(_download_root(bridge) + "runtime.zip", tmp_path / "runtime.part", 100)


def test_approval_is_rechecked_at_redirect(bridge, monkeypatch) -> None:
    approval = _approval(bridge)
    _write_approval(bridge, approval)
    bridge._require_runtime_download_approval()
    approval["expiresAt"] = "2000-01-01T00:00:00Z"
    _write_approval(bridge, approval)
    request = urllib.request.Request(_download_root(bridge) + "first.zip")
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._RuntimeDownloadRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, _download_root(bridge) + "next.zip"
        )
    assert caught.value.code == "runtime_download_approval_required"


def test_private_state_rejects_workspace_and_installed_skill(bridge, monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for root in (workspace, workspace / "private", bridge.SKILL_ROOT / "private"):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(root))
        with pytest.raises(bridge.BridgeError, match="outside"):
            bridge._validate_private_state_location(workspace)
        assert not (root / "skill-runtime").exists()


def test_private_state_rejects_symlink_ancestors(bridge, monkeypatch, tmp_path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(link / "private"))
    with pytest.raises(bridge.BridgeError, match="symbolic"):
        bridge._config_root()


def test_explicit_runtime_cwd_allowlist_is_enforced(bridge, monkeypatch, tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))
    monkeypatch.setenv("IAC_CODE_A2A_TRUST_REQUEST_CWD", "1")
    assert bridge._runtime_cwd_policy() == {"allowedCwds": str(allowed), "trustRequestCwd": "0"}
    bridge._validate_runtime_workspace(allowed / "child")
    with pytest.raises(bridge.BridgeError) as caught:
        bridge._validate_runtime_workspace(tmp_path / "outside")
    assert caught.value.code == "runtime_workspace_denied"


def test_runtime_reuse_does_not_ignore_new_cwd_restriction(bridge, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("IACCODE_A2A_ALLOWED_CWDS", raising=False)
    monkeypatch.delenv("IAC_CODE_A2A_TRUST_REQUEST_CWD", raising=False)
    record = {
        "runtimeTag": bridge.RUNTIME_TAG,
        "iacCodeVersion": bridge.IAC_CODE_VERSION,
        "target": "test-target",
        "mode": "normal",
        "pipelineName": "",
        "handoffToolConfirmation": True,
        "permissionWaitPolicy": None,
        "pid": 123,
        "cwdPolicy": bridge._runtime_cwd_policy(),
    }
    monkeypatch.setattr(bridge, "_pid_alive", lambda *_: True)
    monkeypatch.setattr(bridge, "_http_json", lambda *_args, **_kwargs: pytest.fail("restricted runtime was reused"))
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(tmp_path))
    assert bridge._runtime_matches(record, "normal", "", "test-target") is False


def test_explicit_runtime_trust_deny_is_preserved(bridge, monkeypatch) -> None:
    monkeypatch.delenv("IACCODE_A2A_ALLOWED_CWDS", raising=False)
    monkeypatch.setenv("IAC_CODE_A2A_TRUST_REQUEST_CWD", "0")
    assert bridge._runtime_cwd_policy()["trustRequestCwd"] == "0"


def test_cache_private_state_cannot_be_inside_current_workspace(bridge, monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(workspace / "private"))
    with pytest.raises(bridge.BridgeError, match="outside"):
        bridge._bridge_root()
    assert not (workspace / "private").exists()


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
    _write_approval(bridge, _approval(bridge))
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
    _write_approval(bridge, _approval(bridge))
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
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr(profile.archive_root + "/.claude/settings.json", '{"enabledPlugins":{"fake-observer":true}}')
    with pytest.raises(ValueError, match="explicit publication profile"):
        package["verify_skill_archive"](staged, profile, output)
