#!/usr/bin/env python3
"""Serve the production Wiki Web UI fixture for an external browser driver.

Only model responses are deterministic. No Playwright/browser control here.
Touch RUN_DIR/restart to exercise cold history recovery before browser reload.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from scripts.ci.wiki_auto_model import WikiModel  # noqa: E402
from scripts.web.e2e.run_contract_scenario import (  # noqa: E402
    _free_port,
    _json_request,
    _session_path,
    _start_web_server,
    _stop_web_server,
    _wait_for_health,
    _wait_for_transcript,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    root = parser.parse_args().run_dir.resolve()
    for name in ("config", "workspace", "home"):
        (root / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    with WikiModel() as model:
        (root / "config/settings.yml").write_text(
            "activeProvider: openai_compatible\nlanguage: en\nmodelFallbackEnabled: false\nproviders:\n"
            "  openai_compatible:\n    model: gpt-4o-mini\n    apiBase: " + model.url + "\n", encoding="utf-8")
        prefixes = ("IAC_CODE_", "ALIBABA_CLOUD_", "ALIYUN_", "OPENAI_", "ANTHROPIC_", "DASHSCOPE_")
        env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}
        env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"),
                   PYTHONPATH=str(REPO / "src"), IAC_CODE_CONFIG_DIR=str(root / "config"),
                   IAC_CODE_MODE="normal", IAC_CODE_PROVIDER="openai_compatible", IAC_CODE_MODEL="gpt-4o-mini",
                   IAC_CODE_API_KEY="fixture-key", IAC_CODE_BASE_URL=model.url, IAC_CODE_WIKI_AUTO_SEARCH="true",
                   IAC_CODE_AUTO_MEMORY="false", IAC_CODE_TELEMETRY_E2E_USER_ID="e2e-retention-wiki-browser",
                   IAC_CODE_TELEMETRY_LOCAL_ONLY="1")
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        options = SimpleNamespace(python=sys.executable)
        process = _start_web_server(options, root, port, env, epoch="initial")
        try:
            _wait_for_health(url, process, timeout=45)
            session = _json_request(url, "POST", "/api/sessions", {
                "cwd": str(root / "workspace"), "mode": "normal", "provider": "openai_compatible",
                "model": "gpt-4o-mini"})["webSessionId"]
            _json_request(url, "POST", _session_path(session, "/messages"), {"text": "What is Intel B70?"})
            before = _wait_for_transcript(url, session, "main answer", timeout=45)
            descriptor = {"url": url, "session": session, "model_mocked": True}
            (root / "browser-environment.json").write_text(json.dumps(descriptor), encoding="utf-8")
            print(json.dumps(descriptor), flush=True)
            while not (root / "stop").exists():
                if (root / "restart").exists():
                    (root / "restart").unlink()
                    count = len(model.requests)
                    _stop_web_server(process, timeout=30)
                    process = _start_web_server(options, root, port, env, epoch="restored")
                    _wait_for_health(url, process, timeout=45)
                    after = _json_request(url, "GET", _session_path(session, "/messages"))
                    checks = {"cold history identical": before == after,
                              "no model replay after server restart": count == len(model.requests)}
                    (root / "restart-result.json").write_text(json.dumps(checks), encoding="utf-8")
                time.sleep(0.2)
        finally:
            _stop_web_server(process, timeout=30)


if __name__ == "__main__":
    main()
