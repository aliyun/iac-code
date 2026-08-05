import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.tools.cloud.base_stack import STACK_RESULT_METADATA_KEY
from iac_code.web import outputs
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager


def test_build_ros_console_url_ok():
    url = outputs.build_ros_console_url("cn-hangzhou", "stack-123")
    assert url == "https://ros.console.aliyun.com/cn-hangzhou/stacks/stack-123"


def test_build_ros_console_url_missing_returns_none():
    assert outputs.build_ros_console_url("", "stack-123") is None
    assert outputs.build_ros_console_url("cn-hangzhou", "") is None
    assert outputs.build_ros_console_url(None, None) is None


def test_template_format_by_suffix():
    assert outputs.template_format(".json") == "json"
    assert outputs.template_format(".JSON") == "json"
    assert outputs.template_format(".tf") == "terraform"
    assert outputs.template_format(".yaml") == "yaml"
    assert outputs.template_format(".yml") == "yaml"
    assert outputs.template_format(".txt") == "yaml"


def test_is_template_content_json_ros():
    text = '{"ROSTemplateFormatVersion": "2015-09-01", "Resources": {}}'
    assert outputs.is_template_content(text, ".json") is True


def test_is_template_content_json_plain_rejected():
    assert outputs.is_template_content('{"foo": 1, "bar": [1, 2]}', ".json") is False


def test_is_template_content_json_invalid_rejected():
    assert outputs.is_template_content("not json at all", ".json") is False


def test_is_template_content_yaml_ros():
    text = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  vpc:\n    Type: ALIYUN::ECS::VPC\n"
    assert outputs.is_template_content(text, ".yaml") is True


def test_is_template_content_yaml_transform_terraform():
    text = 'Transform: Aliyun::Terraform-v1.0\nWorkspace:\n  main.tf: |\n    resource "x" "y" {}\n'
    assert outputs.is_template_content(text, ".yml") is True


def test_is_template_content_yaml_plain_rejected():
    assert outputs.is_template_content("name: hello\nvalue: 3\n", ".yaml") is False


def test_is_template_content_tf():
    assert outputs.is_template_content('resource "alicloud_vpc" "v" {}', ".tf") is True
    assert outputs.is_template_content('provider "alicloud" {}', ".tf") is True
    assert outputs.is_template_content("terraform {\n  required_providers {}\n}", ".tf") is True


def test_is_template_content_tf_plain_rejected():
    assert outputs.is_template_content("just some text\nno hcl here\n", ".tf") is False


class _FakeSession:
    def __init__(self, cwd, session_id="s-1", context_id=None):
        self.cwd = str(cwd)
        self.session_id = session_id
        self.context_id = context_id


class _FakeManager:
    """暴露 storage.load 返回预置消息,以及 pipeline A2A envelope 列表。"""

    def __init__(self, messages, envelopes=None):
        self._messages = messages
        self._envelopes = envelopes or []

        class _Storage:
            def load(self, cwd, session_id):
                return messages

        self.storage = _Storage()

    def _load_a2a_pipeline_envelopes(self, context_id):
        return self._envelopes if context_id else []


def _env_tool_result(tool_name, *, tool_input=None, result=None, stack_result=None):
    """构造一个 pipeline A2A `tool_result` envelope(与真实日志同形)。"""
    data = {"toolName": tool_name, "input": tool_input or {}, "result": result}
    if stack_result is not None:
        data["stackResult"] = stack_result
    return {
        "eventType": "tool_result",
        "data": data,
    }


def _env_stack_current_changed(*, stack_id, stack_name, stack_status, region_id="cn-hangzhou"):
    """构造一个 pipeline A2A `stack_current_changed` envelope(部署开始即发,进行中态)。

    字段用真实日志的 camelCase(见 a2a/pipeline_events._stack_current_changed_data_*)。
    """
    return {
        "eventType": "stack_current_changed",
        "data": {
            "toolName": "ros_deploy",
            "action": "CreateStack",
            "regionId": region_id,
            "stackId": stack_id,
            "stackName": stack_name,
            "stackStatus": stack_status,
            "isSuccess": True,
            "current": True,
        },
    }


