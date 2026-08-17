from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/iac-code/scripts/iac_code.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("external_iac_code_skill_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


def _artifact(*, archive: Path, digest: str, size: int, archive_type: str = "tar.gz") -> dict[str, object]:
    return {
        "target": "darwin-arm64-macos-cp312",
        "os": "darwin",
        "arch": "arm64",
        "nativeAbi": "macos",
        "runtimePython": "cp312",
        "compatibility": {"minOsVersion": "12.0"},
        "url": archive.as_uri(),
        "sha256": digest,
        "size": size,
        "archive": archive_type,
        "executable": "iac-code-runtime/iac-code",
    }


def _manifest(artifact: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "kind": "iac-code-skill-runtime-release",
        "runtimeTag": bridge.RUNTIME_TAG,
        "iacCodeVersion": bridge.IAC_CODE_VERSION,
        "runtimePython": "cp312",
        "sourceCommit": "a" * 40,
        "publisherCommit": "b" * 40,
        "publishedAt": "2026-08-15T10:30:00Z",
        "artifacts": [artifact],
    }


def _configuration_readiness(*, llm_ready: bool = True, cloud_ready: bool = True) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "llm": {
            "ready": llm_ready,
            "source": "local",
            "provider": "openai",
            "providerDisplay": "OpenAI",
            "model": "gpt-5.6",
            "missing": [] if llm_ready else ["api_key"],
        },
        "cloud": {
            "ready": cloud_ready,
            "provider": "aliyun",
            "mode": "AK" if cloud_ready else None,
            "regionId": "cn-hangzhou" if cloud_ready else None,
            "missing": [] if cloud_ready else ["credentials"],
        },
    }


def test_bridge_parses_as_python_38_and_uses_only_standard_library_imports() -> None:
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    imported_modules = {
        alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_modules <= {
        "argparse",
        "contextlib",
        "ctypes",
        "errno",
        "fcntl",
        "hashlib",
        "json",
        "msvcrt",
        "os",
        "pathlib",
        "platform",
        "re",
        "secrets",
        "shutil",
        "socket",
        "stat",
        "subprocess",
        "sys",
        "tarfile",
        "tempfile",
        "time",
        "typing",
        "urllib",
        "uuid",
        "zipfile",
    }
    assert "pip install" not in source


def test_manifest_selects_exact_cp312_target_and_checks_numeric_compatibility() -> None:
    artifact = _artifact(archive=Path("/tmp/runtime.zip"), digest="a" * 64, size=10)
    manifest = bridge.validate_manifest(_manifest(artifact))
    selected = bridge.select_artifact(
        manifest,
        {"os": "darwin", "arch": "arm64", "nativeAbi": "macos", "osVersion": "12.10"},
    )
    assert selected["target"] == artifact["target"]
    with pytest.raises(bridge.BridgeError, match="below") as caught:
        bridge.select_artifact(
            manifest,
            {"os": "darwin", "arch": "arm64", "nativeAbi": "macos", "osVersion": "11.9"},
        )
    assert caught.value.code == "incompatible_host"
    invalid_identity = dict(artifact)
    invalid_identity["target"] = "darwin-arm64-macos-cp312-r2"
    with pytest.raises(bridge.BridgeError, match="target identity"):
        bridge.validate_manifest(_manifest(invalid_identity))


def test_safe_extract_rejects_traversal_and_duplicate_paths(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.safe_extract(traversal, "zip", tmp_path / "extract-traversal")
    assert caught.value.code == "artifact_verification_failed"

    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("Runtime/File", "one")
        archive.writestr("runtime/file", "two")
    with pytest.raises(bridge.BridgeError, match="duplicate"):
        bridge.safe_extract(duplicate, "zip", tmp_path / "extract-duplicate")


def test_tar_links_must_resolve_inside_archive_root() -> None:
    bridge._safe_tar_link("iac-code-runtime/lib/current", "../versioned/libiac.dylib")
    bridge._safe_tar_link("iac-code-runtime/lib/current", "iac-code-runtime/lib/libiac.dylib", hard_link=True)
    with pytest.raises(bridge.BridgeError, match="unsafe link"):
        bridge._safe_tar_link("iac-code-runtime/current", "../../outside")
    with pytest.raises(bridge.BridgeError, match="unsafe link"):
        bridge._safe_tar_link("iac-code-runtime/current", "/outside")


@pytest.mark.parametrize(
    ("wire_state", "expected_state", "expected_type"),
    [
        ("TASK_STATE_WORKING", "working", "status"),
        ("TASK_STATE_INPUT_REQUIRED", "input-required", "status"),
        ("TASK_STATE_COMPLETED", "completed", "terminal"),
    ],
)
def test_projection_normalizes_a2a_task_states(
    wire_state: str,
    expected_state: str,
    expected_type: str,
) -> None:
    projected = bridge.project_frame({"result": {"status": {"state": wire_state}}})
    assert projected["state"] == expected_state
    assert projected["type"] == expected_type


def test_ensure_runtime_downloads_verifies_atomically_and_hits_cache(monkeypatch, tmp_path: Path) -> None:
    archive = tmp_path / "runtime.zip"
    script = "#!/bin/sh\necho iac-code {}\n".format(bridge.IAC_CODE_VERSION)
    with zipfile.ZipFile(archive, "w") as bundle:
        executable = zipfile.ZipInfo("iac-code-runtime/iac-code")
        executable.external_attr = (stat.S_IFREG | 0o755) << 16
        bundle.writestr(executable, script)
        bundle.writestr(
            "iac-code-runtime/runtime-version.json",
            json.dumps(
                {
                    "iacCodeVersion": bridge.IAC_CODE_VERSION,
                    "runtimePython": "cp312",
                    "runtimeTag": bridge.RUNTIME_TAG,
                }
            ),
        )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    artifact = _artifact(archive=archive, digest=digest, size=archive.stat().st_size, archive_type="zip")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        bridge,
        "detect_host",
        lambda: {"os": "darwin", "arch": "arm64", "nativeAbi": "macos", "osVersion": "14.0"},
    )
    monkeypatch.setattr(bridge, "_fetch_manifest", lambda: _manifest(artifact))
    downloads: list[Path] = []

    def download(_url, destination, _maximum, expected_size=None):
        downloads.append(destination)
        shutil.copyfile(archive, destination)
        assert expected_size in {None, archive.stat().st_size}

    monkeypatch.setattr(bridge, "_download", download)
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="iac-code {}".format(bridge.IAC_CODE_VERSION).encode(),
            stderr=b"",
        ),
    )
    selected, executable, cache_hit = bridge.ensure_runtime()
    assert selected["target"] == artifact["target"]
    assert executable.is_file()
    assert cache_hit is False
    assert not list(executable.parents[2].glob(".install-*"))

    _selected, same_executable, cache_hit = bridge.ensure_runtime()
    assert same_executable == executable
    assert cache_hit is True
    assert len(downloads) == 1


def test_projection_drops_raw_tool_payload_redacts_secrets_and_preserves_paths() -> None:
    frame = {
        "result": {
            "taskId": "task-1",
            "contextId": "ctx-1",
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {"parts": [{"text": "running Authorization: Bearer secret-token at /Users/alice/project"}]},
            },
            "metadata": {
                "iac_code": {
                    "tool": {
                        "status": "started",
                        "name": "bash",
                        "toolUseId": "tool-1",
                        "input": {"password": "never-persist"},
                        "result": "never-persist",
                    }
                }
            },
        }
    }
    projected = bridge.project_frame(frame)
    rendered = json.dumps(projected)
    assert projected["milestones"][0]["toolName"] == "bash"
    assert "never-persist" not in rendered
    assert "secret-token" not in rendered
    assert "/Users/alice/project" in rendered


def test_projection_marks_authoritative_assistant_final_without_intermediate_text() -> None:
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {"parts": [{"text": "Deployment completed at /Users/alice/project."}]},
                },
                "metadata": {"iac_code": {"assistantFinal": {"complete": True}}},
            }
        }
    }
    projected = bridge.project_frame(frame)

    assert projected["type"] == "assistant-final"
    assert projected["finalText"] == "Deployment completed at /Users/alice/project."
    assert projected["finalTextComplete"] is True


