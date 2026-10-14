"""OPS transport provisioning only; no model/cloud/browser invocation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import worker_hosts

REMOTE_INSTALL = r'''
import hashlib,json,os,sys
from pathlib import Path
r=json.load(sys.stdin);p=Path(r['cwd'])
if not p.is_absolute() or len(p.parts)<3 or '..' in p.parts:raise ValueError('cwd')
p.mkdir(parents=True,exist_ok=True);os.chmod(p,0o700)
for name,text in [('workflow-agent.py',r['script']),('.workflow-client.json',json.dumps(r['descriptor']))]:
 t=p/name
 if t.is_symlink():raise ValueError('symlink')
 with t.open('w',encoding='utf-8') as f:os.chmod(t,0o600);f.write(text)
print(json.dumps({'ready':True,'script':str(p/'workflow-agent.py'),'python':sys.executable,
 'script_sha256':hashlib.sha256((p/'workflow-agent.py').read_bytes()).hexdigest()}))
'''


def install_local(cwd: Path, source: str, descriptor: dict) -> dict:
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cwd, 0o700)
    for name, text in (("workflow-agent.py", source), (".workflow-client.json", json.dumps(descriptor))):
        target = cwd / name
        if target.is_symlink():
            raise ValueError("client file symlink")
        with target.open("w", encoding="utf-8") as handle:
            os.chmod(target, 0o600)
            handle.write(text)
    return {"ready": True, "script": str(cwd / "workflow-agent.py"), "python": sys.executable,
            "script_sha256": hashlib.sha256(source.encode()).hexdigest()}


def reverse_argv(config: dict, port: int) -> list[str]:
    argv = worker_hosts.ssh_base(config)
    destination = argv.pop()
    return argv + ["-N", "-o", "ExitOnForwardFailure=yes", "-R",
                   f"127.0.0.1:0:127.0.0.1:{port}", destination]


def allocated_port(process: subprocess.Popen) -> int:
    """SSH port 0 gives every job its own remote listener, including four slots."""
    deadline = time.monotonic() + 25
    pending = b""
    while process.poll() is None and time.monotonic() < deadline:
        if not select.select([process.stderr], [], [], min(1, deadline - time.monotonic()))[0]:
            continue
        chunk = os.read(process.stderr.fileno(), 4096)
        if not chunk:
            break
        pending += chunk
        for line in pending.split(b"\n")[:-1]:
            match = re.search(rb"Allocated port ([0-9]+) for remote forward", line)
            if match and 1 <= int(match[1]) <= 65535:
                return int(match[1])
        pending = pending.split(b"\n")[-1][-8192:]
    raise ValueError("private reverse forward unavailable before turn/start")


def main() -> int:
    root, job_id, port_text = sys.argv[1:]
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not 1 <= int(port_text) <= 65535:
        raise ValueError("exact job and bridge required")
    directory = Path(root) / "jobs" / job_id
    config = json.loads((Path(root) / "environment.json").read_text(encoding="utf-8"))
    launch = json.loads((directory / "launch.json").read_text(encoding="utf-8"))
    descriptor = json.loads(sys.stdin.readline())
    source = Path(__file__).with_name("agent_client.py").read_text(encoding="utf-8")
    selected = launch.get("worker_backend", {"host": "local"})
    tunnel = None
    try:
        if selected["host"] == "local":
            result = install_local(directory / "sandbox", source, descriptor)
        elif selected["host"] == "macmini":
            # Adapter already verifies durable backend and original link fingerprint.
            # This forward is independent; it never replaces the Web/CDP/model link.
            tunnel = subprocess.Popen(reverse_argv(config, int(port_text)), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            port = allocated_port(tunnel)
            descriptor = {**descriptor, "origin": f"http://127.0.0.1:{port}"}
            request = {"cwd": selected["cwd"], "script": source, "descriptor": descriptor}
            command = shlex.join([worker_hosts.mini(config)["python"], "-c", REMOTE_INSTALL])
            installed = subprocess.run(worker_hosts.ssh_base(config) + [command], input=json.dumps(request),
                                       capture_output=True, encoding="utf-8", timeout=20, check=True)
            result = json.loads(installed.stdout)
            if result.get("script_sha256") != hashlib.sha256(source.encode()).hexdigest():
                raise ValueError("remote client SHA mismatch")
        else:
            raise ValueError("unknown client host")
        print(json.dumps(result), flush=True)
        # Parent lifetime, not a resource deadline. EOF also cleans up after crashes.
        while True:
            if tunnel is not None and tunnel.poll() is not None:
                raise ValueError("original job reverse forward lost; no automatic reconnect")
            if select.select([sys.stdin], [], [], 1)[0] and not sys.stdin.read(1):
                break
        return 0
    finally:
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        print("script client transport failed; preserve original job", file=sys.stderr)
        sys.exit(1)