def _tool_use(name, tool_id, **input_kwargs):
    return Message(role="assistant", content=[ToolUseBlock(id=tool_id, name=name, input=input_kwargs)])


def _tool_result(tool_id, payload, *, metadata=None):
    import json as _json

    body = payload if isinstance(payload, str) else _json.dumps(payload)
    return Message(
        role="user",
        content=[ToolResultBlock(tool_use_id=tool_id, content=body, metadata=metadata or {})],
    )


ROS_JSON = '{"ROSTemplateFormatVersion": "2015-09-01", "Resources": {"v": {"Type": "ALIYUN::ECS::VPC"}}}'
ROS_YAML = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  vpc:\n    Type: ALIYUN::ECS::VPC\n"


def test_payload_stack_with_console_url():
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {
                "stack_id": "stk-1",
                "stack_name": "demo",
                "status": "CREATE_COMPLETE",
                "is_success": True,
                "status_reason": "ok",
            },
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "stk-1"
    assert stack["stackName"] == "demo"
    assert stack["isSuccess"] is True
    assert stack["regionId"] == "cn-hangzhou"
    assert stack["consoleUrl"] == "https://ros.console.aliyun.com/cn-hangzhou/stacks/stk-1"


def test_payload_stack_dedup_latest_wins():
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {"stack_id": "stk-1", "stack_name": "demo", "status": "CREATE_IN_PROGRESS", "is_success": False},
        ),
        _tool_use("ros_stack", "t2", action="UpdateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t2",
            {"stack_id": "stk-1", "stack_name": "demo", "status": "UPDATE_COMPLETE", "is_success": True},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    assert payload["stacks"][0]["status"] == "UPDATE_COMPLETE"


def test_payload_stack_dedup_same_name_different_ids():
    # 失败后重试 CreateStack 会为同名栈生成新 stack_id;对用户是同一个栈,
    # 只应显示一条并取最新状态,而非每次尝试各占一行。
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {"stack_id": "stk-a", "stack_name": "ha-web-app-stack", "status": "CREATE_FAILED", "is_success": False},
        ),
        _tool_use("ros_stack", "t2", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t2",
            {"stack_id": "stk-b", "stack_name": "ha-web-app-stack", "status": "CREATE_FAILED", "is_success": False},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackName"] == "ha-web-app-stack"
    assert stack["stackId"] == "stk-b"  # 最新一次尝试
    assert stack["consoleUrl"] == "https://ros.console.aliyun.com/cn-hangzhou/stacks/stk-b"


def test_payload_stack_delete_updates_to_delete_complete():
    # 释放资源栈后,面板应反映 DELETE_COMPLETE,而非停留在 CREATE_COMPLETE。
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {"stack_id": "stk-1", "stack_name": "ha-web-app-stack", "status": "CREATE_COMPLETE", "is_success": True},
        ),
        _tool_use("ros_stack", "t2", action="DeleteStack", region_id="cn-hangzhou"),
        _tool_result(
            "t2",
            {"stack_id": "stk-1", "stack_name": "ha-web-app-stack", "status": "DELETE_COMPLETE", "is_success": True},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    assert payload["stacks"][0]["status"] == "DELETE_COMPLETE"
    assert payload["stacks"][0]["isSuccess"] is True


def test_payload_stack_same_name_different_region_kept_separate(monkeypatch):
    # 跨 region 的同名栈是不同的栈,不应被并成一条。
    monkeypatch.setattr(outputs, "_default_region_id", lambda: None)
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {"stack_id": "stk-a", "stack_name": "demo", "status": "CREATE_COMPLETE", "is_success": True},
        ),
        _tool_use("ros_stack", "t2", action="CreateStack", region_id="cn-beijing"),
        _tool_result(
            "t2",
            {"stack_id": "stk-b", "stack_name": "demo", "status": "CREATE_COMPLETE", "is_success": True},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 2


def test_payload_stack_missing_region_no_url(monkeypatch):
    monkeypatch.setattr(outputs, "_default_region_id", lambda: None)
    messages = [
        _tool_use("ros_stack", "t1", action="CreateStack"),
        _tool_result(
            "t1",
            {"stack_id": "stk-1", "stack_name": "demo", "status": "CREATE_COMPLETE", "is_success": True},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    assert payload["stacks"][0]["consoleUrl"] is None


def test_payload_files_detects_ros_template(tmp_path):
    (tmp_path / "tpl.json").write_text(ROS_JSON, encoding="utf-8")
    messages = [_tool_use("write_file", "w1", path="tpl.json")]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession(tmp_path))
    assert len(payload["files"]) == 1
    entry = payload["files"][0]
    assert entry["name"] == "tpl.json"
    assert entry["format"] == "json"
    assert entry["relPath"] == "tpl.json"


def test_payload_files_skips_plain_json(tmp_path):
    (tmp_path / "data.json").write_text('{"foo": 1}', encoding="utf-8")
    messages = [_tool_use("write_file", "w1", path="data.json")]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession(tmp_path))
    assert payload["files"] == []


def test_payload_files_tf_and_edit(tmp_path):
    (tmp_path / "main.tf").write_text('resource "alicloud_vpc" "v" {}', encoding="utf-8")
    messages = [
        _tool_use("write_file", "w1", path="main.tf"),
        _tool_use("edit_file", "e1", path="main.tf"),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession(tmp_path))
    assert len(payload["files"]) == 1
    assert payload["files"][0]["format"] == "terraform"


def test_payload_files_skips_deleted(tmp_path):
    messages = [_tool_use("write_file", "w1", path="gone.yaml")]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession(tmp_path))
    assert payload["files"] == []