def test_completed_pipeline_projects_deployment_result_and_normal_handoff(monkeypatch, tmp_path: Path) -> None:
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-pipeline-1",
                "contextId": "ctx-pipeline-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {
                    "iac_code": {
                        "pipelineBatch": {
                            "events": [
                                {
                                    "eventType": "step_completed",
                                    "status": "completed",
                                    "sequence": 20,
                                    "data": {
                                        "conclusionField": "deployment",
                                        "conclusion": {
                                            "status": "success",
                                            "stack_id": "stack-123",
                                            "resources_created": ["ALIYUN::ECS::Instance:web"],
                                            "outputs": {"PublicIp": "203.0.113.10"},
                                            "raw_tool_result": "must-not-leak",
                                        },
                                    },
                                },
                                {
                                    "eventType": "pipeline_handoff_ready",
                                    "status": "completed",
                                    "sequence": 21,
                                    "data": {"action": "switch_to_normal", "targetMode": "normal"},
                                },
                            ]
                        }
                    }
                },
            }
        }
    }
    pipeline_events = frame["result"]["statusUpdate"]["metadata"]["iac_code"]["pipelineBatch"]["events"]
    frame["result"]["statusUpdate"]["metadata"]["iac_code"]["pipelineBatch"]["events"] = [
        {
            "eventType": "tool_started",
            "status": "working",
            "sequence": index,
            "data": {"toolName": "aliyun_api"},
        }
        for index in range(20)
    ] + pipeline_events

    projected = bridge.project_frame(frame)

    assert projected["pipelineResultField"] == "deployment"
    assert projected["pipelineResult"] == {
        "status": "success",
        "stack_id": "stack-123",
        "resources_created": ["ALIYUN::ECS::Instance:web"],
        "outputs": {"PublicIp": "203.0.113.10"},
    }
    assert projected["normalHandoffReady"] is True
    assert "must-not-leak" not in json.dumps(projected)

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "c" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "pipeline",
            "conversationMode": "pipeline",
            "pipelineName": "selling",
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-pipeline-1",
            "contextId": "ctx-pipeline-1",
            "turnArtifacts": [],
        },
    )
    bridge._append_projection(job_id, projected)
    bridge._append_projection(
        job_id,
        {
            "type": "terminal",
            "state": "completed",
            "taskId": "task-pipeline-1",
            "contextId": "ctx-pipeline-1",
        },
    )

    result = bridge._job_result(job_id, 0, bridge.MAX_POLL_BYTES, preserve_final=False)
    assert result["state"] == "completed"
    assert result["conversationMode"] == "normal"
    assert result["pipelineResult"] == projected["pipelineResult"]
    assert "latestText" not in result
    assert len(bridge._json_bytes(result)) <= bridge.MAX_POLL_BYTES


def test_pipeline_handoff_projects_bounded_pending_cleanup() -> None:
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-pipeline-1",
                "contextId": "ctx-pipeline-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "metadata": {
                    "iac_code": {
                        "pipelineBatch": {
                            "events": [
                                {
                                    "eventType": "pipeline_handoff_ready",
                                    "status": "completed",
                                    "sequence": 21,
                                    "data": {
                                        "action": "switch_to_normal",
                                        "targetMode": "normal",
                                        "cleanup": {
                                            "status": "pending",
                                            "resourceCount": 1,
                                            "statusMessage": "Detected rollback cleanup resources.",
                                            "resources": [
                                                {
                                                    "provider": "ros",
                                                    "resourceType": "stack",
                                                    "resourceId": "stack-123",
                                                    "regionId": "cn-hangzhou",
                                                    "cleanupStatus": "pending",
                                                    "secret": "must-not-leak",
                                                }
                                            ],
                                            "prompt": "private cleanup prompt",
                                            "ledgerPath": "/private/cleanup.yaml",
                                        },
                                    },
                                }
                            ]
                        }
                    }
                },
            }
        }
    }

    projected = bridge.project_frame(frame)

    assert projected["normalHandoffReady"] is True
    assert projected["cleanup"] == {
        "status": "pending",
        "resourceCount": 1,
        "statusMessage": "Detected rollback cleanup resources.",
        "resources": [
            {
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "stack-123",
                "regionId": "cn-hangzhou",
                "cleanupStatus": "pending",
            }
        ],
    }
    rendered = json.dumps(projected)
    assert "must-not-leak" not in rendered
    assert "private cleanup prompt" not in rendered
    assert "ledgerPath" not in rendered


def test_bridge_automatically_runs_cleanup_only_task_and_restores_pipeline_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "e" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "pipeline",
            "conversationMode": "normal",
            "pipelineName": "selling",
            "workspace": str(workspace),
            "state": "completed",
            "turn": 1,
            "taskId": "task-pipeline-1",
            "contextId": "ctx-pipeline-1",
            "turnArtifacts": [{"id": "template", "name": "template.yaml"}],
            "pipelineResult": {"status": "success", "stack_id": "stack-123"},
            "cleanup": {"status": "pending", "resourceCount": 1},
        },
    )
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda job: {"port": 1234, "token": "token"})
    captured_payloads = []

    def spawn_worker(_job_id, payload):
        captured_payloads.append(payload)
        return 4321

    monkeypatch.setattr(bridge, "_spawn_worker", spawn_worker)
    monkeypatch.setattr(
        bridge,
        "_wait_for_task_identity",
        lambda _job_id, _previous_task_id, cursor, worker_pid: {
            "ok": True,
            "jobId": job_id,
            "state": "working",
            "cursor": cursor,
            "taskId": "task-cleanup-{}".format(len(captured_payloads)),
            "contextId": "ctx-pipeline-1",
            "workerPid": worker_pid,
        },
    )

    cleanup_passes = 0

    def finish_cleanup(_args):
        nonlocal cleanup_passes
        cleanup_passes += 1
        current = bridge._load_json(job_path)
        current["taskId"] = "task-cleanup-{}".format(cleanup_passes)
        current["state"] = bridge.TURN_COMPLETED_STATE
        current["cleanup"] = {
            "status": "pending" if cleanup_passes == 1 else "completed",
            "resourceCount": 1 if cleanup_passes == 1 else 0,
        }
        current["finalText"] = "internal cleanup response"
        current["finalTextComplete"] = True
        bridge._atomic_json(job_path, current)
        return bridge._job_result(job_id, 0, bridge.MAX_FOLLOW_BYTES, preserve_final=True)

    monkeypatch.setattr(bridge, "_follow_job_once", finish_cleanup)
    result = bridge._advance_pipeline_cleanup(
        argparse.Namespace(job_id=job_id, cursor=0, wait_seconds=60),
        bridge._job_result(job_id, 0, bridge.MAX_FOLLOW_BYTES, preserve_final=True),
    )

    assert len(captured_payloads) == 2
    assert all(
        payload["params"]["message"]["metadata"]["iac_code"]["cleanupOnly"] is True
        for payload in captured_payloads
    )
    assert captured_payloads[0]["params"]["message"]["contextId"] == "ctx-pipeline-1"
    assert result["state"] == "completed"
    assert result["cleanup"] == {"status": "completed", "resourceCount": 0}
    assert result["pipelineResult"] == {"status": "success", "stack_id": "stack-123"}
    assert result["artifacts"] == [{"id": "template", "name": "template.yaml"}]
    assert "finalText" not in result
    saved = bridge._load_json(job_path)
    assert saved["pipelineTerminalState"] == "completed"
    assert saved["taskHistory"] == ["task-pipeline-1", "task-cleanup-1"]
    assert saved["cleanupAttempts"] == 2


def test_failed_cleanup_is_reported_without_automatic_permission_retry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "f" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "pipeline",
            "conversationMode": "normal",
            "pipelineName": "selling",
            "workspace": str(workspace),
            "state": bridge.TURN_COMPLETED_STATE,
            "turn": 1,
            "taskId": "task-cleanup-1",
            "contextId": "ctx-pipeline-1",
            "turnArtifacts": [],
            "pipelineArtifacts": [{"id": "template", "name": "template.yaml"}],
            "pipelineTerminalState": "completed",
            "pipelineResult": {"status": "success", "stack_id": "stack-123"},
            "cleanup": {"status": "failed", "resourceCount": 1},
            "cleanupOnlyActive": True,
            "finalText": "permission denied",
        },
    )
    monkeypatch.setattr(
        bridge,
        "_start_cleanup_only_task",
        lambda _job_id: pytest.fail("failed cleanup must not be retried automatically"),
    )

    result = bridge._advance_pipeline_cleanup(
        argparse.Namespace(job_id=job_id, cursor=0, wait_seconds=60),
        bridge._job_result(job_id, 0, bridge.MAX_FOLLOW_BYTES, preserve_final=True),
    )

    assert result["state"] == "completed"
    assert result["cleanup"] == {"status": "failed", "resourceCount": 1}
    assert result["pipelineResult"] == {"status": "success", "stack_id": "stack-123"}
    assert "finalText" not in result


