"""Tests for the candidate sub-pipeline read-only tool result cache."""

from iac_code.pipeline.engine.candidate_tool_cache import (
    CandidateToolResultCache,
    input_fingerprint,
    is_cacheable_tool,
)


def test_input_fingerprint_is_stable_across_key_order():
    assert input_fingerprint({"product": "ROS", "action": "GetResourceType"}) == input_fingerprint(
        {"action": "GetResourceType", "product": "ROS"}
    )


def test_input_fingerprint_differs_for_different_parameters():
    assert input_fingerprint({"action": "GetResourceType", "type": "NAS"}) != input_fingerprint(
        {"action": "GetResourceType", "type": "InstanceGroup"}
    )


def test_read_only_aliyun_actions_are_cacheable():
    assert is_cacheable_tool("aliyun_api", {"action": "GetResourceType"}) is True
    assert is_cacheable_tool("aliyun_api", {"action": "DescribeRegions"}) is True
    assert is_cacheable_tool("read_file", {"path": "reference.md"}) is True


def test_mutating_tools_are_not_cacheable():
    assert is_cacheable_tool("aliyun_api", {"action": "CreateStack"}) is False
    assert is_cacheable_tool("aliyun_api", {}) is False
    assert is_cacheable_tool("ros_deploy", {"action": "create"}) is False
    assert is_cacheable_tool("write_file", {"path": "template.yaml"}) is False


def test_record_and_replay_scoped_to_candidate_and_sub_step():
    cache = CandidateToolResultCache()

    assert (
        cache.record(
            candidate_index=0,
            sub_step_id="template_generating",
            tool_name="aliyun_api",
            tool_input={"action": "GetResourceType", "type": "ALIYUN::NAS::FileSystem"},
            result={"result": "nas-schema"},
        )
        is True
    )

    assert cache.precompleted_tools_for(0, "template_generating") == {"aliyun_api": {"result": "nas-schema"}}
    assert cache.precompleted_tools_for(1, "template_generating") == {}
    assert cache.precompleted_tools_for(0, "cost_estimating") == {}


def test_record_rejects_non_cacheable_tool_and_non_dict_result():
    cache = CandidateToolResultCache()

    assert (
        cache.record(
            candidate_index=0,
            sub_step_id="template_generating",
            tool_name="write_file",
            tool_input={"path": "template.yaml"},
            result={"file_path": "template.yaml"},
        )
        is False
    )
    assert (
        cache.record(
            candidate_index=0,
            sub_step_id="template_generating",
            tool_name="read_file",
            tool_input={"path": "reference.md"},
            result="plain text",
        )
        is False
    )
    assert cache.cached_result_count() == 0


def test_distinct_parameters_are_cached_separately():
    cache = CandidateToolResultCache()
    for resource_type in ("ALIYUN::NAS::FileSystem", "ALIYUN::ECS::InstanceGroup"):
        cache.record(
            candidate_index=0,
            sub_step_id="template_generating",
            tool_name="aliyun_api",
            tool_input={"action": "GetResourceType", "type": resource_type},
            result={"result": resource_type},
        )

    assert cache.cached_result_count(candidate_index=0) == 2


def test_snapshot_round_trip_preserves_replayable_entries():
    cache = CandidateToolResultCache()
    cache.record(
        candidate_index=1,
        sub_step_id="cost_estimating",
        tool_name="ros_estimate_template_cost",
        tool_input={"template_url": "oss://template.yaml"},
        result={"result": "120 CNY"},
    )

    restored = CandidateToolResultCache.from_snapshot(cache.to_snapshot())

    assert restored.precompleted_tools_for(1, "cost_estimating") == {
        "ros_estimate_template_cost": {"result": "120 CNY"}
    }


def test_from_snapshot_ignores_malformed_entries():
    restored = CandidateToolResultCache.from_snapshot(
        {
            "bad-no-tool": {"candidate_index": 0, "sub_step_id": "s", "result": {}},
            "bad-index": {"candidate_index": "0", "sub_step_id": "s", "tool_name": "read_file", "result": {}},
            "bad-result": {"candidate_index": 0, "sub_step_id": "s", "tool_name": "read_file", "result": "x"},
            "ok": {
                "candidate_index": 0,
                "sub_step_id": "s",
                "tool_name": "read_file",
                "fingerprint": "abc",
                "result": {"result": "content"},
            },
        }
    )

    assert restored.cached_result_count() == 1
    assert restored.precompleted_tools_for(0, "s") == {"read_file": {"result": "content"}}


def test_from_snapshot_tolerates_non_dict_input():
    assert CandidateToolResultCache.from_snapshot(None).cached_result_count() == 0
    assert CandidateToolResultCache.from_snapshot("nope").cached_result_count() == 0
