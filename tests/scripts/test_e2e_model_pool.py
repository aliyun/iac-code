"""Offline concurrency, routing and isolated model policy contracts."""

from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path

import pytest
import yaml

from iac_code.providers.base import ContentBlock, Message, ToolDefinition
from iac_code.providers.manager import ProviderManager, create_provider
from iac_code.services.capabilities.multimodal import _builtin_multimodal_models
from scripts.ci import run_e2e
from scripts.ci.model_pool import MULTIMODAL_MODELS, TEXT_MODELS, ModelAssignment, scheduled_cases
from tests.providers._fakes import FakeOpenAIClient, ns


def case(name: str, *, multimodal: bool = False, lock: str = "") -> run_e2e.Case:
    return run_e2e.Case(name, "fake.py", (), 5, "live", resource_lock=lock, multimodal=multimodal)


def test_pool_routes_case_types_and_bounds_each_model() -> None:
    cases = [case(str(i), multimodal=i % 2 == 0) for i in range(32)]
    active: Counter[str] = Counter()
    peak: Counter[str] = Counter()
    mutex = threading.Lock()
    gate = threading.Event()
    full = threading.Event()
    assignments = []

    def execute(spec, assignment):
        assert assignment.model in (MULTIMODAL_MODELS if spec.multimodal else TEXT_MODELS)
        with mutex:
            active[assignment.model] += 1
            peak[assignment.model] = max(peak[assignment.model], active[assignment.model])
            if sum(active.values()) == 12:
                full.set()
        assert gate.wait(5)
        with mutex:
            active[assignment.model] -= 1

    def collect():
        for spec, assignment, future in scheduled_cases(cases, 16, execute):
            future.result()
            assignments.append((spec, assignment))

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert full.wait(5)
    finally:
        gate.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(assignments) == len(cases)
    assert all(peak[m] == 2 for m in TEXT_MODELS)
    assert all(peak[m] == 1 for m in MULTIMODAL_MODELS)


def test_scheduler_refills_without_waiting_for_slow_case_or_resource_lock() -> None:
    slow_gate = threading.Event()
    refilled = threading.Event()
    order = []

    def execute(spec, _assignment):
        order.append(spec.name)
        if spec.name == "slow":
            assert slow_gate.wait(5)
        if spec.name == "refill":
            refilled.set()

    def collect():
        for _spec, _assignment, future in scheduled_cases(
            [case("slow", lock="shared"), case("blocked", lock="shared"), case("fast"), case("refill")],
            2, execute, enabled=False,
        ):
            future.result()

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert refilled.wait(5)
        assert "blocked" not in order
    finally:
        slow_gate.set()
        thread.join(5)
    assert not thread.is_alive()
    assert order.index("refill") < order.index("blocked")


def test_failed_case_releases_capacity_and_prime_reserves_diagnosis_slot() -> None:
    cases = [case(str(i)) for i in range(5)]

    def fail(_spec, _assignment):
        raise ValueError("fixture")

    results = list(scheduled_cases(cases, 16, fail, text_models=("glm-5.3-prime",), text_jobs=3))
    assert len(results) == 5
    assert all(isinstance(future.exception(), ValueError) for _, _, future in results)


def test_catalog_classifies_images_from_scenario_contracts() -> None:
    for spec in run_e2e.LIVE_CASES:
        if spec.live_runner == "selling":
            original = next(item for item in run_e2e.SELLING_SCENARIOS if spec.name == "ssf-" + item.name)
            assert spec.multimodal is original.multimodal
        elif spec.live_runner == "repl":
            assert spec.multimodal is (spec.args[1] in run_e2e.REPL_MULTIMODAL_SCENARIOS)
        elif spec.live_runner.startswith("legacy_a2a"):
            assert spec.multimodal is (spec.args[1] in run_e2e.A2A_MULTIMODAL_SCENARIOS)
        else:
            assert not spec.multimodal
    assert run_e2e.parse_args(["--suite", "live", "--list"]).jobs == 12
    assert run_e2e.parse_args(["--suite", "full", "--list"]).jobs == 3


@pytest.mark.parametrize("model", TEXT_MODELS + MULTIMODAL_MODELS)
def test_low_thinking_policy_reaches_real_provider_wire(model, tmp_path) -> None:
    path = tmp_path / "settings.yml"
    path.write_text(json.dumps({
        "userID": "iac_user_e2e_fixture", "activeProvider": "dashscope",
        "providers": {"dashscope": {"effort": "xhigh", "thinkingBudget": 32768,
                                      "models": {model: {"effort": "max", "thinkingBudget": 65536}}}},
    }), encoding="utf-8")
    assignment = ModelAssignment(model, model in MULTIMODAL_MODELS)
    run_e2e._prepare_model_settings(path, assignment)
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = settings["providers"]["dashscope"]
    provider = create_provider(model, {"dashscope": "fake"}, provider_key_override="dashscope",
                               provider_config_override=config)
    wire = provider._build_thinking_kwargs()
    if assignment.thinking_budget:
        assert wire["extra_body"]["thinking_budget"] == 2048
        assert "reasoning_effort" not in wire
    else:
        assert wire["reasoning_effort"] == "low"
        assert "thinking_budget" not in wire.get("extra_body", {})
    assert config["modelFallbackEnabled"] is False
    assert settings["userID"] == "iac_user_e2e_fixture"
    assert "qwen3.8-omni-flash" in _builtin_multimodal_models()


