from __future__ import annotations

import re
from pathlib import Path

import yaml

from iac_code.tools.cloud.aliyun.ros_validation.model import MaterializedTemplateSource, RequestValidationContext
from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template

ROOT = Path(__file__).parents[3]
BUNDLED = ROOT / "src/iac_code/skills/bundled/iac_aliyun/references/solutions"
SELLING = ROOT / "src/iac_code/pipeline/selling"
GOLDEN = BUNDLED / "iac-code-web.ros.yml"


def _template() -> dict:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def test_solution_reference_is_shared_and_pipeline_prompts_are_minimal() -> None:
    link = SELLING / "references/solutions"
    assert link.is_symlink()
    assert link.resolve() == BUNDLED.resolve()
    assert (link / "iac-code-web.md").is_file()
    assert (link / "iac-code-web.ros.yml").is_file()

    architecture = (SELLING / "skills/iac-aliyun-architecture/SKILL.md").read_text(encoding="utf-8")
    generating = (SELLING / "skills/iac-aliyun-template-generating/SKILL.md").read_text(encoding="utf-8")
    deploying = (SELLING / "skills/iac-aliyun-deploying/SKILL.md").read_text(encoding="utf-8")
    assert "candidate.name = iac-code-web-single-ecs" in architecture
    assert "references/solutions/iac-code-web.md" in generating
    assert "references/solutions/iac-code-web.ros.yml" in generating
    assert "iac-code-web-single-ecs" not in deploying
    assert "PublicUrl" not in deploying and "WebAccessToken" not in deploying
    reference = (link / "iac-code-web.md").read_text(encoding="utf-8")
    assert "保持现有 Pipeline" not in reference
    assert "中国内地地域" not in reference
    assert "海外地域" not in reference
    for deployment_detail in (
        "AllocatePublicIP: false",
        "ALIYUN::Bailian::ApiKey",
        "--access-token-file",
        "IAC_CODE_CONFIG_DIR",
        "PublicUrl",
        "WebAccessToken",
    ):
        assert deployment_detail in reference
    for implementation_detail in (
        "默认本地 Web",
        "CLI `/auth`",
        "Token transport v1",
        "JS/Python 互操作向量",
        "HKDF",
        "ChaCha20",
        "loopback",
        "ASGI",
        "OAuth",
    ):
        assert implementation_detail not in reference


def test_golden_template_has_only_the_fixed_single_ecs_topology() -> None:
    template = _template()
    resources = template["Resources"]

    assert {name: resource["Type"] for name, resource in resources.items()} == {
        "Vpc": "ALIYUN::ECS::VPC",
        "VSwitch": "ALIYUN::ECS::VSwitch",
        "SecurityGroup": "ALIYUN::ECS::SecurityGroup",
        "Instance": "ALIYUN::ECS::Instance",
        "Eip": "ALIYUN::VPC::EIP",
        "EipAssociation": "ALIYUN::VPC::EIPAssociation",
        "BailianApiKey": "ALIYUN::Bailian::ApiKey",
        "Bootstrap": "ALIYUN::ECS::RunCommand",
    }
    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [{"IpProtocol": "tcp", "PortRange": "8766/8766", "SourceCidrIp": {"Ref": "AccessCidr"}}]
    assert resources["Instance"]["Properties"]["AllocatePublicIP"] is False
    assert resources["BailianApiKey"]["Properties"] == {
        "Description": "Managed by the iac-code Web ROS stack",
        "AuthSetModel": {
            "AuthSetMode": "Custom",
            "AccessIps": [{"Fn::GetAtt": ["Eip", "EipAddress"]}],
        },
    }
    assert "ExistingBailianApiKey" not in template["Parameters"]


def test_golden_template_parameters_outputs_and_bootstrap_follow_contract() -> None:
    template = _template()
    parameters = template["Parameters"]
    grouped = {
        name
        for group in template["Metadata"]["ALIYUN::ROS::Interface"]["ParameterGroups"]
        for name in group["Parameters"]
    }
    assert grouped == set(parameters)
    assert all(parameter.get("AssociationProperty") for parameter in parameters.values())
    assert all(set(parameter["Label"]) == {"en", "zh-cn"} for parameter in parameters.values())

    bootstrap = template["Resources"]["Bootstrap"]
    assert bootstrap["DependsOn"] == "EipAssociation"
    properties = bootstrap["Properties"]
    assert properties["Type"] == "RunShellScript"
    assert properties["ContentEncoding"] == "PlainText"
    assert properties["Sync"] is True
    assert properties["Timeout"] == 1800
    script, variables = properties["CommandContent"]["Fn::Sub"]
    assert variables == {
        "BailianApiKeyB64": {"Fn::Base64Encode": {"Fn::GetAtt": ["BailianApiKey", "Key"]}},
        "MasterAccountId": {"Ref": "ALIYUN::TenantId"},
    }
    assert "iac-code[http]==${IacCodeVersion}" in script
    assert "--host 0.0.0.0 --port 8766 --access-token-file" in script
    assert "Restart=on-failure" in script
    assert "secrets.token_urlsafe(32)" in script
    assert 'exec >>"$LOG" 2>&1' in script
    assert "printf '%s' \"$TOKEN\" >&3" in script
    assert set(re.findall(r"\$\{([^}]+)\}", script)) == {
        "IacCodeVersion",
        "BailianApiKeyB64",
        "MasterAccountId",
    }
    for expected in (
        "'activeProvider': 'dashscope'",
        "'memory': {'autoMemory': True}",
        "'pipeline': {'sellingReviewStep': False}",
        "'apiBase': 'https://dashscope.aliyuncs.com/compatible-mode/v1'",
        "'model': 'qwen3.7-max'",
        "'name': 'DashScope'",
        "'mode': 'normal'",
        "'permissionMode': 'bypass_permissions'",
        "'pipelineName': 'selling'",
        "'ui': {'language': 'zh'}",
        "'userID': '${MasterAccountId}'",
    ):
        assert expected in script

    outputs = template["Outputs"]
    assert set(outputs) == {"PublicUrl", "WebAccessToken", "InstanceId", "EipAddress", "IacCodeVersion"}
    assert all(set(output["Label"]) == {"en", "zh-cn"} for output in outputs.values())
    assert outputs["WebAccessToken"]["Value"] == {
        "Fn::Base64Decode": {"Fn::Jq": ["First", ".[0].Output", {"Fn::GetAtt": ["Bootstrap", "InvokeResults"]}]}
    }
    assert "ApiKey" not in outputs


def test_golden_template_passes_strict_local_ros_validation() -> None:
    source = GOLDEN.read_text(encoding="utf-8")
    report = validate_ros_template(
        MaterializedTemplateSource(source),
        RequestValidationContext(action="PreviewStack"),
    )

    assert report.error_count == 0
    assert report.warning_count == 0