def test_project_frame_unwraps_a2a_v1_status_update() -> None:
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
                "metadata": {
                    "iac_code": {
                        "input": {
                            "schemaVersion": 1,
                            "kind": "permission",
                            "requestTaskId": "task-1",
                            "contextId": "ctx-1",
                            "inputId": "permission-task-1-tool-1",
                            "toolUseId": "tool-1",
                            "toolName": "bash",
                            "prompt": "Allow?",
                            "safeSummary": "bash: pwd",
                            "options": [
                                {"id": "allow_once", "label": "Allow once"},
                                {"id": "deny", "label": "Deny"},
                            ],
                            "required": True,
                        }
                    }
                },
            }
        }
    }
    projected = bridge.project_frame(frame)
    assert projected["type"] == "input-required"
    assert projected["taskId"] == "task-1"
    assert projected["contextId"] == "ctx-1"
    assert projected["state"] == "input-required"
    assert projected["inputRequired"]["toolUseId"] == "tool-1"


def test_working_permission_is_a_sideband_boundary_and_recovers_from_task_metadata(monkeypatch, tmp_path: Path) -> None:
    pending = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-opaque",
        "toolUseId": "tool-1",
        "toolName": "bash",
        "prompt": "Allow?",
        "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "required": True,
    }
    frame = {
        "result": {
            "task": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {"iac_code": {"pendingPermissions": [pending]}},
            }
        }
    }
    projected = bridge.project_frame(frame)
    assert projected["type"] == "permission-requested"
    assert projected["state"] == "working"

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "1" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"state": "working", "workspace": str(tmp_path), "turn": 1})
    bridge._append_projection(job_id, projected)
    job = bridge._load_json(job_path)
    assert job["state"] == "working"
    assert job["inputRequired"]["inputId"] == "permission-opaque"
    assert job["pendingPermissions"] == [job["inputRequired"]]


def test_workspace_artifact_uses_accessible_file_uri_and_keeps_a2a_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "template.yaml"
    source.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    job_id = "b" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "turnArtifacts": [],
        },
    )
    frame = {
        "result": {
            "artifactUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "artifact": {
                    "artifactId": "artifact-1",
                    "name": "template.yaml",
                    "parts": [{"url": "iac-code-artifact://artifact-1/template.yaml"}],
                    "metadata": {
                        "mediaType": "application/yaml",
                        "byteSize": source.stat().st_size,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "sourcePath": str(source),
                    },
                },
            }
        }
    }

    bridge._append_projection(job_id, bridge.project_frame(frame))

    [artifact] = bridge._load_json(job_path)["turnArtifacts"]
    assert artifact["uri"] == source.resolve().as_uri()
    assert artifact["a2aUri"] == "iac-code-artifact://artifact-1/template.yaml"
    assert "sourcePath" not in artifact


def test_permission_worker_payload_is_single_json_part_bound_to_pending_task() -> None:
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
    }
    payload = bridge._worker_payload(
        {"workspace": "/tmp/work", "inputRequired": pending},
        response={
            "kind": "permission",
            "requestTaskId": "task-1",
            "contextId": "ctx-1",
            "inputId": "permission-task-1-tool-1",
            "toolUseId": "tool-1",
            "decision": "allow_once",
        },
    )
    message = payload["params"]["message"]
    assert payload["method"] == "SendStreamingMessage"
    assert message["taskId"] == "task-1"
    assert message["contextId"] == "ctx-1"
    assert len(message["parts"]) == 1
    assert message["parts"][0]["mediaType"] == "application/json"
    assert message["parts"][0]["data"] == {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }


def test_sideband_permission_payload_uses_short_send_message() -> None:
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-opaque",
        "toolUseId": "tool-1",
    }
    payload = bridge._worker_payload(
        {"workspace": "/tmp/work", "inputRequired": pending, "pendingPermissions": [pending]},
        response={**pending, "decision": "allow_once"},
    )
    assert payload["method"] == "SendMessage"


def test_worker_subscribes_after_follow_up_stream_while_task_is_working(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "b" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"taskId": "task-1", "state": "working"})
    monkeypatch.setattr(
        bridge,
        "_http_json",
        lambda *_args, **_kwargs: {
            "result": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
            }
        },
    )
    payload = bridge._subscription_after_stream({"port": 1, "token": "token"}, job_id)
    assert payload["method"] == "SubscribeToTask"
    assert payload["params"] == {"id": "task-1"}


def test_worker_stops_for_normal_turn_input_required_without_envelope(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "c" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"taskId": "task-1", "state": "working"})
    monkeypatch.setattr(
        bridge,
        "_http_json",
        lambda *_args, **_kwargs: {
            "result": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
            }
        },
    )
    assert bridge._subscription_after_stream({"port": 1, "token": "token"}, job_id) is None


def test_job_runtime_identity_rejects_a_replaced_generation(monkeypatch, tmp_path: Path) -> None:
    record_path = tmp_path / "runtime.json"
    bridge._atomic_json(
        record_path,
        {
            "generation": "new-generation",
            "mode": "normal",
            "pipelineName": "",
            "workspace": str(tmp_path),
            "target": "darwin-arm64-macos-cp312",
        },
    )
    job = {
        "runtimeRecord": str(record_path),
        "runtimeGeneration": "old-generation",
        "mode": "normal",
        "pipelineName": "",
        "workspace": str(tmp_path),
        "target": "darwin-arm64-macos-cp312",
    }
    monkeypatch.setattr(bridge, "_runtime_matches", lambda *_args: True)
    with pytest.raises(bridge.BridgeError, match="generation") as caught:
        bridge._runtime_record_for_job(job)
    assert caught.value.code == "runtime_identity_mismatch"


def test_poll_output_is_bounded_and_cursor_is_incremental(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "a" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"state": "working", "createdAt": int(bridge.time.time())})
    spool.write_text(
        "".join(json.dumps({"type": "text", "text": "x" * 800, "state": "working"}) + "\n" for _ in range(30)),
        encoding="utf-8",
    )
    result = bridge.poll_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert result["cursor"] == 30
    assert len(bridge._json_bytes(result)) <= bridge.MAX_POLL_BYTES
    assert bridge.poll_job(SimpleNamespace(job_id=job_id, cursor=30, wait_seconds=0))["cursor"] == 30


@pytest.mark.parametrize("requested, expected", [(-1, 0), (60, 60), (120, 120), (300, 120), (float("nan"), 0)])
def test_follow_wait_is_bounded_to_120_seconds(requested: float, expected: float) -> None:
    assert bridge._bounded_follow_seconds(requested) == expected


def test_follow_job_uses_one_120_second_deadline_across_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "4" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(job_path, {"jobId": job_id, "state": "working"})
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 10.0)
    captured = {}

    def follow_once(args):
        captured["streamDeadline"] = args.follow_deadline
        return {"ok": True, "jobId": job_id, "state": "working", "cursor": 0}

    def advance(args, result):
        captured["cleanupDeadline"] = args.follow_deadline
        return result

    monkeypatch.setattr(bridge, "_follow_job_once", follow_once)
    monkeypatch.setattr(bridge, "_advance_pipeline_cleanup", advance)

    bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=300))

    assert captured == {"streamDeadline": 130.0, "cleanupDeadline": 130.0}


