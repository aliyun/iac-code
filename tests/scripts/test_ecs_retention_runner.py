"""Offline assertions for regression evidence; never calls a model or cloud."""

import copy
import json
from types import SimpleNamespace

import pytest

from scripts.ci.run_ecs_retention_a2a import (
    SCENARIOS,
    alias_checks,
    pricing_checks,
    routing_checks,
    routing_observations,
    successful_api,
)


def test_cloud_safety_uses_the_operation_not_only_a_group_tool_name():
    from scripts.ci.run_ecs_retention_a2a import is_readonly_call

    assert is_readonly_call({"name": "ros_resource_type_registration", "input": {"action": "GetResourceType"}})
    assert not is_readonly_call({"name": "ros_resource_type_registration", "input": {"action": "RegisterResourceType"}})
    assert not is_readonly_call({"name": "ros_deploy", "input": {}})
    assert not is_readonly_call({"name": "aliyun_api", "input": {"action": "RunInstances"}})


def test_permission_driver_only_reads_this_runs_generated_tool_result(tmp_path):
    from scripts.ci.run_ecs_retention_a2a import may_read_generated_result

    result = tmp_path / "transcript" / "tool-results" / "query.txt"
    result.parent.mkdir(parents=True)
    result.write_text("large read-only query response", encoding="utf-8")
    permission = {"isReadOnly": True, "toolName": "grep", "toolUseId": "query"}
    calls = [{"id": "query", "input": {"path": str(result)}}]
    assert may_read_generated_result(permission, calls, tmp_path)
    assert not may_read_generated_result(permission | {"isReadOnly": False}, calls, tmp_path)
    assert not may_read_generated_result(permission | {"toolName": "aliyun_api"}, calls, tmp_path)
    assert not may_read_generated_result(permission, calls, tmp_path / "different-run")


@pytest.mark.parametrize("final_kind,expected", [("permission", "failed"), ("ask_user_question", "passed"),
                                               ("completed", "passed")])
def test_noncloud_runner_cannot_pass_while_permission_is_still_pending(monkeypatch, tmp_path, final_kind, expected):
    from scripts.ci import a2a_context_evidence as evidence
    from scripts.ci import run_ecs_retention_a2a as runner

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))

    class Server:
        def __init__(self, **kwargs):
            self.process = SimpleNamespace(poll=lambda: 0)

        def start(self):
            pass

        def terminate(self):
            pass

    class Capture:
        env = {}

        def __init__(self, *args):
            pass

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "fixture"}}]

    calls = []

    def stream(**kwargs):
        key = "read-" + str(len(calls))
        calls.append({"id": key, "name": "read_file", "input": {"path": str(tmp_path / "outside-result")}})
        kind = final_kind if len(calls) == 9 else "permission"
        event = {"metadata": {"iac_code": {"input": {
            "kind": kind, "inputId": key, "toolName": "read_file", "toolUseId": key, "isReadOnly": True,
        }}}}
        (tmp_path / (kwargs["name"] + ".events.jsonl")).write_text(json.dumps(event) + "\n", encoding="utf-8")
        return SimpleNamespace(context_id="ctx", task_id="task", normal_handoff_ready=False,
                               response_content_type="text/event-stream", text="",
                               last_status_state=("TASK_STATE_COMPLETED" if kind == "completed"
                                                  else "TASK_STATE_INPUT_REQUIRED"))

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(runner, "wait_for_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_free_port", lambda _: 12345)
    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(evidence, "stream_message", stream)
    monkeypatch.setattr(runner, "tool_inputs", lambda _: calls)
    monkeypatch.setattr(evidence, "tool_inputs", lambda _: calls)
    monkeypatch.setattr(evidence, "_permission_response_query", lambda permission, decision: decision)
    monkeypatch.setattr(runner, "results", lambda _: {})
    monkeypatch.setattr(runner, "envelopes", lambda _: [])
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: "")
    monkeypatch.setattr(runner, "final_text", lambda _: "")
    report = runner.run(SimpleNamespace(scenario="noncloud-purchase", run_dir=tmp_path, python="fixture",
                                       provider="fixture", model="fixture", timeout=1))
    assert report["status"] == expected
    assert len(calls) == 9
    if final_kind == "permission":
        assert report["checks"]["scenario completed"] is False


def test_missing_alias_execution_cannot_pass_comparison():
    assert not any(alias_checks([], {}).values())


