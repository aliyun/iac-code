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
    assert "`candidate.name` 固定为 `iac-code-web-single-ecs`" in architecture
    assert "单 ECS + EIP" in architecture
    assert "安全组仅开放 8766" in architecture
    assert "不得增加其他入口资源" in architecture
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
        "ALIYUN::RAM::Role",
        "ALIYUN::ECS::RamRoleAttachment",
        "ALIYUN::Bailian::ApiKey",
        "EcsRamRole",
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

    assert template["Metadata"]["ALIYUN::ROS::Interface"]["TemplateTags"] == ["acs:solution:iac-code:iac-code-web"]
    assert {name: resource["Type"] for name, resource in resources.items()} == {
        "Vpc": "ALIYUN::ECS::VPC",
        "VSwitch": "ALIYUN::ECS::VSwitch",
        "SecurityGroup": "ALIYUN::ECS::SecurityGroup",
        "Instance": "ALIYUN::ECS::Instance",
        "Eip": "ALIYUN::VPC::EIP",
        "EipAssociation": "ALIYUN::VPC::EIPAssociation",
        "InstanceRamRole": "ALIYUN::RAM::Role",
        "InstanceRamRoleAttachment": "ALIYUN::ECS::RamRoleAttachment",
        "BailianApiKey": "ALIYUN::Bailian::ApiKey",
        "Bootstrap": "ALIYUN::ECS::RunCommand",
    }
    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [{"IpProtocol": "tcp", "PortRange": "8766/8766", "SourceCidrIp": {"Ref": "AccessCidr"}}]
    assert resources["Instance"]["Properties"]["AllocatePublicIP"] is False
    assert resources["BailianApiKey"]["Properties"] == {
        "RegionId": "cn-beijing",
        "Description": "Managed by the iac-code Web ROS stack",
        "AuthSetModel": {
            "AuthSetMode": "Custom",
            "AccessIps": [{"Fn::GetAtt": ["Eip", "EipAddress"]}],
        },
    }
    assert resources["InstanceRamRole"]["Properties"] == {
        "RoleName": {"Fn::Sub": "iac-code-web-${ALIYUN::StackId}"},
        "Description": "Managed by the iac-code Web ROS stack",
        "DeletionForce": True,
        "AssumeRolePolicyDocument": {
            "Version": "1",
            "Statement": [
                {
                    "Action": "sts:AssumeRole",
                    "Effect": "Allow",
                    "Principal": {"Service": ["ecs.aliyuncs.com"]},
                }
            ],
        },
        "PolicyAttachments": {"System": ["AdministratorAccess"]},
    }
    assert resources["InstanceRamRoleAttachment"]["Properties"] == {
        "RamRoleName": {"Fn::GetAtt": ["InstanceRamRole", "RoleName"]},
        "InstanceIds": [{"Ref": "Instance"}],
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
    assert bootstrap["DependsOn"] == ["EipAssociation", "InstanceRamRoleAttachment"]
    properties = bootstrap["Properties"]
    assert properties["Type"] == "RunShellScript"
    assert properties["ContentEncoding"] == "PlainText"
    assert properties["Sync"] is True
    assert properties["Timeout"] == 1800
    script, variables = properties["CommandContent"]["Fn::Sub"]
    assert variables == {
        "BailianApiKeyB64": {"Fn::Base64Encode": {"Fn::GetAtt": ["BailianApiKey", "Key"]}},
        "MasterAccountId": {"Ref": "ALIYUN::TenantId"},
        "StackRegion": {"Ref": "ALIYUN::Region"},
        "InstanceRamRoleName": {"Fn::GetAtt": ["InstanceRamRole", "RoleName"]},
        "LocalInstanceId": {"Ref": "Instance"},
        "LocalInstanceType": {"Ref": "InstanceType"},
        "LocalZoneId": {"Ref": "ZoneId"},
        "LocalVpcId": {"Ref": "Vpc"},
        "LocalVSwitchId": {"Ref": "VSwitch"},
        "LocalSecurityGroupId": {"Ref": "SecurityGroup"},
        "LocalEipAddress": {"Fn::GetAtt": ["Eip", "EipAddress"]},
    }
    assert 'pip install --upgrade --index-url https://mirrors.aliyun.com/pypi/simple/ "iac-code[http]"' in script
    assert "iac-code[http]==" not in script
    assert "--host 0.0.0.0 --port 8766 --access-token-file" in script
    assert "Restart=on-failure" in script
    assert "secrets.token_urlsafe(32)" in script
    assert 'exec >>"$LOG" 2>&1' in script
    assert "printf '%s' \"$TOKEN\" >&3" in script
    assert "User=root" in script
    assert "Group=root" in script
    assert "WorkingDirectory=/root" in script
    assert "Environment=IAC_CODE_CONFIG_DIR=/root/.iac-code" in script
    assert "--access-token-file /root/.iac-code/web-access.token" in script
    assert "useradd" not in script
    assert "/var/lib/iac-code" not in script
    assert "AGENTS_FILE=/root/AGENTS.md" in script
    assert "用户提到的“本机”“这台机器”“当前服务器”“当前 ECS”均指此实例" in script
    assert "- 实例 ID：`${LocalInstanceId}`" in script
    assert "- 实例规格：`${LocalInstanceType}`" in script
    assert "- 地域：`${StackRegion}`" in script
    assert "- 可用区：`${LocalZoneId}`" in script
    assert "- VPC ID：`${LocalVpcId}`" in script
    assert "- VSwitch ID：`${LocalVSwitchId}`" in script
    assert "- 安全组 ID：`${LocalSecurityGroupId}`" in script
    assert "- 公网 EIP：`${LocalEipAddress}`" in script
    assert "https://ecs.console.aliyun.com/#/server/region/${StackRegion}?instanceIds=${LocalInstanceId}" in script
    assert "停止、重启、释放本 ECS" in script
    assert 'chmod 0600 "$AGENTS_TMP"' in script
    assert set(re.findall(r"\$\{([^}]+)\}", script)) == {
        "BailianApiKeyB64",
        "InstanceRamRoleName",
        "LocalEipAddress",
        "LocalInstanceId",
        "LocalInstanceType",
        "LocalSecurityGroupId",
        "LocalVpcId",
        "LocalVSwitchId",
        "LocalZoneId",
        "MasterAccountId",
        "StackRegion",
    }
    for expected in (
        "'activeProvider': 'dashscope'",
        "root = pathlib.Path('/root/.iac-code')",
        "root / '.cloud-credentials.yml'",
        "'mode': 'EcsRamRole'",
        "'region_id': '${StackRegion}'",
        "'ram_role_name': '${InstanceRamRoleName}'",
        "'memory': {'autoMemory': True}",
        "'pipeline': {'sellingReviewStep': False}",
        "'apiBase': 'https://dashscope.aliyuncs.com/compatible-mode/v1'",
        "'model': 'qwen3.8-max'",
        "'name': 'DashScope'",
        "'mode': 'normal'",
        "'permissionMode': 'bypass_permissions'",
        "'pipelineName': 'selling'",
        "'ui': {'language': 'zh'}",
        "'userID': '${MasterAccountId}'",
    ):
        assert expected in script
    assert "'mode': 'OAuth'" not in script
    assert "'oauth_site_type': 'CN'" not in script
    assert "'model': 'deepseek-v4-flash-0731'" not in script

    outputs = template["Outputs"]
    assert set(outputs) == {"PublicUrl", "WebAccessToken", "InstanceId", "EipAddress"}
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