def test_failed_pipeline_returns_authoritative_result_when_cleanup_is_not_pending(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "3" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    base_job = {
        "jobId": job_id,
        "mode": "pipeline",
        "conversationMode": "pipeline",
        "pipelineName": "selling",
        "state": "failed",
        "turn": 1,
        "taskId": "task-pipeline-1",
        "contextId": "ctx-pipeline-1",
        "preferredLanguage": "zh",
        "turnArtifacts": [],
        "pipelineResult": {"status": "failed", "error": "地域库存不足"},
    }
    bridge._atomic_json(job_path, base_job)

    result = bridge._job_result(job_id, 0, bridge.MAX_POLL_BYTES, preserve_final=False)
    assert result["state"] == "failed"
    assert result["pipelineResult"] == base_job["pipelineResult"]
    assert result["preferredLanguage"] == "zh"

    pending_job = {**base_job, "cleanup": {"status": "pending", "resourceCount": 1}}
    bridge._atomic_json(job_path, pending_job)
    pending = bridge._job_result(job_id, 0, bridge.MAX_POLL_BYTES, preserve_final=False)
    assert "pipelineResult" not in pending

    failed_cleanup_job = {**base_job, "cleanup": {"status": "failed", "resourceCount": 1}}
    bridge._atomic_json(job_path, failed_cleanup_job)
    failed_cleanup = bridge._job_result(job_id, 0, bridge.MAX_POLL_BYTES, preserve_final=False)
    assert failed_cleanup["pipelineResult"] == base_job["pipelineResult"]


def test_poll_repeats_current_input_envelope_after_its_event_cursor(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "d" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
    }
    bridge._atomic_json(job_path, {"state": "input-required", "inputRequired": pending})
    spool.write_text(json.dumps({"type": "input-required", "inputRequired": pending}) + "\n", encoding="utf-8")
    result = bridge.poll_job(SimpleNamespace(job_id=job_id, cursor=1, wait_seconds=0))
    assert result["inputRequired"] == pending


def test_large_input_required_remains_answerable_and_bounded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    options = [
        {
            "id": str(index),
            "label": "candidate " + ("x" * 200),
            "summary": "summary " + ("s" * 600),
            "architectureDiagram": "flowchart LR\n" + ("A --> B\n" * 100),
            "totalMonthlyCost": "¥88/月",
            "costItems": [{"name": "ECS", "spec": "2核4G", "monthlyCost": "¥88/月"}] * 12,
        }
        for index in range(20)
    ]
    frame = {
        "result": {
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "metadata": {
                    "iac_code": {
                        "input": {
                            "schemaVersion": 1,
                            "kind": "candidate_selection",
                            "requestTaskId": "task-1",
                            "contextId": "ctx-1",
                            "inputId": "candidate-1",
                            "prompt": "Choose one " + ("y" * 600),
                            "options": options,
                            "required": True,
                        }
                    }
                },
            },
        }
    }
    projection = bridge.project_frame(frame)
    assert projection["type"] == "input-required"
    assert len(projection["inputRequired"]["options"]) == 20
    assert len(bridge._json_bytes(projection)) <= bridge.MAX_INPUT_PROJECTION_BYTES

    job_id = "f" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"state": "working", "createdAt": int(bridge.time.time())})
    bridge._append_projection(job_id, projection)
    result = bridge.poll_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert result["inputRequired"]["kind"] == "candidate_selection"
    assert len(result["inputRequired"]["options"]) == 20
    assert len(bridge._json_bytes(result)) <= bridge.MAX_POLL_BYTES


def test_candidate_presentation_survives_bounded_bridge_projection() -> None:
    frame = {
        "result": {
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "metadata": {
                    "iac_code": {
                        "input": {
                            "schemaVersion": 1,
                            "kind": "candidate_selection",
                            "requestTaskId": "task-1",
                            "contextId": "ctx-1",
                            "inputId": "candidate-rich",
                            "prompt": "请选择方案",
                            "options": [
                                {
                                    "id": "0",
                                    "label": "方案 A",
                                    "summary": "单 ECS 低成本方案。",
                                    "architectureDiagram": "flowchart LR\nU[用户] --> E[ECS]",
                                    "totalMonthlyCost": "¥88/月",
                                    "costItems": [
                                        {"name": "ECS", "spec": "2核4G", "monthlyCost": "¥88/月"}
                                    ],
                                }
                            ],
                            "required": True,
                        }
                    }
                },
            },
        }
    }

    projection = bridge.project_frame(frame)

    [option] = projection["inputRequired"]["options"]
    assert option["summary"] == "单 ECS 低成本方案。"
    assert option["architectureDiagram"] == "flowchart LR\nU[用户] --> E[ECS]"
    assert option["totalMonthlyCost"] == "¥88/月"
    assert option["costItems"] == [{"name": "ECS", "spec": "2核4G", "monthlyCost": "¥88/月"}]
    assert len(bridge._json_bytes(projection)) <= bridge.MAX_INPUT_PROJECTION_BYTES


def test_bridge_detects_language_and_sends_it_in_a2a_metadata() -> None:
    assert bridge._preferred_language("请部署一个 VPC", "auto") == "zh"
    assert bridge._preferred_language("日本語で説明してください", "auto") == "ja"
    assert bridge._preferred_language("Deploy a VPC", "fr") == "fr"

    payload = bridge._worker_payload(
        {"workspace": "/tmp/work", "preferredLanguage": "zh"},
        prompt="请部署一个 VPC",
    )
    assert payload["params"]["message"]["metadata"]["iac_code"]["preferredLanguage"] == "zh"
    assert payload["params"]["message"]["metadata"]["iac_code"]["candidatePresentation"] == "rich-v1"


def test_job_results_repeat_preferred_language(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "8" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    job = {
        "jobId": job_id,
        "state": "working",
        "turn": 1,
        "taskId": "task-1",
        "contextId": "ctx-1",
        "preferredLanguage": "zh",
        "turnArtifacts": [],
    }
    bridge._atomic_json(job_path, job)

    assert bridge._identity_result(job_id, job, 0, 123)["preferredLanguage"] == "zh"
    result = bridge._job_result(job_id, 0, bridge.MAX_POLL_BYTES, preserve_final=False)
    assert result["preferredLanguage"] == "zh"
    try:
        bridge._set_output_language("zh")
        assert bridge.BridgeError("test", "failed").payload()["preferredLanguage"] == "zh"
    finally:
        bridge._set_output_language("en")


def test_bridge_localizes_user_visible_progress_for_chinese(capsys) -> None:
    try:
        bridge._set_output_language("zh")
        bridge._progress("start", "Starting or reusing the local A2A runtime")
        signature, message = bridge._follow_progress_message(
            {"milestones": [{"eventType": "tool_started", "toolName": "aliyun_api"}]}
        )
    finally:
        bridge._set_output_language("en")

    assert "正在启动或复用本地 A2A Runtime" in capsys.readouterr().err
    assert signature == "tool_started"
    assert message == "工具已开始：aliyun_api"


def test_runtime_readiness_blocks_missing_llm_and_required_cloud_credentials(monkeypatch) -> None:
    response = _configuration_readiness(llm_ready=False, cloud_ready=False)
    monkeypatch.setattr(bridge, "_http_json", lambda *_args, **_kwargs: response)
    try:
        bridge._set_output_language("zh")
        with pytest.raises(bridge.BridgeError) as llm_error:
            bridge._runtime_configuration_readiness({"port": 41242, "token": "token"}, require_cloud=False)
    finally:
        bridge._set_output_language("en")

    assert llm_error.value.code == "llm_not_configured"
    assert "LLM 配置不完整" in llm_error.value.message
    assert llm_error.value.details["configurationReadiness"]["llm"]["missing"] == ["api_key"]

    response["llm"]["ready"] = True
    response["llm"]["missing"] = []
    with pytest.raises(bridge.BridgeError) as cloud_error:
        bridge._runtime_configuration_readiness({"port": 41242, "token": "token"}, require_cloud=True)

    assert cloud_error.value.code == "cloud_credentials_not_configured"
    assert cloud_error.value.details["configurationReadiness"]["cloud"]["requiredForStart"] is True


def test_runtime_readiness_warns_but_allows_normal_without_cloud_credentials(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bridge,
        "_http_json",
        lambda *_args, **_kwargs: _configuration_readiness(cloud_ready=False),
    )
    try:
        bridge._set_output_language("zh")
        result = bridge._runtime_configuration_readiness(
            {"port": 41242, "token": "token"},
            require_cloud=False,
        )
    finally:
        bridge._set_output_language("en")

    assert result["llm"]["requiredForStart"] is True
    assert result["cloud"]["requiredForStart"] is False
    assert result["cloud"]["ready"] is False
    assert "当前仅适合不调用云 API 的模板任务" in capsys.readouterr().err


def test_start_checks_pipeline_readiness_before_creating_a_job(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = workspace / "prompt.txt"
    prompt.write_text("请设计并部署一个方案", encoding="utf-8")
    monkeypatch.setattr(
        bridge,
        "ensure_runtime",
        lambda: ({"target": "darwin-arm64-macos-cp312"}, tmp_path / "iac-code", True),
    )
    monkeypatch.setattr(
        bridge,
        "ensure_server",
        lambda *_args: {"port": 41242, "token": "token", "generation": "generation-1"},
    )
    observed = {}

    def fail_readiness(_record, require_cloud):
        observed["requireCloud"] = require_cloud
        raise bridge.BridgeError("cloud_credentials_not_configured", "missing cloud credentials")

    monkeypatch.setattr(bridge, "_runtime_configuration_readiness", fail_readiness)
    monkeypatch.setattr(bridge, "_spawn_worker", lambda *_args: pytest.fail("worker must not start"))

    try:
        with pytest.raises(bridge.BridgeError) as error:
            bridge.start_job(
                SimpleNamespace(
                    cwd=str(workspace),
                    prompt_file=str(prompt),
                    language="auto",
                    mode="pipeline",
                    pipeline_name="selling",
                    follow=False,
                    follow_seconds=0,
                )
            )
    finally:
        bridge._set_output_language("en")

    assert error.value.code == "cloud_credentials_not_configured"
    assert observed == {"requireCloud": True}


def test_follow_surfaces_every_parent_and_candidate_step_boundary() -> None:
    try:
        bridge._set_output_language("zh")
        messages = bridge._follow_progress_messages(
            {
                "milestones": [
                    {
                        "eventType": "step_started",
                        "sequence": 1,
                        "step": {"id": "intent_parsing", "index": 1, "total": 5},
                    },
                    {
                        "eventType": "step_completed",
                        "sequence": 2,
                        "step": {"id": "intent_parsing", "index": 1, "total": 5},
                    },
                    {
                        "eventType": "candidate_step_started",
                        "sequence": 3,
                        "candidate": {"id": "candidate-1", "name": "低成本方案"},
                        "candidateStep": {"id": "template_generating", "index": 1, "total": 3},
                    },
                    {
                        "eventType": "candidate_step_completed",
                        "sequence": 4,
                        "candidate": {"id": "candidate-1", "name": "低成本方案"},
                        "candidateStep": {"id": "template_generating", "index": 1, "total": 3},
                    },
                    {"eventType": "tool_started", "toolName": "ros_validate_template"},
                    {"eventType": "tool_result", "toolName": "ros_validate_template"},
                ]
            }
        )
    finally:
        bridge._set_output_language("en")

    assert [message for _signature, message in messages] == [
        "步骤开始：1/5 理解部署需求",
        "步骤完成：1/5 理解部署需求",
        "候选步骤开始：低成本方案 · 1/3 生成 IaC 模板",
        "候选步骤完成：低成本方案 · 1/3 生成 IaC 模板",
        "工具已返回结果：ros_validate_template",
    ]
    assert len({signature for signature, _message in messages}) == len(messages)


def test_projection_keeps_step_boundaries_and_coordinates_ahead_of_recent_tools() -> None:
    events = [
        {
            "eventType": "step_started",
            "status": "working",
            "sequence": 1,
            "step": {"id": "intent_parsing", "index": 1, "total": 5},
            "data": {},
        },
        {
            "eventType": "candidate_step_completed",
            "status": "working",
            "sequence": 2,
            "step": {"id": "evaluate_candidates", "index": 3, "total": 5},
            "candidate": {"id": "candidate-1", "name": "低成本方案", "index": 0},
            "candidateStep": {"id": "cost_estimating", "index": 3, "total": 3},
            "data": {},
        },
    ]
    events.extend(
        {
            "eventType": "tool_started",
            "status": "working",
            "sequence": index,
            "data": {"toolName": "aliyun_api"},
        }
        for index in range(3, 20)
    )
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {"iac_code": {"pipelineBatch": {"events": events}}},
            }
        }
    }

    projection = bridge.project_frame(frame)

    by_type = {milestone["eventType"]: milestone for milestone in projection["milestones"]}
    assert by_type["step_started"]["step"] == {"id": "intent_parsing", "index": 1, "total": 5}
    assert by_type["candidate_step_completed"]["candidateStep"] == {
        "id": "cost_estimating",
        "index": 3,
        "total": 3,
    }
    assert len(bridge._json_bytes(projection)) + 1 <= bridge.MAX_PROJECTION_BYTES