def test_payload_pipeline_write_file_from_envelope_content(tmp_path):
    # pipeline 生成的模板文件可能已从磁盘删除,须靠 envelope 捕获内容识别。
    envelopes = [
        _env_tool_result("write_file", tool_input={"path": "templates/net.yml", "content": ROS_YAML}),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["files"]) == 1
    entry = payload["files"][0]
    assert entry["name"] == "net.yml"
    assert entry["format"] == "yaml"
    assert entry["relPath"] == "templates/net.yml"


def test_payload_pipeline_stack_defensive_parse(tmp_path):
    # 两次 CreateStack:先失败(无 stack_id 的错误文本),后成功;只应识别真栈。
    envelopes = [
        _env_tool_result(
            "ros_stack",
            tool_input={"action": "CreateStack", "region_id": "cn-hangzhou"},
            result="[CreateStack] 模板必须使用 TemplateURL 而非 TemplateBody。",
        ),
        _env_tool_result(
            "ros_stack",
            tool_input={"action": "CreateStack", "region_id": "cn-hangzhou"},
            result='{"stack_id": "stk-9", "stack_name": "vswitch", "status": "CREATE_COMPLETE", "is_success": true}',
        ),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "stk-9"
    assert stack["isSuccess"] is True
    assert stack["consoleUrl"] == "https://ros.console.aliyun.com/cn-hangzhou/stacks/stk-9"


def test_payload_pipeline_ros_deploy_stack(tmp_path):
    # 流水线部署工具已从 ros_stack 迁移到 ros_deploy(action: create/continue_create/...),
    # 其结果结构与 ros_stack 兼容(含 stack_id/stack_name/status/is_success)。
    # 先 create 失败、后 continue_create 成功:应识别为同一栈并取最新成功状态。
    envelopes = [
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "3bc4c072", "stack_name": "miniapp-budget", '
                '"status": "CREATE_FAILED", "status_reason": "InvalidDBInstanceClass.Offline", '
                '"is_success": false}'
            ),
        ),
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "continue_create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "3bc4c072", "stack_name": "miniapp-budget", '
                '"status": "CREATE_COMPLETE", "status_reason": "ok", "is_success": true}'
            ),
        ),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "3bc4c072"
    assert stack["stackName"] == "miniapp-budget"
    assert stack["status"] == "CREATE_COMPLETE"
    assert stack["isSuccess"] is True
    assert stack["consoleUrl"] == "https://ros.console.aliyun.com/cn-hangzhou/stacks/3bc4c072"


# ros_stack/ros_deploy 的结果 JSON 后可能被 attach_ros_validation 追加本地预检诊断块
# (见 tools/cloud/aliyun/ros_validation/outcome.py),使整体结果内容不再是合法 JSON。
_PREFLIGHT_SUFFIX = (
    "\n\n---\nROS local preflight diagnostics:\n"
    "ROS local validation completed: 0 errors, 0 warnings, 5 restrictions.\n"
)


