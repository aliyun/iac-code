"""GLM/Chrome host selection; Web, SQLite, Sol and evidence remain local."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .files import WorkflowError, digest, json_write, read_json

LOADED_SHA = digest(Path(__file__))
HOSTS = {"local", "macmini"}


def host(config: dict[str, Any]) -> str:
    value = config.get("worker_host", "local")
    if not isinstance(value, str) or value not in HOSTS:
        raise WorkflowError("worker_host must be local or macmini")
    return value


def mini(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("macmini")
    required = ("ip_resolver", "identity_file", "known_hosts", "user", "chrome_executable", "chrome_profile",
                "worker_cwd", "python")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise WorkflowError("Mac mini needs explicit SSH, Chrome profile and worker cwd configuration")
    for key in ("identity_file", "known_hosts", "chrome_executable", "chrome_profile", "worker_cwd"):
        path = Path(value[key]) if key in {"identity_file", "known_hosts"} else PurePosixPath(value[key])
        if not path.is_absolute() or len(path.parts) < 3 or any(part in {"..", "~"} for part in path.parts):
            raise WorkflowError("Mac mini paths must be explicit absolute non-root paths")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", value["user"]):
        raise WorkflowError("invalid SSH user")
    return value


def binding(config: dict[str, Any], role: str) -> dict[str, Any]:
    if role == "sol":
        result = {"id": "sol-local", "endpoint": config.get("sol_endpoint"), "host": "local"}
    elif role == "glm" and host(config) == "macmini":
        result = {"id": "alilang-macmini", "endpoint": "ws://127.0.0.1:4510", "host": "macmini",
                  "cwd": mini(config)["worker_cwd"]}
    elif role == "glm":
        result = {"id": "bailian-local", "endpoint": config.get("bailian_endpoint"), "host": "local"}
    else:
        raise WorkflowError("only Sol and GLM have worker bindings")
    if (not isinstance(result["endpoint"], str)
            or not re.fullmatch(r"ws://127\.0\.0\.1:[0-9]{1,5}", result["endpoint"])
            or not 1 <= int(result["endpoint"].rsplit(":", 1)[1]) <= 65535):
        raise WorkflowError("configure worker app-server loopback endpoint before launch")
    return result


def ssh_base(config: dict[str, Any]) -> list[str]:
    value = mini(config)
    try:
        result = subprocess.run([value["ip_resolver"]], capture_output=True, encoding="utf-8", timeout=15, check=False)
        address = str(ipaddress.IPv4Address(result.stdout.strip()))
        if result.returncode:
            raise ValueError
    except (ValueError, OSError, subprocess.SubprocessError):
        raise WorkflowError("Mac mini address discovery failed; no cached IP fallback") from None
    return [value.get("ssh", "ssh"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "HostKeyAlias=macmini-dynamic", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + value["known_hosts"], "-i", value["identity_file"],
            "-o", "IdentitiesOnly=yes", value["user"] + "@" + address]


# Fixed OPS process operations, not a model shell tool. No Web tokens, cloud
# credentials, repositories or screenshots need copying to the remote machine.
REMOTE_CHROME = r'''
import json,os,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
def identity(pid):
 return subprocess.check_output(['ps','-p',str(pid),'-o','lstart=,command='],encoding='utf-8').strip()
def validate(current,profile):
 import re
 if '--remote-debugging-port=9225' not in current or not re.search(
  re.escape('--user-data-dir='+str(profile))+r'(?=$| --)',current):raise ValueError('profile mismatch')
profile=Path(r['profile'])
if not profile.is_absolute() or len(profile.parts)<3 or '..' in profile.parts:raise ValueError('profile')
if r['operation']=='workspace':
 p=Path(r['cwd'])
 if not p.is_absolute() or len(p.parts)<3 or '..' in p.parts:raise ValueError('cwd')
 p.mkdir(parents=True,exist_ok=True);os.chmod(p,0o700);print(json.dumps({'workspace_ready':True}));sys.exit()
if r['operation']=='start':
 import socket
 if r.get('pid'):
  try:
   current=identity(r['pid'])
  except subprocess.CalledProcessError:current=None
  if current is not None:
   if current!=r['identity']:raise ValueError('owner mismatch')
   validate(current,profile)
   print(json.dumps({'pid':r['pid'],'identity':current,'profile':str(profile)}));sys.exit()
 with socket.socket() as probe:probe.bind(('127.0.0.1',9225))
 profile.mkdir(parents=True,exist_ok=True);os.chmod(profile,0o700)
 with (profile.parent/'chrome.log').open('ab') as log:
  os.chmod(profile.parent/'chrome.log',0o600)
  p=subprocess.Popen([r['executable'],'--remote-debugging-port=9225','--remote-debugging-address=127.0.0.1',
   '--user-data-dir='+str(profile),'--no-first-run','about:blank'],stdout=log,stderr=log,start_new_session=True)
 current=identity(p.pid);pid=p.pid
elif r['operation'] in ('status','adopt'):
 pid=r['pid'];current=identity(pid)
 if r['operation']=='status' and current!=r['identity']:raise ValueError('owner mismatch')
else:raise ValueError('operation')
validate(current,profile)
print(json.dumps({'pid':pid,'identity':current,'profile':str(profile)}))
'''


def remote_chrome(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    value = mini(config)
    command = shlex.join([value["python"], "-c", REMOTE_CHROME])
    try:
        result = subprocess.run(ssh_base(config) + [command], input=json.dumps(request), capture_output=True,
                                encoding="utf-8", timeout=30, check=False)
        if result.returncode:
            raise ValueError
        return json.loads(result.stdout)
    except (ValueError, subprocess.SubprocessError, OSError):
        raise WorkflowError("Mac mini Chrome OPS failed; preserve owner/profile, never fall back locally") from None


def tunnel_fingerprint(config: dict[str, Any], ports: list[int]) -> str:
    return hashlib.sha256(json.dumps({"config": mini(config), "ports": ports}, sort_keys=True).encode()).hexdigest()


def require_tunnel(runtime: Any, *, connection: Any = None) -> dict[str, Any]:
    marker = runtime.root / "macmini-link.json"
    if not marker.is_file():
        raise WorkflowError("start the owned Mac mini link before using remote GLM/Chrome")
    record = read_json(marker)
    # launch already holds the Store write transaction. Reuse that snapshot;
    # opening status() here would recursively BEGIN IMMEDIATE and lock itself.
    slots = connection.execute("SELECT origin FROM slots ORDER BY id") if connection is not None else (
        runtime.store.status()["slots"]
    )
    ports = [int(slot["origin"].rsplit(":", 1)[1]) for slot in slots]
    if record.get("fingerprint") != tunnel_fingerprint(runtime.config, ports):
        raise WorkflowError("Mac mini link configuration changed; restore original link under OPS")
    runtime.assert_process(record["pid"], record["identity"])
    return record


def start_link(runtime: Any) -> dict[str, Any]:
    if host(runtime.config) != "macmini":
        raise WorkflowError("select macmini first; no remote link starts in local mode")
    marker = runtime.root / "macmini-link.json"
    if marker.exists():
        try:
            old = require_tunnel(runtime)
            return {"already_running": True, "pid": old["pid"]}
        except subprocess.CalledProcessError:
            pass  # Only an absent exact PID can be restarted, not a conflicting owner/config.
    ports = [int(slot["origin"].rsplit(":", 1)[1]) for slot in runtime.store.status()["slots"]]
    if sorted(ports) != list(range(16100, 16104)):
        raise WorkflowError("remote Web forwards require the four owned 16100-16103 slots")
    runtime.require_free_port(4510)
    runtime.require_free_port(9226)
    argv = ssh_base(runtime.config)
    # Options precede the discovered destination. Every listener is loopback;
    # reverse forwards keep the same bare origin on both machines.
    destination = argv.pop()
    argv += ["-N", "-o", "ExitOnForwardFailure=yes", "-L", "127.0.0.1:4510:127.0.0.1:4500",
             "-L", "127.0.0.1:9226:127.0.0.1:9225"]
    for port in ports:
        argv += ["-R", f"127.0.0.1:{port}:127.0.0.1:{port}"]
    argv.append(destination)
    with (runtime.root / "macmini-link.log").open("ab") as log:
        os.chmod(runtime.root / "macmini-link.log", 0o600)
        process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
    identity = runtime.process_identity(process.pid)
    json_write(marker, {"pid": process.pid, "identity": identity,
                       "fingerprint": tunnel_fingerprint(runtime.config, ports)}, private=True)
    # Popen is not readiness. Real forward/controller/CDP checks occur before dispatch.
    return {"pid": process.pid, "link_started": True, "ready": False, "preflight_required": True}


def stop_link(runtime: Any) -> dict[str, Any]:
    require_idle(runtime)
    old = require_tunnel(runtime)
    os.kill(old["pid"], signal.SIGTERM)
    return {"stopped_link": old["pid"], "remote_chrome_and_login_preserved": True}


def require_idle(runtime: Any) -> None:
    with runtime.store.transaction() as connection:
        assert_idle(connection)


def assert_idle(connection: Any) -> None:
    paused = connection.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
    if (not paused or paused[0] != "true"
            or connection.execute("SELECT 1 FROM jobs WHERE state!='complete'").fetchone()
            or connection.execute("SELECT 1 FROM slots WHERE task_id IS NOT NULL").fetchone()):
        raise WorkflowError("pause and drain all jobs/leases before switching worker hosts")
    if connection.execute("SELECT 1 FROM owned WHERE state!='deleted'").fetchone():
        raise WorkflowError("resolve owned cloud resources before switching worker hosts")
    if connection.execute("SELECT 1 FROM actions WHERE state='issued'").fetchone():
        raise WorkflowError("drain issued actions before switching worker hosts")
    if connection.execute(
        "SELECT 1 FROM jobs j JOIN tasks t ON t.id=j.task_id "
        "WHERE t.state NOT IN ('complete','superseded') AND j.role='glm' AND j.thread_id IS NOT NULL"
    ).fetchone():
        raise WorkflowError("finish the original resource thread before switching worker hosts")


def assert_selection(runtime: Any) -> None:
    # File and SQLite cannot commit atomically. A torn OPS switch fails closed,
    # rather than driving the previous machine's targets with the new config.
    with runtime.store.transaction() as connection:
        row = connection.execute("SELECT value FROM meta WHERE key='worker_host'").fetchone()
    if row and row[0] != host(runtime.config):
        raise WorkflowError("worker host/config commit mismatch; OPS restore the exact saved selection")


def select_host(runtime: Any, selected: str) -> dict[str, Any]:
    if selected not in HOSTS:
        raise WorkflowError("worker host must be local or macmini")
    require_idle(runtime)
    assert_selection(runtime)
    previous = host(runtime.config)
    config = {**runtime.config, "worker_host": selected}
    if selected == "macmini":
        mini(config)
    if selected == previous:
        return {"worker_host": selected, "changed": False}
    # Preserve each machine's exact targets; do not point remote Chrome at local target IDs.
    with runtime.store.transaction() as connection:
        assert_idle(connection)
        targets = {str(row["id"]): row["target"] for row in connection.execute("SELECT id,target FROM slots")}
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                           ("browser_targets:" + previous, json.dumps(targets)))
        row = connection.execute("SELECT value FROM meta WHERE key=?", ("browser_targets:" + selected,)).fetchone()
        saved = json.loads(row[0]) if row else {}
        for key in targets:
            connection.execute("UPDATE slots SET target=? WHERE id=?", (saved.get(key), int(key)))
        runtime.store.event(connection, "worker_host_selected", {"from": previous, "to": selected})
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('worker_host',?)", (selected,))
        json_write(runtime.root / "environment.json", config, private=True)
    runtime.config = config
    return {"worker_host": selected, "changed": True, "restart_same_server_required": True,
            "web_and_sol_remain_local": True, "bootstrap_exact_host_targets_required": True}