def test_projection_keeps_bounded_step_one_and_two_conclusion_summaries() -> None:
    events = [
        {
            "eventType": "step_completed",
            "status": "working",
            "sequence": 1,
            "step": {"id": "intent_parsing", "index": 1, "total": 5},
            "data": {
                "conclusionField": "intent",
                "conclusion": {
                    "user_message_summary": "在杭州部署一个高可用 Web 服务",
                    "cloud_platform": "Alibaba Cloud",
                    "business_type": "Web 应用",
                    "non_functional": {"region_preference": "cn-hangzhou"},
                    "resource_intents": [
                        {"product": "ECS", "action": "create", "role": "应用服务器"},
                        {"product": "RDS", "action": "create", "role": "业务数据库"},
                    ],
                    "hard_constraints": [
                        {"target": "ECS", "property": "count", "operator": "gte", "value": 2},
                        {"target": "RDS", "property": "password", "operator": "eq", "value": "secret"},
                    ],
                },
            },
        },
        {
            "eventType": "step_completed",
            "status": "working",
            "sequence": 2,
            "step": {"id": "architecture_planning", "index": 2, "total": 5},
            "data": {
                "conclusionField": "architecture",
                "conclusion": {
                    "candidates": [
                        {
                            "name": "均衡高可用方案",
                            "products": ["ALB", "ECS", "RDS"],
                            "topology": "ALB 连接两台 ECS，并使用高可用 RDS",
                            "monthly_estimate": "约 1200 元/月",
                            "pros": ["可用性高"],
                            "cons": ["成本较高"],
                        },
                        {
                            "name": "经济方案",
                            "products": ["ECS", "RDS"],
                            "topology": "单台 ECS 连接基础版 RDS",
                            "monthly_estimate": "约 500 元/月",
                            "pros": ["成本较低"],
                            "cons": ["可用性较低"],
                        },
                    ]
                },
            },
        },
    ]
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "metadata": {"iac_code": {"pipelineBatch": {"events": events}}},
            }
        }
    }

    projection = bridge.project_frame(frame)

    by_step = {milestone["step"]["id"]: milestone for milestone in projection["milestones"]}
    intent = by_step["intent_parsing"]["conclusionSummary"]
    assert intent["requirementSummary"] == "在杭州部署一个高可用 Web 服务"
    assert intent["region"] == "cn-hangzhou"
    assert intent["resources"][0] == {"product": "ECS", "action": "create", "role": "应用服务器"}
    assert intent["hardConstraints"][0]["value"] == 2
    assert "value" not in intent["hardConstraints"][1]
    architecture = by_step["architecture_planning"]["conclusionSummary"]
    assert architecture["candidateCount"] == 2
    assert architecture["candidates"][0]["monthlyEstimate"] == "约 1200 元/月"
    assert len(bridge._json_bytes(intent)) <= bridge.MAX_STEP_CONCLUSION_SUMMARY_BYTES
    assert len(bridge._json_bytes(architecture)) <= bridge.MAX_STEP_CONCLUSION_SUMMARY_BYTES
    assert len(bridge._json_bytes(projection)) + 1 <= bridge.MAX_PROJECTION_BYTES


def test_follow_formats_step_conclusion_summary_for_the_user() -> None:
    item = {
        "milestones": [
            {
                "eventType": "step_completed",
                "sequence": 1,
                "step": {"id": "intent_parsing", "index": 1, "total": 5},
                "conclusionSummary": {
                    "requirementSummary": "部署高可用 Web 服务",
                    "region": "cn-hangzhou",
                    "resources": [
                        {"product": "ECS", "action": "create"},
                        {"product": "RDS", "action": "create"},
                    ],
                },
            },
            {
                "eventType": "step_completed",
                "sequence": 2,
                "step": {"id": "architecture_planning", "index": 2, "total": 5},
                "conclusionSummary": {
                    "candidateCount": 2,
                    "candidates": [
                        {"name": "高可用方案", "monthlyEstimate": "约 1200 元/月"},
                        {"name": "经济方案", "monthlyEstimate": "约 500 元/月"},
                    ],
                },
            },
        ]
    }
    try:
        bridge._set_output_language("zh")
        messages = bridge._follow_progress_messages(item)
        user_updates = bridge._step_boundary_user_updates(item["milestones"])
    finally:
        bridge._set_output_language("en")

    expected = [
        "步骤完成：1/5 理解部署需求；结论：部署高可用 Web 服务；地域 cn-hangzhou；资源 ECS (新建)、RDS (新建)",
        "步骤完成：2/5 设计候选架构；结论：2 个候选方案 高可用方案 (约 1200 元/月)、经济方案 (约 500 元/月)",
    ]
    assert [message for _signature, message in messages] == expected
    assert user_updates == expected


def test_ask_question_free_text_contract_survives_spool_and_poll(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    frame = {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
                "metadata": {
                    "iac_code": {
                        "input": {
                            "schemaVersion": 1,
                            "kind": "ask_user_question",
                            "requestTaskId": "task-1",
                            "contextId": "ctx-1",
                            "inputId": "question-1",
                            "prompt": "Choose or describe",
                            "allowFreeText": True,
                            "freeTextPrompt": "Describe the custom region",
                            "options": [{"id": "cn-hangzhou", "label": "Hangzhou"}],
                            "required": True,
                        }
                    }
                },
            }
        }
    }
    projection = bridge.project_frame(frame)
    job_id = "1" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {"state": "working", "createdAt": int(bridge.time.time())})
    bridge._append_projection(job_id, projection)
    result = bridge.poll_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert result["inputRequired"]["allowFreeText"] is True
    assert result["inputRequired"]["freeTextPrompt"] == "Describe the custom region"
    assert len(bridge._json_bytes(result)) <= bridge.MAX_POLL_BYTES


