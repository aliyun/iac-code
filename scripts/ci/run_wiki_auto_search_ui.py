#!/usr/bin/env python3
"""Local CLI/Web Wiki display acceptance, using the existing PTY and DOM drivers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pexpect

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from scripts.ci.wiki_auto_model import WikiModel  # noqa: E402
from scripts.repl.e2e.run_contract_scenarios import (  # noqa: E402
    _exit_repl,
    _expect_repl_input_ready,
    _write_pty_artifacts,
)
from scripts.repl.e2e.run_pipeline_scenarios import ReplPty  # noqa: E402
from scripts.web.e2e.run_contract_scenario import (  # noqa: E402
    _free_port,
    _json_request,
    _session_path,
    _start_web_server,
    _stop_web_server,
    _verify_browser,
    _wait_for_health,
    _wait_for_transcript,
)


def run(args):
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config, workspace, home = (root / name for name in ("config", "workspace", "home"))
    for path in (config, workspace, home):
        path.mkdir(exist_ok=True, mode=0o700)
    checks = {}
    with WikiModel() as model:
        (config / "settings.yml").write_text(
            "activeProvider: openai_compatible\nlanguage: en\nmodelFallbackEnabled: false\nproviders:\n"
            "  openai_compatible:\n    model: gpt-4o-mini\n    apiBase: " + model.url + "\n",
            encoding="utf-8",
        )
        blocked = ("IAC_CODE_", "ALIBABA_CLOUD_", "ALIYUN_", "OPENAI_", "ANTHROPIC_", "DASHSCOPE_")
        env = {key: value for key, value in os.environ.items() if not key.startswith(blocked)}
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PYTHONPATH": str(REPO / "src"),
                "IAC_CODE_CONFIG_DIR": str(config),
                "IAC_CODE_CWD": str(workspace),
                "IAC_CODE_MODE": "normal",
                "IAC_CODE_PROVIDER": "openai_compatible",
                "IAC_CODE_MODEL": "gpt-4o-mini",
                "IAC_CODE_API_KEY": "fixture-key",
                "IAC_CODE_BASE_URL": model.url,
                "IAC_CODE_WIKI_AUTO_SEARCH": "true",
                "IAC_CODE_AUTO_MEMORY": "false",
                "IAC_CODE_TELEMETRY_E2E_USER_ID": "e2e-wiki-auto-ui",
                "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
            }
        )
        if args.surface == "web":
            port = _free_port()
            url = f"http://127.0.0.1:{port}"
            options = SimpleNamespace(python=sys.executable)
            process = None
            try:
                process = _start_web_server(options, root, port, env, epoch="initial")
                _wait_for_health(url, process, timeout=30)
                session = _json_request(
                    url,
                    "POST",
                    "/api/sessions",
                    {
                        "cwd": str(workspace),
                        "mode": "normal",
                        "provider": "openai_compatible",
                        "model": "gpt-4o-mini",
                    },
                )["webSessionId"]
                _json_request(url, "POST", _session_path(session, "/messages"), {"text": "What is Intel B70?"})
                before = _wait_for_transcript(url, session, "main answer", timeout=30)
                _verify_browser(
                    base_url=url,
                    session_id=session,
                    expected_text="main answer",
                    screenshot=root / "initial.png",
                    audit=root / "initial-dom.json",
                    require_wiki=True,
                )
                checks["real browser renders Wiki navigation"] = True
                before_reload = len(model.requests)
                _stop_web_server(process, timeout=30)
                process = _start_web_server(options, root, port, env, epoch="reloaded")
                _wait_for_health(url, process, timeout=30)
                after = _json_request(url, "GET", _session_path(session, "/messages"))
                checks["cold Web history keeps the original transcript"] = before == after
                _verify_browser(
                    base_url=url,
                    session_id=session,
                    expected_text="main answer",
                    screenshot=root / "reloaded.png",
                    audit=root / "reloaded-dom.json",
                    require_wiki=True,
                )
                checks["real browser restores one static Wiki summary"] = True
                checks["opening browser history does not repeat model calls"] = len(model.requests) == before_reload
                checks["only original input extracts keywords"] = (
                    sum(r["kind"] == "helper" for r in model.requests) == 1
                )
            finally:
                if process is not None:
                    _stop_web_server(process, timeout=30)
        else:
            options = SimpleNamespace(
                python=sys.executable,
                timeout=30,
                terminal_height=40,
                terminal_width=140,
                permission_prompt_response="enter",
            )
            for epoch in ("initial", "restored"):
                pty = ReplPty(args=options, run_dir=root, cwd=workspace, env=env)
                try:
                    pty.spawn(extra_args=["--continue"] if epoch == "restored" else [])
                    _expect_repl_input_ready(pty, description=epoch + " prompt", timeout=30)
                    if epoch == "initial":
                        pty.sendline("What is Intel B70?")
                        pty.expect_any(("main answer",), description="main reply", timeout=30)
                    else:
                        checks["CLI history keeps Wiki without model replay"] = (
                            "Wiki" in pty.transcript and "main answer" in pty.transcript and len(model.requests) == 2
                        )
                    checks[epoch + " CLI renders Wiki summary"] = "Wiki" in pty.transcript
                    if epoch == "restored":
                        pty.sendline("/exit")  # Its ready prompt was already consumed above.
                        pty.child.expect(pexpect.EOF, timeout=30)
                    else:
                        _exit_repl(pty, timeout=30)
                finally:
                    _write_pty_artifacts(pty, root, epoch)
                    pty.terminate()
    return {"passed": bool(checks) and all(checks.values()), "surface": args.surface, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("web", "cli"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:
        result = {"passed": False, "surface": args.surface, "error_type": type(error).__name__}
    args.run_dir.mkdir(parents=True, exist_ok=True)
    (args.run_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