def _alias_quotes():
    calls, outcomes = [], {}
    for number, (action, params, price) in enumerate([
        ("DescribePrice", {"PriceUnit": "Hour", "SystemDisk.Size": 40, "SystemDisk.Category": "cloud_essd"}, 1),
        ("DescribePrices", {"PriceUnit": "Hour", "SystemDisk.Capacity": 40, "SystemDisk.1.Category": "cloud_essd"}, 1),
        ("DescribePrice", {"PriceUnit": "Month", "SystemDisk.Size": 40, "SystemDisk.Category": "cloud_essd"}, 20),
        ("DescribePrice", {"PriceUnit": "Monthly", "SystemDisk.Size": 40, "SystemDisk.Category": "cloud_essd"}, 20),
        ("DescribePrice", {"PeriodType": "Monthly", "SystemDisk.Size": 40, "SystemDisk.Category": "cloud_essd"}, 20),
    ]):
        key = str(number)
        calls.append({"id": key, "name": "aliyun_api", "input": {"action": action, "params": params}})
        outcomes[key] = {"is_error": False, "content": json.dumps({"PriceInfo": {"Price": {"TradePrice": price}}})}
    return calls, outcomes


def test_alias_comparison_does_not_mix_different_resource_configurations():
    calls, outcomes = _alias_quotes()
    before = copy.deepcopy(calls)
    calls.append({"id": "different", "name": "aliyun_api", "input": {
        "action": "DescribePrices", "params": {"PriceUnit": "Hour", "SystemDisk.Capacity": 100},
    }})
    outcomes["different"] = {"is_error": False, "content": '{"PriceInfo":{"Price":{"TradePrice":3}}}'}
    assert all(alias_checks(calls, outcomes).values())
    assert calls[:-1] == before


@pytest.mark.parametrize("missing", [0, 1, 2, 3, 4])
def test_alias_comparison_requires_each_successful_standard_and_alias_call(missing):
    calls, outcomes = _alias_quotes()
    outcomes[calls[missing]["id"]]["is_error"] = True
    assert not all(alias_checks(calls, outcomes).values())


def test_duplicate_canonical_quotes_cannot_pass_alias_acceptance():
    calls, outcomes = _alias_quotes()
    canonical = [calls[0], copy.deepcopy(calls[0]), calls[2], copy.deepcopy(calls[2])]
    assert not all(alias_checks(canonical, outcomes).values())
    assert not any(ok for name, ok in alias_checks(canonical, outcomes).items()
                   if name.startswith("successful alias exercised:"))


def test_alias_price_mismatch_still_blocks_with_complete_coverage():
    calls, outcomes = _alias_quotes()
    outcomes["1"]["content"] = '{"PriceInfo":{"Price":{"TradePrice":99}}}'
    checks = alias_checks(calls, outcomes)
    assert all(ok for name, ok in checks.items() if name.startswith("successful alias exercised:"))
    assert not checks["hour standard and alias quotes agree"]


def test_knowledge_miss_accepts_virtual_and_physical_roots():
    from scripts.ci.run_knowledge_wiki_a2a import is_knowledge_location

    assert is_knowledge_location("knowledge://")
    assert is_knowledge_location("knowledge://common")
    assert is_knowledge_location("C:\\pkg\\knowledge\\clouds")
    assert is_knowledge_location("/pkg/knowledge")
    assert not is_knowledge_location("/unrelated/wiki")


def test_actual_success_is_required_for_price_evidence():
    call = {"id": "q", "name": "aliyun_api", "input": {
        "action": "DescribePrice", "params": {"SystemDisk.Size": 40},
    }}
    assert successful_api([call], {}) == []
    assert not pricing_checks([call], {"q": {"is_error": True}})["real ECS quote succeeded"]
    assert all(pricing_checks([call], {"q": {"is_error": False}}).values())


def test_a_successful_nonpricing_query_is_not_a_quote():
    call = {"id": "q", "name": "aliyun_api", "input": {"action": "DescribeInstanceTypes"}}
    assert not any(pricing_checks([call], {"q": {"is_error": False}}).values())


def test_quote_without_disk_fails_disk_coverage():
    call = {"id": "q", "name": "aliyun_api", "input": {"action": "DescribePrices", "params": {}}}
    checks = pricing_checks([call], {"q": {"is_error": False}})
    assert checks["real ECS quote succeeded"]
    assert not checks["successful quote includes system disk"]