def test_respond_rejects_wrong_kind_and_normal_turn_without_envelope(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps({"kind": "ask_user_question", "answer": "yes"}), encoding="utf-8")
    job_id = "e" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    pending = {"kind": "permission", "inputId": "input-1", "toolUseId": "tool-1", "requestTaskId": "task-1"}
    bridge._atomic_json(job_path, {"state": "input-required", "inputRequired": pending})
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {"port": 1, "token": "token"})
    with pytest.raises(bridge.BridgeError, match="kind"):
        bridge.respond_job(SimpleNamespace(job_id=job_id, input_file=str(response_path)))

    bridge._atomic_json(job_path, {"state": "input-required"})
    with pytest.raises(bridge.BridgeError, match="not waiting"):
        bridge.respond_job(SimpleNamespace(job_id=job_id, input_file=str(response_path)))


def test_inline_permission_response_is_one_command_and_remains_correlated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "c" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
    }
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "inputRequired": pending,
        },
    )
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})
    captured = {}

    def spawn(_job_id, payload):
        captured["payload"] = payload
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)

    result = bridge.respond_job(
        SimpleNamespace(
            job_id=job_id,
            input_file=None,
            input_id=pending["inputId"],
            tool_use_id=pending["toolUseId"],
            decision="allow_once",
            follow=False,
        )
    )

    response = captured["payload"]["params"]["message"]["parts"][0]["data"]
    assert response == {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "inputId": pending["inputId"],
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    assert result["state"] == "working"

    bridge._atomic_json(job_path, {**bridge._load_json(job_path), "inputRequired": pending, "state": "input-required"})
    with pytest.raises(bridge.BridgeError, match="correlation fields"):
        bridge.respond_job(
            SimpleNamespace(
                job_id=job_id,
                input_file=None,
                input_id=pending["inputId"],
                tool_use_id="stale-tool",
                decision="allow_once",
                follow=False,
            )
        )


def test_sideband_permission_response_uses_unary_ack_and_keeps_background_worker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "d" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-opaque",
        "toolUseId": "tool-1",
    }
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "inputRequired": pending,
            "pendingPermissions": [pending],
            "workerPid": 4321,
        },
    )
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {"port": 1, "token": "token"})
    captured = {}

    def send(_url, _token, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {
            "result": {
                "message": {
                    "messageId": "permission-ack-1",
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "role": "ROLE_AGENT",
                    "parts": [
                        {
                            "mediaType": "application/json",
                            "data": {
                                "schemaVersion": 1,
                                "kind": "permission_ack",
                                "inputId": "permission-opaque",
                                "toolUseId": "tool-1",
                                "decision": "allow_once",
                                "accepted": True,
                            },
                        }
                    ],
                }
            }
        }

    monkeypatch.setattr(bridge, "_http_json", send)
    monkeypatch.setattr(
        bridge,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start a second stream worker")),
    )
    result = bridge.respond_job(
        SimpleNamespace(
            job_id=job_id,
            input_file=None,
            input_id="permission-opaque",
            tool_use_id="tool-1",
            decision="allow_once",
            follow=False,
        )
    )
    assert captured["payload"]["method"] == "SendMessage"
    assert result["permissionAck"]["accepted"] is True
    job = bridge._load_json(job_path)
    assert job["state"] == "working"
    assert "inputRequired" not in job
    assert "pendingPermissions" not in job
    assert job["workerPid"] == 4321


def test_normal_turn_aggregates_authoritative_result_without_private_session_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "2" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "normal",
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "turnArtifacts": [{"id": "template", "name": "main.tf", "uri": "file:///workspace/main.tf"}],
        },
    )

    bridge._append_projection(
        job_id,
        {
            "type": "text",
            "state": "working",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "text": "intermediate narration",
        },
    )
    bridge._append_projection(
        job_id,
        {
            "type": "assistant-final",
            "state": "working",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "finalText": "Hello world",
            "finalTextComplete": True,
        },
    )
    bridge._complete_normal_turn(job_id)

    result = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert result["state"] == "turn_completed"
    assert result["finalText"] == "Hello world"
    assert result["finalTextComplete"] is True
    assert result["taskId"] == "task-1"
    assert result["contextId"] == "ctx-1"
    assert result["artifacts"][0]["name"] == "main.tf"
    assert "session.jsonl" not in json.dumps(result)
    assert len(bridge._json_bytes(result)) <= bridge.MAX_FOLLOW_BYTES


def test_worker_emits_turn_completed_after_normal_stream_end(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "9" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "normal",
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "turnArtifacts": [],
        },
    )
    request_path = root / "request.json"
    bridge._atomic_json(request_path, {"jsonrpc": "2.0", "method": "SendStreamingMessage", "params": {}})
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {"port": 1, "token": "token"})

    def stream(_record, _payload):
        yield {
            "result": {
                "statusUpdate": {
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "status": {
                        "state": "TASK_STATE_WORKING",
                        "message": {"parts": [{"text": "intermediate narration"}]},
                    },
                }
            }
        }
        yield {
            "result": {
                "statusUpdate": {
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "status": {
                        "state": "TASK_STATE_WORKING",
                        "message": {"parts": [{"text": "Hello world"}]},
                    },
                    "metadata": {"iac_code": {"assistantFinal": {"complete": True}}},
                }
            }
        }

    monkeypatch.setattr(bridge, "_stream_jsonrpc", stream)
    monkeypatch.setattr(
        bridge,
        "_http_json",
        lambda *_args, **_kwargs: {
            "result": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
            }
        },
    )

    assert bridge.worker(job_id, str(request_path)) == 0
    job = bridge._load_json(job_path)
    assert job["state"] == "turn_completed"
    assert job["finalText"] == "Hello world"
    assert job["finalTextComplete"] is True


def test_long_normal_result_becomes_public_workspace_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "3" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "normal",
            "workspace": str(workspace),
            "state": "working",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "turnArtifacts": [],
        },
    )
    complete_text = "结果" * (bridge.MAX_FINAL_TEXT_BYTES // 2 + 10)
    bridge._append_projection(
        job_id,
        {
            "type": "assistant-final",
            "state": "working",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "finalText": complete_text,
            "finalTextComplete": True,
        },
    )
    bridge._complete_normal_turn(job_id)

    job = bridge._load_json(job_path)
    assert job["state"] == "turn_completed"
    assert job["finalTextComplete"] is False
    result_artifact = job["finalArtifacts"][-1]
    result_path = workspace / ".iac-code-skill-results" / result_artifact["name"]
    assert result_artifact["uri"] == result_path.resolve().as_uri()
    assert result_path.read_text(encoding="utf-8") == complete_text


def test_continue_reuses_context_and_gets_a_new_task(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = workspace / "next.txt"
    prompt.write_text("Deploy the confirmed template", encoding="utf-8")
    job_id = "4" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "normal",
            "workspace": str(workspace),
            "state": "turn_completed",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "taskHistory": [],
            "finalText": "ready",
        },
    )
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})
    monkeypatch.setattr(
        bridge,
        "_runtime_configuration_readiness",
        lambda _record, require_cloud: _configuration_readiness(),
    )
    captured = {}

    def spawn(worker_job_id, payload):
        captured["payload"] = payload
        current = bridge._load_json(job_path)
        assert "taskId" not in current
        current["taskId"] = "task-2"
        current["contextId"] = "ctx-1"
        current["state"] = "working"
        bridge._atomic_json(job_path, current)
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    result = bridge.continue_job(
        SimpleNamespace(job_id=job_id, prompt_file=str(prompt), follow=False, follow_seconds=0)
    )

    message = captured["payload"]["params"]["message"]
    assert message["contextId"] == "ctx-1"
    assert "taskId" not in message
    assert result["taskId"] == "task-2"
    assert result["contextId"] == "ctx-1"
    assert result["turn"] == 2
    assert result["configurationReadiness"]["llm"]["ready"] is True
    assert bridge._load_json(job_path)["taskHistory"] == ["task-1"]


