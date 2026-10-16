#!/usr/bin/env python3
"""Cold permission handoff across real PID namespaces with equal worker PIDs.

Linux can run directly with unshare; Docker is the portable fallback. Missing
namespace support is a test failure, never a skipped or simulated acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from contextlib import suppress
from pathlib import Path

from filelock import FileLock

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from tests.a2a.test_permission_handoff_process_e2e import assert_success, count_lines, document, wait_for  # noqa: E402
from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply  # noqa: E402


def _backend(requested):
    if requested in {"auto", "unshare"} and platform.system() == "Linux" and shutil.which("unshare"):
        for backend, user_flags in (("unshare", ["--user", "--map-root-user"]), ("unshare-root", [])):
            probe = subprocess.run(
                ["unshare", *user_flags, "--mount", "--pid", "--fork", "--mount-proc", "true"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if probe.returncode == 0:
                return backend
        if requested == "unshare":
            raise RuntimeError("unshare cannot create a real PID namespace: " + probe.stderr.decode(errors="replace"))
    if requested in {"auto", "docker"} and shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=15
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return "docker"
    raise RuntimeError("Real Linux PID namespaces require usable unshare or a Docker daemon")


def _image(run_dir):
    dockerfile = Path(__file__).with_name("namespace.Dockerfile")
    digest = hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes() + dockerfile.read_bytes()).hexdigest()[:16]
    image_name = "iac-code-e2e-namespace:" + digest
    with FileLock(str(Path(tempfile.gettempdir()) / ("iac-code-e2e-image-" + digest + ".lock")), timeout=600):
        exists = subprocess.run(["docker", "image", "inspect", image_name], capture_output=True, check=False)
        if exists.returncode == 0:
            return image_name
        with tempfile.TemporaryDirectory(prefix="iac-namespace-build-") as temporary:
            context = Path(temporary)
            for name in ("pyproject.toml", "uv.lock", "README.md", "setup.py", "src/iac_code/__init__.py"):
                target = context / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / name, target)
            shutil.copyfile(dockerfile, context / "Dockerfile")
            with (run_dir / "container-build.log").open("w", encoding="utf-8") as log:
                subprocess.run(
                    ["docker", "build", "-t", image_name, str(context)], stdout=log, stderr=log, timeout=600, check=True
                )
    return image_name


class NamespaceWorker(Server):
    def __init__(self, root, name, backend, image=None):
        super().__init__(Path("/") if backend == "docker" else root)
        self.host_root, self.name, self.backend, self.image = root, name, backend, image
        self.local = root / name / "config"
        self.local.mkdir(parents=True)
        self.container = "iac-code-e2e-" + uuid.uuid4().hex
        self.log = None
        self.started = False

    def start(self):
        self.log = (self.host_root / (self.name + ".log")).open("a", encoding="utf-8")
        self.started = True
        if self.backend == "docker":
            command = [
                "docker",
                "run",
                "--detach",
                "--name",
                self.container,
                "--publish",
                "127.0.0.1:{}:8000".format(self.port),
                "--volume",
                str(REPO_ROOT) + ":/repo:ro",
                "--volume",
                str(self.local) + ":/config",
                "--volume",
                str(self.host_root / "shared") + ":/shared",
                "--volume",
                str(self.host_root / "workspace") + ":/workspace",
                self.image,
                "--root",
                "/",
                "--port",
                "8000",
                "--host",
                "0.0.0.0",
                "--config-dir",
                "/config",
                "--shared-dir",
                "/shared",
            ]
            subprocess.run(command, stdout=self.log, stderr=self.log, check=True, timeout=30)
        else:
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))
            command = [
                "unshare",
                *(["--user", "--map-root-user"] if self.backend == "unshare" else []),
                "--mount",
                "--pid",
                "--fork",
                "--mount-proc",
                sys.executable,
                "-m",
                "scripts.a2a.e2e.permission_wait.namespace_fixture",
                "--root",
                str(self.host_root),
                "--port",
                str(self.port),
                "--config-dir",
                str(self.local),
                "--shared-dir",
                str(self.host_root / "shared"),
            ]
            self.process = subprocess.Popen(
                command, cwd=REPO_ROOT, env=env, stdout=self.log, stderr=self.log, start_new_session=True
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.client.get("/health").status_code == 200:
                    return
            except Exception:
                pass
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("Namespace worker exited: " + (self.host_root / (self.name + ".log")).read_text())
            time.sleep(0.05)
        raise RuntimeError("Namespace worker startup timed out")

    def stop(self):
        if not self.started:
            return
        if self.backend == "docker":
            if self.log is not None:
                with suppress(subprocess.TimeoutExpired):
                    subprocess.run(["docker", "logs", self.container], stdout=self.log, stderr=self.log, timeout=3)
            subprocess.run(["docker", "rm", "--force", self.container], capture_output=True, timeout=12)
        elif self.process is not None:
            # unshare forks the namespace's PID 1; terminate the owned process
            # group so a timed-out runner cannot leave that child serving.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
        self.started = False


def run(run_dir, decision, backend):
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("shared", "workspace"):
        (run_dir / name).mkdir()
    actual_backend = _backend(backend)
    image = _image(run_dir) if actual_backend == "docker" else None
    old = NamespaceWorker(run_dir, "old", actual_backend, image)
    new = NamespaceWorker(run_dir, "new", actual_backend, image)
    try:
        old.start()
        permission = _permission(old.send("original"))
        wait_for(lambda: (document(run_dir).get("inputHandoff") or {}).get("phase") == "COMMITTED")
        before = document(run_dir)
        old_identity = json.loads((old.local / "fixture-owner.json").read_text())
        assert before["inputHandoff"]["backupReceipt"]["sharedCommitted"] is True
        assert before["inputHandoffReady"] is True
        assert list(new.local.iterdir()) == [], "Replacement must start with an empty local config"
        new.start()
        new_identity = json.loads((new.local / "fixture-owner.json").read_text())
        assert old_identity["pid"] == new_identity["pid"] == 1
        assert old_identity["pidNamespace"] and new_identity["pidNamespace"]
        assert old_identity["pidNamespace"] != new_identity["pidNamespace"]
        assert old_identity["pidNamespace"].split(":")[:2] != new_identity["pidNamespace"].split(":")[:2]
        assert old_identity["runtimeIncarnation"] and new_identity["runtimeIncarnation"]
        old.stop()
        assert count_lines(run_dir / "shared/executions.jsonl") == 0
        if decision == "prompt":
            events = new.send("new prompt")
            assert_success(events, "handled new prompt")
        else:
            events = new.send(_reply(permission, decision), task_id=permission["requestTaskId"])
            assert_success(events, "completed approved batch")
        expected_executions = int(decision == "allow_once")
        assert count_lines(run_dir / "shared/executions.jsonl") == expected_executions
        assert document(run_dir)["executionId"] != before["executionId"]
        assert list((new.local / "projects").rglob("session.jsonl")), "Replacement must restore the shared session"
        assert_success(new.send("follow-up prompt"), "handled new prompt")
        calls = count_lines(run_dir / "shared/model-inputs.jsonl")
        assert calls == 3
        new.send(_reply(permission), task_id=permission["requestTaskId"])
        new.send(_reply(permission, "deny"), task_id=permission["requestTaskId"])
        assert count_lines(run_dir / "shared/model-inputs.jsonl") == calls
        assert count_lines(run_dir / "shared/executions.jsonl") == expected_executions
        return {
            "passed": True,
            "backend": actual_backend,
            "decision": decision,
            "oldOwner": old_identity,
            "newOwner": new_identity,
            "checks": {
                "equal PID in distinct real namespaces": True,
                "replacement restored shared session into empty local config": True,
                "permission or new prompt followed by ordinary input succeeds": True,
                "stale replies do not execute tools": True,
            },
        }
    finally:
        for server in (new, old):
            server.stop()
            server.client.close()


def main():
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--decision", choices=("allow_once", "deny", "prompt"), required=True)
    parser.add_argument("--backend", choices=("auto", "unshare", "docker"), default="auto")
    args = parser.parse_args()
    try:
        result = run(args.run_dir, args.decision, args.backend)
    except Exception as exc:
        traceback.print_exc()
        result = {"passed": False, "error": repr(exc)}
    args.run_dir.mkdir(parents=True, exist_ok=True)
    (args.run_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not result["passed"]:
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main()