def test_payload_pipeline_stack_result_with_preflight_diagnostics_suffix(tmp_path):
    # 真实 create 结果内容是「栈 JSON + 追加的本地预检诊断块」,整体不是合法 JSON。
    # 派生必须容忍尾随文本、解析开头的 JSON 对象,否则该栈的权威终态会被整条丢弃。
    envelopes = [
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "stk-diag", "stack_name": "webapp", '
                '"status": "CREATE_COMPLETE", "status_reason": "ok", "is_success": true}'
                + _PREFLIGHT_SUFFIX
            ),
        ),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "stk-diag"
    assert stack["status"] == "CREATE_COMPLETE"
    assert stack["isSuccess"] is True


def test_payload_pipeline_delete_and_create_shows_new_stack(tmp_path):
    # 复现 bug:create → delete_and_create 后,输出面板永远只显示「之前删除的那个 stack」。
    # 旧栈先以 CREATE_IN_PROGRESS(stack_current_changed)落面板;随后 create 与
    # delete_and_create 的权威终态 tool_result 都携带本地预检诊断块——若解析用严格 json.loads
    # 会整条丢弃,新栈(delete_and_create 产出的 create_result)永远进不来,面板停留在旧的已删栈。
    envelopes = [
        _env_stack_current_changed(stack_id="stk-old", stack_name="app", stack_status="CREATE_IN_PROGRESS"),
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "stk-old", "stack_name": "app", '
                '"status": "CREATE_FAILED", "status_reason": "ContinueCreateStackValidationFailed", '
                '"is_success": false}' + _PREFLIGHT_SUFFIX
            ),
        ),
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "delete_and_create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "stk-new", "stack_name": "app", '
                '"status": "CREATE_COMPLETE", "status_reason": "ok", "is_success": true}' + _PREFLIGHT_SUFFIX
            ),
        ),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "stk-new"  # 应显示新建的栈,而非之前删除的 stk-old
    assert stack["status"] == "CREATE_COMPLETE"
    assert stack["isSuccess"] is True


def test_payload_pipeline_ros_deploy_prefers_structured_stack_result(tmp_path):
    stack_result = {
        "stack_id": "stack-failed",
        "stack_name": "demo",
        "status": "CREATE_FAILED",
        "status_reason": "Bootstrap failed",
        "is_success": False,
    }
    envelopes = [
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "create", "region_id": "cn-hangzhou"},
            result=json.dumps(stack_result) + "\n---\nROS local preflight diagnostics:\n3 limitations",
            stack_result=stack_result,
        )
    ]

    payload = outputs.outputs_payload(
        _FakeManager([], envelopes),
        _FakeSession(tmp_path, context_id="ctx-1"),
    )

    assert payload["stacks"][0]["stackId"] == "stack-failed"
    assert payload["stacks"][0]["statusReason"] == "Bootstrap failed"