def test_continue_reuses_completed_pipeline_handoff_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = workspace / "next.txt"
    prompt.write_text("查询刚部署的 Stack 状态", encoding="utf-8")
    job_id = "d" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "pipeline",
            "conversationMode": "normal",
            "normalHandoffReady": True,
            "pipelineName": "selling",
            "workspace": str(workspace),
            "state": "completed",
            "turn": 1,
            "taskId": "task-pipeline-1",
            "contextId": "ctx-pipeline-1",
            "taskHistory": [],
            "pipelineResult": {"status": "success", "stack_id": "stack-123"},
        },
    )
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})
    monkeypatch.setattr(
        bridge,
        "_runtime_configuration_readiness",
        lambda _record, require_cloud: _configuration_readiness(),
    )
    captured = {}

    def spawn(_worker_job_id, payload):
        captured["payload"] = payload
        current = bridge._load_json(job_path)
        assert current["mode"] == "pipeline"
        assert current["conversationMode"] == "normal"
        assert "taskId" not in current
        assert "pipelineResult" not in current
        current["taskId"] = "task-normal-2"
        current["contextId"] = "ctx-pipeline-1"
        current["state"] = "working"
        bridge._atomic_json(job_path, current)
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)

    result = bridge.continue_job(
        SimpleNamespace(job_id=job_id, prompt_file=str(prompt), follow=False, follow_seconds=0)
    )

    message = captured["payload"]["params"]["message"]
    assert message["contextId"] == "ctx-pipeline-1"
    assert "taskId" not in message
    assert result["taskId"] == "task-normal-2"
    assert result["contextId"] == "ctx-pipeline-1"
    assert result["turn"] == 2
    job = bridge._load_json(job_path)
    assert job["mode"] == "pipeline"
    assert job["conversationMode"] == "normal"
    assert job["taskHistory"] == ["task-pipeline-1"]


def test_pipeline_handoff_normal_turn_gets_authoritative_final_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_id = "e" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "mode": "pipeline",
            "conversationMode": "normal",
            "pipelineName": "selling",
            "workspace": str(workspace),
            "state": "working",
            "turn": 2,
            "taskId": "task-normal-2",
            "contextId": "ctx-pipeline-1",
            "turnArtifacts": [],
        },
    )
    bridge._append_projection(
        job_id,
        {
            "type": "assistant-final",
            "state": "working",
            "taskId": "task-normal-2",
            "contextId": "ctx-pipeline-1",
            "finalText": "Stack 状态为 CREATE_COMPLETE。",
            "finalTextComplete": True,
        },
    )

    bridge._complete_normal_turn(job_id)

    result = bridge._job_result(job_id, 0, bridge.MAX_FOLLOW_BYTES, preserve_final=True)
    assert result["state"] == "turn_completed"
    assert result["conversationMode"] == "normal"
    assert result["finalText"] == "Stack 状态为 CREATE_COMPLETE。"


def test_job_rejects_context_change_without_spooling_it(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "8" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(job_path, {"state": "working", "turn": 1, "contextId": "ctx-1"})

    with pytest.raises(bridge.BridgeError, match="changed the Skill job context"):
        bridge._append_projection(
            job_id,
            {"type": "text", "state": "working", "taskId": "task-2", "contextId": "ctx-other"},
        )

    assert spool.read_text(encoding="utf-8") == ""
    assert bridge._load_json(job_path)["contextId"] == "ctx-1"


def test_continue_rejects_pipeline_and_incomplete_normal_turn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("next", encoding="utf-8")
    job_id = "5" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})
    bridge._atomic_json(
        job_path,
        {"mode": "pipeline", "conversationMode": "pipeline", "state": "completed", "workspace": str(tmp_path)},
    )
    with pytest.raises(bridge.BridgeError, match="completed Pipeline handoff"):
        bridge.continue_job(SimpleNamespace(job_id=job_id, prompt_file=str(prompt), follow=False))
    bridge._atomic_json(
        job_path,
        {"mode": "normal", "conversationMode": "normal", "state": "working", "workspace": str(tmp_path)},
    )
    with pytest.raises(bridge.BridgeError, match="has not completed"):
        bridge.continue_job(SimpleNamespace(job_id=job_id, prompt_file=str(prompt), follow=False))


def test_follow_folds_repeated_progress_and_ignores_text_deltas(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "6" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    events = []
    for _index in range(100):
        events.append(
            {
                "type": "milestone",
                "state": "working",
                "milestones": [{"eventType": "tool_started", "toolName": "aliyun_api"}],
            }
        )
        events.append({"type": "text", "state": "working", "text": "token"})
    events.append({"type": "turn_completed", "state": "turn_completed", "finalTextAvailable": True})
    spool.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "state": "turn_completed",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "finalText": "done",
            "finalTextComplete": True,
            "finalArtifacts": [],
        },
    )

    result = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    progress = capsys.readouterr().err.splitlines()
    assert result["state"] == "turn_completed"
    assert result["finalText"] == "done"
    assert result["progressLines"] == 2
    assert len(progress) == 2
    assert all("token" not in line for line in progress)
    assert len(result["milestones"]) == 1
    assert result["folded"]["duplicate_milestone"] == 99
    assert result["progressBytes"] <= bridge.MAX_FOLLOW_PROGRESS_BYTES
    assert len(bridge._json_bytes(result)) <= bridge.MAX_FOLLOW_BYTES


def test_follow_returns_each_step_boundary_before_current_input(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "c" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    pending = {
        "kind": "candidate_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "candidate-input-1",
        "prompt": "Choose a plan",
        "options": [{"id": "candidate-1", "label": "Plan 1"}],
    }
    events = [
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-1",
            "milestones": [{"eventType": "step_started", "step": {"id": "intent_parsing"}}],
        },
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-1",
            "milestones": [
                {"eventType": "step_completed", "step": {"id": "intent_parsing"}},
                {"eventType": "step_started", "step": {"id": "architecture_planning"}},
            ],
        },
        {
            "type": "input-required",
            "state": "input-required",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "inputRequired": pending,
        },
    ]
    spool.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "state": "input-required",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "conversationMode": "pipeline",
            "inputRequired": pending,
            "turnArtifacts": [],
        },
    )

    first = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert first["state"] == "working"
    assert first["boundaryReached"] is True
    assert first["presentationRequired"] is True
    assert first["userUpdates"] == ["Step started: understand deployment requirements"]
    assert first["cursor"] == 1
    assert [item["eventType"] for item in first["milestones"]] == ["step_started"]
    assert "inputRequired" not in first

    second = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=first["cursor"], wait_seconds=0))
    assert second["state"] == "working"
    assert second["boundaryReached"] is True
    assert second["presentationRequired"] is True
    assert second["userUpdates"] == [
        "Step completed: understand deployment requirements",
        "Step started: design candidate architectures",
    ]
    assert second["cursor"] == 2
    assert [item["eventType"] for item in second["milestones"]] == ["step_completed", "step_started"]
    assert "inputRequired" not in second

    third = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=second["cursor"], wait_seconds=0))
    assert third["state"] == "input-required"
    assert third["cursor"] == len(events)
    assert third["inputRequired"] == pending
    assert "boundaryReached" not in third
    assert "presentationRequired" not in third
    assert "userUpdates" not in third
    capsys.readouterr()


def test_follow_drains_step_boundaries_before_current_terminal_result(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "d" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    events = [
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-1",
            "milestones": [{"eventType": "step_started", "step": {"id": "deploying"}}],
        },
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-1",
            "milestones": [{"eventType": "step_completed", "step": {"id": "deploying"}}],
        },
        {"type": "terminal", "state": "completed", "taskId": "task-1", "contextId": "ctx-1"},
    ]
    spool.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "state": "completed",
            "mode": "pipeline",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "conversationMode": "pipeline",
            "pipelineResult": {"status": "CREATE_COMPLETE"},
            "turnArtifacts": [],
        },
    )

    first = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    assert first["state"] == "working"
    assert first["cursor"] == 1
    assert "pipelineResult" not in first

    second = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=first["cursor"], wait_seconds=0))
    assert second["state"] == "working"
    assert second["cursor"] == 2
    assert "pipelineResult" not in second

    third = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=second["cursor"], wait_seconds=0))
    assert third["state"] == "completed"
    assert third["cursor"] == len(events)
    assert third["pipelineResult"] == {"status": "CREATE_COMPLETE"}
    assert "boundaryReached" not in third
    capsys.readouterr()


def test_follow_uses_current_job_boundary_when_cursor_includes_previous_turn(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "a" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    events = [
        {
            "type": "input-required",
            "state": "input-required",
            "taskId": "task-1",
            "inputRequired": {"kind": "permission", "inputId": "old-input"},
        },
        {
            "type": "turn_completed",
            "state": "turn_completed",
            "taskId": "task-1",
            "text": "old final",
        },
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-2",
            "milestones": [{"eventType": "tool_started", "toolName": "aliyun_api"}],
        },
    ]
    spool.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "state": "working",
            "turn": 2,
            "taskId": "task-2",
            "contextId": "ctx-1",
            "turnArtifacts": [],
        },
    )

    result = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    progress = capsys.readouterr().err
    assert result["state"] == "working"
    assert result["taskId"] == "task-2"
    assert result["followTimedOut"] is True
    assert result["cursor"] == len(events)
    assert "inputRequired" not in result
    assert "finalText" not in result
    assert result["milestones"] == [{"eventType": "tool_started", "toolName": "aliyun_api"}]
    assert result["folded"]["stale_task_event"] == 2
    assert "old-input" not in progress and "old final" not in progress