def test_pinned_model_disables_degradation_and_refusal_fallback() -> None:
    manager = ProviderManager(model="deepseek-v4.1-flash", credentials={}, provider_key_override="dashscope",
                              provider_config_override={"modelFallbackEnabled": False})
    assert manager._get_fallback_model("deepseek-v4.1-flash", "dashscope") is None
    assert manager._get_refusal_fallback_model("claude-opus-5", "anthropic") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", TEXT_MODELS + MULTIMODAL_MODELS)
async def test_stream_requests_keep_low_policy_with_tools_and_image(model, tmp_path) -> None:
    path = tmp_path / "settings.yml"
    path.write_text('{"activeProvider":"dashscope"}', encoding="utf-8")
    assignment = ModelAssignment(model, model in MULTIMODAL_MODELS)
    run_e2e._prepare_model_settings(path, assignment)
    provider = create_provider(model, {"dashscope": "fake"}, provider_key_override="dashscope",
                               provider_config_override=yaml.safe_load(path.read_text(encoding="utf-8"))["providers"]["dashscope"])
    client = FakeOpenAIClient(stream_chunks=[ns(
        usage=ns(prompt_tokens=1, completion_tokens=1),
        choices=[ns(finish_reason="stop", delta=ns(content="ok", tool_calls=None))],
    )])
    provider._client = client
    blocks = [ContentBlock(type="text", text="fixture")]
    if assignment.multimodal:
        blocks.append(ContentBlock(type="image", media_type="image/png", data="ZmFrZQ=="))
    tools = [ToolDefinition("fixture", "Fixture tool", {"type": "object"})]
    _ = [event async for event in provider.stream([Message("user", blocks)], "", tools)]
    request = client.chat.completions.calls[0]
    assert request["model"] == model
    assert request["tools"][0]["function"]["name"] == "fixture"
    if assignment.thinking_budget:
        assert request["extra_body"]["thinking_budget"] == 2048
        assert "reasoning_effort" not in request
    else:
        assert request["reasoning_effort"] == "low"
    if assignment.multimodal:
        assert any(block.get("type") == "image_url" for block in request["messages"][0]["content"])


@pytest.mark.parametrize("runner", ["selling", "canary", "selector", "repl", "legacy_a2a", "smoke"])
def test_assignment_is_inherited_by_every_live_adapter(tmp_path: Path, monkeypatch, runner) -> None:
    script = tmp_path / "fake.py"
    script.write_text(
        "import json, os, sys\nfrom pathlib import Path\nimport yaml\n"
        "assert os.environ['IAC_CODE_MODEL'] == 'glm-5.2-fast-preview'\n"
        "option = '--credential-source-dir' if '--credential-source-dir' in sys.argv else '--source-config-dir'\n"
        "config = Path(sys.argv[sys.argv.index(option)+1]) if option in sys.argv else "
        "Path(os.environ['IAC_CODE_CONFIG_DIR'])\n"
        "settings = yaml.safe_load((config/'settings.yml').read_text(encoding='utf-8'))\n"
        "assert settings['providers']['dashscope']['model'] == 'glm-5.2-fast-preview'\n"
        "assert settings['providers']['dashscope']['effort'] == 'low'\n"
        "assert 'e2e' in settings['userID']\n"
        "if '--model' in sys.argv: assert sys.argv[sys.argv.index('--model')+1] == 'glm-5.2-fast-preview'\n"
        "if '--concurrency' in sys.argv: assert sys.argv[sys.argv.index('--concurrency')+1] == '1'\n"
        "if '--provider' in sys.argv: assert '--model' in sys.argv\n"
        "run = Path(sys.argv[sys.argv.index('--run-dir')+1]); run.mkdir(parents=True, exist_ok=True)\n"
        "(run/'summary.json').write_text(json.dumps({'passed':True, 'checks':{'ok':True}}), encoding='utf-8')\n"
    , encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    for filename in (".credentials.yml", ".cloud-credentials.yml"):
        (source / filename).write_text("fake-key", encoding="utf-8")
    original = '{"activeProvider":"dashscope", "userID":"iac_user_e2e_original"}'
    (source / "settings.yml").write_text(original, encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    spec = run_e2e.Case("fixture", "fake.py", (), 5, "live", live_runner=runner)
    result = run_e2e.run_case(spec, tmp_path / "report", source,
                               model_assignment=ModelAssignment("glm-5.2-fast-preview", False))
    assert result["status"] == "passed"
    assert result["model"] == "glm-5.2-fast-preview"
    assert result["reasoningEffort"] == "low"
    assert (source / "settings.yml").read_text(encoding="utf-8") == original
    run_e2e._write_reports(tmp_path / "report", [result], 1)
    assert "glm-5.2-fast-preview / low" in (tmp_path / "report/report.md").read_text(encoding="utf-8")
    assert "glm-5.2-fast-preview / low" in (tmp_path / "report/report.html").read_text(encoding="utf-8")
    assert 'name="model" value="glm-5.2-fast-preview"' in (tmp_path / "report/junit.xml").read_text(encoding="utf-8")