def test_payload_pipeline_stack_appears_in_progress(tmp_path):
    # 部署开始:仅有进行中态 stack_current_changed(尚无终态 tool_result)时,资源栈就应出现,
    # 状态为 CREATE_IN_PROGRESS、isSuccess=False、带 console URL——让面板在创建开始即显示,而非完成后。
    envelopes = [
        _env_stack_current_changed(stack_id="stk-inprog", stack_name="webapp", stack_status="CREATE_IN_PROGRESS"),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["stackId"] == "stk-inprog"
    assert stack["stackName"] == "webapp"
    assert stack["status"] == "CREATE_IN_PROGRESS"
    assert stack["isSuccess"] is False
    assert stack["consoleUrl"] == "https://ros.console.aliyun.com/cn-hangzhou/stacks/stk-inprog"


def test_payload_pipeline_terminal_result_overwrites_in_progress(tmp_path):
    # 进行中态先落面板,终态 tool_result 到来后应以相同 region::栈名 键覆盖:单个栈、终态权威
    # (status_reason/is_success 来自 tool_result),不残留「创建中」行。
    envelopes = [
        _env_stack_current_changed(stack_id="stk-1", stack_name="webapp", stack_status="CREATE_IN_PROGRESS"),
        _env_tool_result(
            "ros_deploy",
            tool_input={"action": "create", "region_id": "cn-hangzhou"},
            result=(
                '{"stack_id": "stk-1", "stack_name": "webapp", '
                '"status": "CREATE_COMPLETE", "status_reason": "ok", "is_success": true}'
            ),
        ),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["stacks"]) == 1
    stack = payload["stacks"][0]
    assert stack["status"] == "CREATE_COMPLETE"
    assert stack["statusReason"] == "ok"
    assert stack["isSuccess"] is True


def test_payload_pipeline_terminal_stack_current_changed_ignored(tmp_path):
    # 终态 stack_current_changed(CREATE_COMPLETE,非进行中)不应单独入栈:终态一律走 tool_result
    # 权威路径。仅有一条终态 stack_current_changed、无 tool_result 时,面板为空(避免用非权威结果建栈)。
    envelopes = [
        _env_stack_current_changed(stack_id="stk-done", stack_name="webapp", stack_status="CREATE_COMPLETE"),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert payload["stacks"] == []


def test_payload_stack_ros_deploy_main_session():
    # ros_deploy 出现在主会话消息里时,同样应派生出资源栈。
    messages = [
        _tool_use("ros_deploy", "t1", action="create", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            {"stack_id": "stk-d", "stack_name": "app", "status": "CREATE_COMPLETE", "is_success": True},
        ),
    ]
    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))
    assert len(payload["stacks"]) == 1
    assert payload["stacks"][0]["stackId"] == "stk-d"
    assert payload["stacks"][0]["status"] == "CREATE_COMPLETE"


def test_payload_stack_ros_deploy_main_session_uses_metadata():
    stack_result = {
        "stack_id": "stack-failed",
        "stack_name": "app",
        "status": "CREATE_FAILED",
        "status_reason": "Bootstrap failed",
        "is_success": False,
    }
    messages = [
        _tool_use("ros_deploy", "t1", action="create", region_id="cn-hangzhou"),
        _tool_result(
            "t1",
            json.dumps(stack_result) + "\n---\nROS local preflight diagnostics:\n3 limitations",
            metadata={STACK_RESULT_METADATA_KEY: stack_result},
        ),
    ]

    payload = outputs.outputs_payload(_FakeManager(messages), _FakeSession("/tmp/x"))

    assert payload["stacks"][0]["stackId"] == "stack-failed"
    assert payload["stacks"][0]["status"] == "CREATE_FAILED"