def test_follow_non_ascii_progress_uses_actual_stderr_byte_budget(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "b" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    events = [
        {
            "type": "milestone",
            "state": "working",
            "taskId": "task-1",
            "milestones": [{"eventType": "阶段_{}".format(index), "message": "正在处理阿里云资源" * 30}],
        }
        for index in range(24)
    ]
    spool.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "state": "turn_completed",
            "turn": 1,
            "taskId": "task-1",
            "contextId": "ctx-1",
            "finalText": "done",
            "finalTextComplete": True,
            "finalArtifacts": [],
        },
    )

    result = bridge.follow_job(SimpleNamespace(job_id=job_id, cursor=0, wait_seconds=0))
    stderr_bytes = capsys.readouterr().err.encode("utf-8")
    assert result["state"] == "turn_completed"
    assert result["progressBytes"] == len(stderr_bytes)
    assert len(stderr_bytes) <= bridge.MAX_FOLLOW_PROGRESS_BYTES
    assert result["progressLines"] <= bridge.MAX_FOLLOW_PROGRESS_LINES


def test_permission_response_requires_every_correlation_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    job_id = "7" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    pending = {
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
    }
    bridge._atomic_json(job_path, {"state": "input-required", "inputRequired": pending})
    monkeypatch.setattr(bridge, "_runtime_record_for_job", lambda _job: {})

    for missing in ("requestTaskId", "contextId", "inputId", "toolUseId"):
        response = {**pending, "decision": "allow_once"}
        response.pop(missing)
        response_path = tmp_path / (missing + ".json")
        response_path.write_text(json.dumps(response), encoding="utf-8")
        with pytest.raises(bridge.BridgeError, match="correlation fields"):
            bridge.respond_job(SimpleNamespace(job_id=job_id, input_file=str(response_path), follow=False))

    response = {**pending, "decision": "allow_once", "contextId": "ctx-old"}
    response_path = tmp_path / "mismatch.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    with pytest.raises(bridge.BridgeError, match="correlation fields"):
        bridge.respond_job(SimpleNamespace(job_id=job_id, input_file=str(response_path), follow=False))


def test_skill_contract_uses_implicit_trigger_normal_default_and_follow() -> None:
    skill = (ROOT / "skills/iac-code/SKILL.md").read_text(encoding="utf-8")
    agent_metadata = (ROOT / "skills/iac-code/agents/openai.yaml").read_text(encoding="utf-8")
    assert "even when the user does not mention iac-code, ROS, Terraform" in skill
    assert "Normal is the default" in skill
    assert "candidate-architecture, cost-comparison, plan-confirmation" in skill
    assert "start --mode normal" in skill and "--follow" in skill
    assert "python3 scripts/iac_code.py continue" in skill
    assert "python scripts/iac_code.py" not in skill
    assert "On Windows, replace `python3` with `py -3`" in skill
    assert "CPython 3.8–3.14" in skill
    assert "Pipeline reaches any terminal state" in skill
    assert "treat `pipelineResult` and `artifacts` as its authoritative result" in skill
    assert "bridge enforces a 120-second maximum" in skill
    assert "never translate Chinese user-visible content into English" in skill
    assert "Never call `start --mode normal` to continue a completed Pipeline" in skill
    assert "apply the outer Agent's own equivalent permission policy" in skill
    assert "`presentationRequired: true`" in skill
    assert "ready-to-display localized strings in `userUpdates`" in skill
    assert "Before invoking another tool, emit every `userUpdates` string" in skill
    assert "automatically runs a cleanup-only normal task" in skill
    assert "Never send a synthetic cleanup prompt" in skill
    assert "Do not expand this into raw tool-event or token-delta output" in skill
    assert "`llm_not_configured`" in skill
    assert "`cloud_credentials_not_configured`" in skill
    assert "python3 scripts/iac_code.py cache list" in skill
    assert "cache clean --candidates --confirm" in skill
    assert "remove only downloaded Runtime packages" in skill
    assert "session.jsonl" not in skill
    assert "`pip install" not in skill
    assert "Default to normal" in agent_metadata
    assert "candidate architectures, cost comparison, plan confirmation" in agent_metadata


def test_parser_exposes_continue_follow_and_diagnostic_poll() -> None:
    parser = bridge._parser()
    continued = parser.parse_args(
        ["continue", "--job-id", "a" * 32, "--prompt-file", "/workspace/next.txt", "--follow"]
    )
    followed = parser.parse_args(["follow", "--job-id", "a" * 32, "--cursor", "7", "--wait-seconds", "60"])
    polled = parser.parse_args(["poll", "--job-id", "a" * 32, "--cursor", "7"])
    permission = parser.parse_args(
        [
            "respond",
            "--job-id",
            "a" * 32,
            "--input-id",
            "permission-1",
            "--tool-use-id",
            "tool-1",
            "--decision",
            "allow_once",
            "--follow",
        ]
    )
    cache_list = parser.parse_args(["cache", "list"])
    cache_clean = parser.parse_args(["cache", "clean", "--candidates", "--confirm"])

    assert continued.follow is True
    assert followed.cursor == 7 and followed.wait_seconds == 60
    assert polled.command == "poll"
    assert permission.input_file is None
    assert permission.decision == "allow_once"
    assert cache_list.cache_command == "list"
    assert cache_clean.candidates is True and cache_clean.confirm is True


def _cached_runtime(config_root: Path, runtime_tag: str, target: str, content: bytes) -> Path:
    runtime = config_root / "skill-runtime" / runtime_tag / target / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "payload.bin").write_bytes(content)
    bridge._atomic_json(
        runtime / ".iac-code-runtime.json",
        {
            "runtimeTag": runtime_tag,
            "target": target,
            "runtimePython": "cp312",
            "artifactSha256": "a" * 64,
            "installedAt": 123,
        },
    )
    return runtime


def test_runtime_cache_list_reports_size_and_active_status_without_installing(monkeypatch, tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_root))
    target = "darwin-arm64-macos-cp312"
    _cached_runtime(config_root, bridge.RUNTIME_TAG, target, b"current")
    _cached_runtime(config_root, "candidate-old", target, b"candidate")
    server = config_root / "skill-runtime" / "servers" / "server-1"
    bridge._atomic_json(
        server / "runtime.json",
        {"runtimeTag": "candidate-old", "target": target, "pid": 42},
    )
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: pid == 42)

    result = bridge.list_runtime_cache()

    assert result["runtimeCount"] == 2
    assert result["totalSizeBytes"] > len(b"current") + len(b"candidate")
    by_tag = {entry["runtimeTag"]: entry for entry in result["runtimes"]}
    assert by_tag[bridge.RUNTIME_TAG]["current"] is True
    assert by_tag["candidate-old"]["candidate"] is True
    assert by_tag["candidate-old"]["active"] is True
    assert all("Path" not in key for entry in result["runtimes"] for key in entry)


def test_runtime_cache_clean_requires_confirmation_and_protects_current_and_active(monkeypatch, tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_root))
    target = "darwin-arm64-macos-cp312"
    current = _cached_runtime(config_root, bridge.RUNTIME_TAG, target, b"current")
    active = _cached_runtime(config_root, "candidate-active", target, b"active")
    stale = _cached_runtime(config_root, "candidate-stale", target, b"stale")
    server = config_root / "skill-runtime" / "servers" / "server-1"
    bridge._atomic_json(
        server / "runtime.json",
        {"runtimeTag": "candidate-active", "target": target, "pid": 42},
    )
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: pid == 42)

    with pytest.raises(bridge.BridgeError) as caught:
        bridge.clean_runtime_cache(SimpleNamespace(confirm=False, candidates=True, runtime_tag=None))
    assert caught.value.code == "cache_cleanup_confirmation_required"

    result = bridge.clean_runtime_cache(SimpleNamespace(confirm=True, candidates=True, runtime_tag=None))

    assert result["deletedCount"] == 1
    assert result["deleted"][0]["runtimeTag"] == "candidate-stale"
    assert result["freedBytes"] > 0
    assert current.is_dir()
    assert active.is_dir()
    assert not stale.exists()
    assert result["skipped"][0]["reason"] == "active_runtime"

    protected = bridge.clean_runtime_cache(
        SimpleNamespace(confirm=True, candidates=False, runtime_tag=bridge.RUNTIME_TAG)
    )
    assert protected["deletedCount"] == 0
    assert protected["skipped"][0]["reason"] == "current_runtime"