def test_handoff_decodes_reserved_characters_once():
    answer = "https://ros.console.aliyun.com/cn-beijing/chat?prompt=web%2Ba%26b%231"
    assert all(routing_checks(answer, handoff=True).values())
    assert routing_observations(answer, "web+a&b#1")["literal_request_context_preserved"]
    assert not routing_observations(answer.replace("%2B", "+"), "web+a&b#1")["literal_request_context_preserved"]


def test_handoff_summary_may_omit_an_instance_name_without_failing_routing():
    answer = "https://ros.console.aliyun.com/cn-beijing/chat?prompt=ECS%E7%BB%AD%E8%B4%B9%E6%8A%98%E6%89%A3"
    assert all(routing_checks(answer, handoff=True, expected_region="cn-beijing").values())
    assert routing_observations(answer, "web+a&b#1") == {"literal_request_context_preserved": False}


def test_plain_link_does_not_prove_prompt_handoff():
    assert not all(routing_checks("https://ros.console.aliyun.com/cn-beijing/chat", handoff=True).values())
    assert all(routing_checks("请先说明需求", handoff=False).values())


def test_handoff_region_must_match_the_configured_test_region():
    answer = "https://ros.console.aliyun.com/cn-beijing/chat?prompt=renewal"
    assert all(routing_checks(answer, handoff=True, expected_region="cn-beijing").values())
    assert not all(routing_checks(answer, handoff=True, expected_region="cn-shanghai").values())


def test_regression_catalog_requires_cloud_readonly_and_separate_contexts():
    assert len(SCENARIOS) == 17
    assert SCENARIOS["n56-price-chain"].select and SCENARIOS["n59-price-chain"].select
    assert SCENARIOS["n29-swas"].product == "swas"
    assert SCENARIOS["api-aliases"].mode == "normal"
    assert SCENARIOS["route-disabled"].awareness is False


def test_all_new_cases_registered_with_main_ci_entry():
    from scripts.ci.run_e2e import RETENTION_CASES, parse_args, select_cases

    cases = select_cases(parse_args(["--suite", "live-retention", "--list"]))
    assert len(cases) == len(RETENTION_CASES) == 17
    assert all(not case.cloud_write for case in cases)


def test_cloud_helper_exports_sts_without_copying_rotating_oauth(tmp_path, monkeypatch):
    import os

    import yaml

    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
    from scripts.ci.cloud_test_credentials import export

    source = tmp_path / "durable"
    source.mkdir()
    original = source / ".cloud-credentials.yml"
    original.write_text("durable OAuth source", encoding="utf-8")
    monkeypatch.setenv("IAC_CODE_E2E_CLOUD_SOURCE", str(source))
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "initial-config"))
    credential = AliyunCredential(mode="OAuth", access_key_id="fixture-ak", access_key_secret="fixture-secret",
                                  sts_token="fixture-sts", oauth_refresh_token="fixture-rotating-token")
    monkeypatch.setattr(AliyunCredentials, "load", staticmethod(lambda: credential))
    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", staticmethod(lambda value: value))
    output = tmp_path / "case" / ".cloud-credentials.yml"
    export(output)
    data = yaml.safe_load(output.read_text(encoding="utf-8"))["aliyun"]
    assert data["mode"] == "StsToken" and data["sts_token"] == "fixture-sts"
    assert not any(key.startswith("oauth") for key in data)
    assert original.read_text(encoding="utf-8") == "durable OAuth source"
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_n59_explicit_pl0_is_not_satisfied_by_entry_or_default_performance(tmp_path):
    import yaml

    from scripts.ci.run_ecs_retention_a2a import requested_data_disk_preserved

    template = {"Parameters": {"DiskCategory": {"Default": "cloud_essd"}}, "Resources": {
        "Ecs": {"Type": "ALIYUN::ECS::Instance", "Properties": {"DiskMappings": [
            {"Category": {"Ref": "DiskCategory"}, "Size": 200, "PerformanceLevel": "PL0"},
        ]}},
    }}
    path = tmp_path / "template.yml"
    path.write_text(yaml.safe_dump(template), encoding="utf-8")
    conclusion = {"template_url": path.name}
    assert requested_data_disk_preserved(conclusion, tmp_path)
    assert not requested_data_disk_preserved(conclusion | {
        "effective_deployment_parameters": {"DiskCategory": "cloud_essd_entry"}}, tmp_path)
    template["Resources"]["Ecs"]["Properties"]["DiskMappings"][0].pop("PerformanceLevel")
    path.write_text(yaml.safe_dump(template), encoding="utf-8")
    assert not requested_data_disk_preserved(conclusion, tmp_path)