def test_payload_pipeline_dedup_abs_and_relative_path(tmp_path):
    # 同一文件既以绝对路径又以相对路径记录时,须按解析后的绝对路径去重为一条。
    abs_path = str((tmp_path / "templates" / "net.yml"))
    envelopes = [
        _env_tool_result("write_file", tool_input={"path": abs_path, "content": ROS_YAML}),
        _env_tool_result("edit_file", tool_input={"path": "templates/net.yml"}),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert len(payload["files"]) == 1


def test_payload_pipeline_skips_non_template_suffix(tmp_path):
    envelopes = [
        _env_tool_result("write_file", tool_input={"path": "notes.md", "content": ROS_YAML}),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert payload["files"] == []


def test_payload_merges_main_session_and_pipeline(tmp_path):
    (tmp_path / "tpl.json").write_text(ROS_JSON, encoding="utf-8")
    messages = [_tool_use("write_file", "w1", path="tpl.json")]
    envelopes = [
        _env_tool_result("write_file", tool_input={"path": "templates/net.yml", "content": ROS_YAML}),
    ]
    session = _FakeSession(tmp_path, context_id="ctx-1")
    payload = outputs.outputs_payload(_FakeManager(messages, envelopes), session)
    names = sorted(f["name"] for f in payload["files"])
    assert names == ["net.yml", "tpl.json"]


def test_payload_no_context_id_skips_pipeline(tmp_path):
    envelopes = [
        _env_tool_result("write_file", tool_input={"path": "templates/net.yml", "content": ROS_YAML}),
    ]
    session = _FakeSession(tmp_path, context_id=None)
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert payload["files"] == []


def _env_input_required(options, *, prompt="请选择要部署的方案"):
    """构造一个 pipeline A2A `input_required` envelope(与真实 confirm_and_select 同形)。"""
    return {
        "eventType": "input_required",
        "data": {"stepId": "confirm_and_select", "prompt": prompt, "options": options},
    }


def test_candidate_options_from_input_required(tmp_path):
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [
        _env_input_required(
            [
                {"name": "经济极简方案", "summary": "低成本", "candidate_index": 0},
                {"name": "均衡性价比方案", "summary": "更均衡", "candidate_index": 1},
            ]
        ),
    ]
    candidates = outputs.pipeline_candidate_options(_FakeManager([], envelopes), session)
    assert candidates == [
        {"candidateName": "经济极简方案", "candidateIndex": 0, "summary": "低成本"},
        {"candidateName": "均衡性价比方案", "candidateIndex": 1, "summary": "更均衡"},
    ]


def test_candidate_options_sorted_by_index(tmp_path):
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [
        _env_input_required(
            [
                {"name": "B", "summary": "b", "candidate_index": 1},
                {"name": "A", "summary": "a", "candidate_index": 0},
            ]
        ),
    ]
    candidates = outputs.pipeline_candidate_options(_FakeManager([], envelopes), session)
    assert [c["candidateIndex"] for c in candidates] == [0, 1]


def test_candidate_options_no_input_required(tmp_path):
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [_env_tool_result("write_file", tool_input={"path": "n.yml", "content": ROS_YAML})]
    assert outputs.pipeline_candidate_options(_FakeManager([], envelopes), session) == []


def test_candidate_options_ask_user_question_ignored(tmp_path):
    """ask_user_question 的 options 无 candidate_index,不应被当作候选表。"""
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [
        _env_input_required([{"name": "是"}, {"name": "否"}], prompt="确认?"),
    ]
    assert outputs.pipeline_candidate_options(_FakeManager([], envelopes), session) == []


def test_candidate_options_latest_wins(tmp_path):
    """多个 input_required 时取最后一个候选表。"""
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [
        _env_input_required([{"name": "旧A", "candidate_index": 0}]),
        _env_input_required(
            [
                {"name": "新A", "summary": "", "candidate_index": 0},
                {"name": "新B", "summary": "", "candidate_index": 1},
            ]
        ),
    ]
    candidates = outputs.pipeline_candidate_options(_FakeManager([], envelopes), session)
    assert [c["candidateName"] for c in candidates] == ["新A", "新B"]


def test_candidate_options_no_context_id(tmp_path):
    session = _FakeSession(tmp_path, context_id=None)
    envelopes = [_env_input_required([{"name": "A", "candidate_index": 0}])]
    assert outputs.pipeline_candidate_options(_FakeManager([], envelopes), session) == []


def test_payload_candidates_includes_all_even_without_diagram(tmp_path):
    """权威候选表含全部候选,即使某候选无可渲染模板(与 diagrams 只含可渲染者形成对照)。"""
    session = _FakeSession(tmp_path, context_id="ctx-1")
    envelopes = [
        _env_input_required(
            [
                {"name": "经济极简方案", "summary": "s0", "candidate_index": 0},
                {"name": "均衡性价比方案", "summary": "s1", "candidate_index": 1},
            ]
        ),
        # 仅 idx1 有可渲染模板;idx0 无模板写入 → diagrams 不含 idx0,但 candidates 仍含两者。
        {
            "eventType": "tool_result",
            "candidate": {"index": 1, "name": "均衡性价比方案"},
            "data": {"toolName": "write_file", "input": {"path": "tpl1.yaml", "content": ROS_YAML}},
        },
    ]
    payload = outputs.outputs_payload(_FakeManager([], envelopes), session)
    assert [c["candidateIndex"] for c in payload["candidates"]] == [0, 1]
    assert [d["candidateIndex"] for d in payload["diagrams"]] == [1]


def test_read_output_file_ok(tmp_path):
    (tmp_path / "tpl.yaml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    result = outputs.read_output_file(_FakeSession(tmp_path), "tpl.yaml")
    assert result["path"] == "tpl.yaml"
    assert result["format"] == "yaml"
    assert "ROSTemplateFormatVersion" in result["content"]


def test_read_output_file_traversal_forbidden(tmp_path):
    with pytest.raises(outputs.OutputPathForbidden):
        outputs.read_output_file(_FakeSession(tmp_path), "../../etc/passwd")


def test_read_output_file_missing(tmp_path):
    with pytest.raises(outputs.OutputFileMissing):
        outputs.read_output_file(_FakeSession(tmp_path), "nope.json")


def test_read_output_file_out_of_cwd_allowed(tmp_path):
    # Agent 把模板写到 cwd 之外(如 /tmp/xx.yml)时,只要它在派生输出集里就应可预览。
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "ros-vswitch-template.yml"
    outside.write_text(ROS_YAML, encoding="utf-8")
    allowed = {str(outside.resolve())}
    result = outputs.read_output_file(_FakeSession(proj), str(outside), allowed_paths=allowed)
    assert result["format"] == "yaml"
    assert "ROSTemplateFormatVersion" in result["content"]


def test_read_output_file_out_of_cwd_forbidden_without_allow(tmp_path):
    # 不在允许集里的 cwd 外路径仍须拒绝(防目录穿越)。
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "ros-vswitch-template.yml"
    outside.write_text(ROS_YAML, encoding="utf-8")
    with pytest.raises(outputs.OutputPathForbidden):
        outputs.read_output_file(_FakeSession(proj), str(outside))


def _seed_manager(tmp_path):
    manager = WebSessionManager(cwd=str(tmp_path))
    session = manager.create_session(cwd=str(tmp_path), session_id="out-1")
    (Path(session.cwd) / "tpl.json").write_text(ROS_JSON, encoding="utf-8")
    manager.storage.append(session.cwd, "out-1", _tool_use("write_file", "w1", path="tpl.json"))
    return manager, session


def test_route_outputs_unknown_session_404(tmp_path):
    manager = WebSessionManager(cwd=str(tmp_path))
    app = create_app(session_manager=manager)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions/does-not-exist/outputs")
    assert resp.status_code == 404


def test_route_outputs_ok(tmp_path):
    manager, session = _seed_manager(tmp_path)
    app = create_app(session_manager=manager)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/sessions/{}/outputs".format(session.session_id))
    assert resp.status_code == 200
    body = resp.json()
    assert "stacks" in body and "files" in body
    assert any(f["name"] == "tpl.json" for f in body["files"])


def test_route_output_file_out_of_cwd_listed_file(tmp_path):
    # 回归:模板写到 cwd 之外(/tmp 等),面板列出后点击预览不应报「文件已不存在」。
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "ros-vswitch-template.yml"
    outside.write_text(ROS_YAML, encoding="utf-8")
    manager = WebSessionManager(cwd=str(proj))
    session = manager.create_session(cwd=str(proj), session_id="out-2")
    manager.storage.append(session.cwd, "out-2", _tool_use("write_file", "w1", path=str(outside)))
    app = create_app(session_manager=manager)
    with TestClient(app, raise_server_exceptions=False) as client:
        listing = client.get("/api/sessions/{}/outputs".format(session.session_id)).json()
        assert any(f["name"] == "ros-vswitch-template.yml" for f in listing["files"])
        abs_path = next(f["path"] for f in listing["files"] if f["name"] == "ros-vswitch-template.yml")
        resp = client.get(
            "/api/sessions/{}/outputs/file".format(session.session_id),
            params={"path": abs_path},
        )
    assert resp.status_code == 200
    assert resp.json()["format"] == "yaml"


def test_route_outputs_file_states(tmp_path):
    manager, session = _seed_manager(tmp_path)
    app = create_app(session_manager=manager)
    with TestClient(app, raise_server_exceptions=False) as client:
        ok = client.get("/api/sessions/{}/outputs/file?path=tpl.json".format(session.session_id))
        forbidden = client.get("/api/sessions/{}/outputs/file?path=../../etc/passwd".format(session.session_id))
        missing = client.get("/api/sessions/{}/outputs/file?path=nope.json".format(session.session_id))
    assert ok.status_code == 200
    assert ok.json()["format"] == "json"
    assert forbidden.status_code == 403
    assert missing.status_code == 404
