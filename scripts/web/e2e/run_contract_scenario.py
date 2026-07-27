#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aliyun.e2e_contract_audit import (  # noqa: E402
    audit_aliyun_result_contract,
    find_latest_aliyun_tool_result,
)
from scripts.observability.local_observe.e2e_audit import (  # noqa: E402
    ObserveCapture,
    audit_provider_attempts,
)
from scripts.repl.e2e.deterministic_openai_server import (  # noqa: E402
    ALIYUN_PROMPT_MARKER,
    FINAL_RESPONSE,
    FIXTURE_MODEL,
    DeterministicOpenAIServer,
)
from scripts.repl.e2e.run_contract_scenarios import (  # noqa: E402
    EXPECTED_BODY,
    HEADER_SENTINEL,
    TELEMETRY_MODEL,
)

SCENARIO = "e4-web-normal-contract-reload"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real Web contract E2E scenario.")
    parser.add_argument("--run-dir", default="")
    parser.add_argument(
        "--run-root",
        default=str(Path(tempfile.gettempdir()) / "iac-code-web-e2e-runs" / "contract"),
    )
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--skip-browser", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    config_dir = run_dir / "config"
    config_dir.mkdir(exist_ok=True)
    provider_capture_path = run_dir / "provider-requests.jsonl"
    aliyun_capture_path = run_dir / "aliyun-transport.jsonl"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    checks: dict[str, bool] = {}
    notes: list[str] = []
    public_payloads: list[Any] = []
    manifest: dict[str, Any] = {
        "scenario": SCENARIO,
        "entry": "web",
        "mode": "normal",
        "provider": "openai_compatible",
        "model": FIXTURE_MODEL,
        "expected_provider_attempts": 3,
        "process_epochs": [],
        "base_url": base_url,
    }

    observe = ObserveCapture(run_dir / "telemetry").start()
    provider = DeterministicOpenAIServer(provider_capture_path).start()
    server: subprocess.Popen[str] | None = None
    first_transcript: dict[str, Any] | None = None
    resumed_transcript: dict[str, Any] | None = None
    try:
        _write_settings(config_dir, provider.base_url)
        env = _child_env(
            config_dir=config_dir,
            workspace=workspace,
            observe=observe,
            aliyun_capture_path=aliyun_capture_path,
        )
        server = _start_web_server(args, run_dir, port, env, epoch="epoch-1")
        _wait_for_health(base_url, server, timeout=args.timeout)

        created = _json_request(
            base_url,
            "POST",
            "/api/sessions",
            {
                "cwd": str(workspace),
                "mode": "normal",
                "provider": "openai_compatible",
                "model": FIXTURE_MODEL,
                "permissionMode": "danger",
            },
        )
        public_payloads.append(created)
        web_session_id = str(created["webSessionId"])
        session_id = str(created["sessionId"])
        manifest.update({"web_session_id": web_session_id, "session_id": session_id})

        accepted = _json_request(
            base_url,
            "POST",
            _session_path(web_session_id, "/messages"),
            {"text": "请调用 aliyun_api 查询 VPC，只使用工具结果回答。" + ALIYUN_PROMPT_MARKER},
        )
        public_payloads.append(accepted)
        checks["Web accepted real turn"] = accepted.get("accepted") is True
        first_transcript = _wait_for_transcript(base_url, web_session_id, FINAL_RESPONSE, timeout=args.timeout)
        public_payloads.append(first_transcript)
        checks["live Web transcript rendered final response"] = _contains_text(first_transcript, FINAL_RESPONSE)
        checks["live Web transcript rendered aliyun_api"] = _contains_text(first_transcript, "aliyun_api")
        if not args.skip_browser:
            _verify_browser(
                base_url=base_url,
                session_id=web_session_id,
                expected_text=FINAL_RESPONSE,
                screenshot=run_dir / "epoch-1-browser.png",
            )
            checks["live browser DOM rendered final response"] = True

        _stop_web_server(server, timeout=args.timeout)
        server = None
        epoch1_records = observe.wait_for(
            lambda records: _terminal_count(records) >= 2 and _has_request_metrics(records),
            timeout=args.timeout,
        )
        epoch1_end = len(epoch1_records)
        manifest["process_epochs"].append({"name": "initial", "record_start": 0, "record_end": epoch1_end})

        session_path, tool_result = find_latest_aliyun_tool_result(config_dir)
        manifest["session_path"] = str(session_path)

        server = _start_web_server(args, run_dir, port, env, epoch="epoch-2")
        _wait_for_health(base_url, server, timeout=args.timeout)
        resumed_transcript = _json_request(base_url, "GET", _session_path(web_session_id, "/messages"))
        public_payloads.append(resumed_transcript)
        checks["reload restored equivalent transcript"] = resumed_transcript == first_transcript
        if not args.skip_browser:
            _verify_browser(
                base_url=base_url,
                session_id=web_session_id,
                expected_text=FINAL_RESPONSE,
                screenshot=run_dir / "epoch-2-browser.png",
            )
            checks["reloaded browser DOM rendered final response"] = True

        followup = _json_request(
            base_url,
            "POST",
            _session_path(web_session_id, "/messages"),
            {"text": "请只回复恢复成功 [E4_RELOAD]"},
        )
        public_payloads.append(followup)
        final_transcript = _wait_for_provider_count_and_transcript(
            base_url,
            web_session_id,
            provider,
            expected_requests=3,
            expected_text=FINAL_RESPONSE,
            timeout=args.timeout,
        )
        public_payloads.append(final_transcript)
        checks["reload follow-up used same session"] = len(provider.requests()) == 3

        _stop_web_server(server, timeout=args.timeout)
        server = None
        all_records = observe.wait_for(
            lambda records: _terminal_count(records[epoch1_end:]) >= 1 and _has_request_metrics(records[epoch1_end:]),
            timeout=args.timeout,
        )
        epoch2_records = all_records[epoch1_end:]
        manifest["process_epochs"].append(
            {"name": "reload", "record_start": epoch1_end, "record_end": len(all_records)}
        )

        resumed_session_path, resumed_tool_result = find_latest_aliyun_tool_result(config_dir)
        checks["reload selected the same persisted session"] = resumed_session_path == session_path
        provider_requests = provider.requests()
        public_payloads.append(provider_requests)
        contract = audit_aliyun_result_contract(
            expected_body=EXPECTED_BODY,
            tool_result_content=tool_result["content"],
            tool_result_metadata=tool_result.get("metadata") or {},
            resumed_content=resumed_tool_result["content"],
            resumed_metadata=resumed_tool_result.get("metadata") or {},
            public_payloads=public_payloads,
            forbidden_values=[HEADER_SENTINEL],
            output_path=run_dir / "aliyun-contract-audit.json",
        )
        checks["Web Aliyun result contract"] = contract["passed"]

        telemetry1 = audit_provider_attempts(
            all_records[:epoch1_end],
            expected_attempts=2,
            expected_provider="openai",
            expected_model=TELEMETRY_MODEL,
            expected_span_attributes={"iac_code.mode": "normal", "gen_ai.session.id": f"iac_sess_{session_id}"},
        )
        telemetry2 = audit_provider_attempts(
            epoch2_records,
            expected_attempts=1,
            expected_provider="openai",
            expected_model=TELEMETRY_MODEL,
            expected_span_attributes={"iac_code.mode": "normal", "gen_ai.session.id": f"iac_sess_{session_id}"},
        )
        telemetry = {
            "passed": telemetry1["passed"] and telemetry2["passed"],
            "process_epochs": {"initial": telemetry1, "reload": telemetry2},
        }
        (run_dir / "telemetry-audit.json").write_text(
            json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        checks["Web provider attempt telemetry"] = telemetry["passed"]
    except Exception as exc:
        notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        if server is not None:
            _stop_web_server(server, timeout=10.0)
        provider.stop()
        records = observe.stop()
        manifest["telemetry_record_count"] = len(records)
        (run_dir / "scenario-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if first_transcript is not None:
            (run_dir / "epoch-1-messages.json").write_text(
                json.dumps(first_transcript, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if resumed_transcript is not None:
            (run_dir / "epoch-2-messages.json").write_text(
                json.dumps(resumed_transcript, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    passed = bool(checks) and all(checks.values()) and not notes
    summary = {"scenario": SCENARIO, "passed": passed, "checks": checks, "notes": notes, "run_dir": str(run_dir)}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


def _run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    return (Path(args.run_root).expanduser().resolve() / f"{int(time.time())}-{os.getpid()}").resolve()


def _write_settings(config_dir: Path, provider_base_url: str) -> None:
    (config_dir / "settings.yml").write_text(
        "\n".join(
            [
                "activeProvider: openai_compatible",
                "providers:",
                "  openai_compatible:",
                f"    model: {FIXTURE_MODEL}",
                f"    apiBase: {provider_base_url}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _child_env(
    *,
    config_dir: Path,
    workspace: Path,
    observe: ObserveCapture,
    aliyun_capture_path: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    bootstrap = REPO_ROOT / "scripts" / "aliyun" / "e2e_fixture_bootstrap"
    env.update(observe.env)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(item for item in [str(bootstrap), env.get("PYTHONPATH", "")] if item),
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_CWD": str(workspace),
            "IAC_CODE_MODE": "normal",
            "IAC_CODE_PROVIDER": "openai_compatible",
            "IAC_CODE_MODEL": FIXTURE_MODEL,
            "IAC_CODE_API_KEY": "e2e-provider-key",
            "IAC_CODE_E2E_ALIYUN_TRANSPORT_FIXTURE": "1",
            "IAC_CODE_E2E_ALIYUN_CAPTURE": str(aliyun_capture_path),
            "IAC_CODE_E2E_ALIYUN_HEADER_SENTINEL": HEADER_SENTINEL,
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "e2e-access-key-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "e2e-access-key-secret",
            "ALIBABA_CLOUD_REGION_ID": "cn-hangzhou",
        }
    )
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_web_server(
    args: argparse.Namespace,
    run_dir: Path,
    port: int,
    env: dict[str, str],
    *,
    epoch: str,
) -> subprocess.Popen[str]:
    command = shlex.split(args.python) + [
        "-m",
        "iac_code.cli.main",
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-open",
    ]
    stdout = (run_dir / f"{epoch}-server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stdout.close()
    return process


def _stop_web_server(process: subprocess.Popen[str], *, timeout: float) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _wait_for_health(base_url: str, process: subprocess.Popen[str], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Web server exited early with code {process.returncode}")
        try:
            payload = _json_request(base_url, "GET", "/health")
            if payload.get("status") == "ok":
                return
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for Web health: {last_error}")


def _json_request(base_url: str, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        base_url + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _session_path(session_id: str, suffix: str = "") -> str:
    return f"/api/sessions/{quote(session_id, safe='')}{suffix}"


def _wait_for_transcript(
    base_url: str,
    session_id: str,
    expected_text: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _json_request(base_url, "GET", _session_path(session_id, "/messages"))
        if _contains_text(latest, expected_text):
            return latest
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for Web transcript text {expected_text!r}")


def _wait_for_provider_count_and_transcript(
    base_url: str,
    session_id: str,
    provider: DeterministicOpenAIServer,
    *,
    expected_requests: int,
    expected_text: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _json_request(base_url, "GET", _session_path(session_id, "/messages"))
        if len(provider.requests()) == expected_requests and _contains_text(latest, expected_text):
            return latest
        time.sleep(0.1)
    raise TimeoutError("timed out waiting for the Web reload follow-up")


def _contains_text(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_text(item, expected) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_text(item, expected) for item in value)
    return isinstance(value, str) and expected in value


def _verify_browser(*, base_url: str, session_id: str, expected_text: str, screenshot: Path) -> None:
    script = REPO_ROOT / "scripts" / "web" / "e2e" / "verify_contract_dom.mjs"
    subprocess.run(
        [
            "node",
            str(script),
            "--url",
            base_url,
            "--sessionId",
            session_id,
            "--expectedText",
            expected_text,
            "--screenshot",
            str(screenshot),
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )


def _terminal_count(records: list[dict[str, Any]]) -> int:
    return sum(
        record.get("kind") == "log" and record.get("name") in {"iac.api.request.succeeded", "iac.api.request.failed"}
        for record in records
    )


def _has_request_metrics(records: list[dict[str, Any]]) -> bool:
    names = {record.get("name") for record in records if record.get("kind") == "metric"}
    return {"iac.api.request.count", "iac.api.request.duration"}.issubset(names)


if __name__ == "__main__":
    raise SystemExit(main())
