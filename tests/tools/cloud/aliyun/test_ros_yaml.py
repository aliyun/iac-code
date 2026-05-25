"""Tests for ROS YAML loader."""

from __future__ import annotations

import pytest

from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load


class TestRosYamlLoad:
    def test_ref_scalar(self) -> None:
        result = ros_yaml_load("Value: !Ref MyVpc")
        assert result == {"Value": {"Ref": "MyVpc"}}

    def test_getatt_scalar_dot_split(self) -> None:
        result = ros_yaml_load("Value: !GetAtt MyEcs.PublicIp")
        assert result == {"Value": {"Fn::GetAtt": ["MyEcs", "PublicIp"]}}

    def test_getatt_sequence(self) -> None:
        text = "Value: !GetAtt\n  - MyEcs\n  - PublicIp"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::GetAtt": ["MyEcs", "PublicIp"]}}

    def test_getatt_outputs_dot(self) -> None:
        result = ros_yaml_load("Value: !GetAtt NetworkStack.Outputs.VpcId")
        assert result == {"Value": {"Fn::GetAtt": ["NetworkStack", "Outputs.VpcId"]}}

    def test_sub_scalar(self) -> None:
        result = ros_yaml_load("Value: !Sub 'hello-${Name}'")
        assert result == {"Value": {"Fn::Sub": "hello-${Name}"}}

    def test_join_sequence(self) -> None:
        text = "Value: !Join\n  - ','\n  - - a\n    - b"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::Join": [",", ["a", "b"]]}}

    def test_select_sequence(self) -> None:
        text = "Value: !Select\n  - 0\n  - - a\n    - b"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::Select": [0, ["a", "b"]]}}

    def test_if_sequence(self) -> None:
        text = "Value: !If\n  - IsProd\n  - 'yes'\n  - 'no'"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::If": ["IsProd", "yes", "no"]}}

    def test_equals_sequence(self) -> None:
        text = "Value: !Equals\n  - a\n  - b"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::Equals": ["a", "b"]}}

    def test_base64_scalar(self) -> None:
        result = ros_yaml_load("Value: !Base64 'hello world'")
        assert result == {"Value": {"Fn::Base64": "hello world"}}

    def test_mapping_node(self) -> None:
        text = "Value: !Sub\n  - 'hello-${Var}'\n  - Var: world"
        result = ros_yaml_load(text)
        assert result == {"Value": {"Fn::Sub": ["hello-${Var}", {"Var": "world"}]}}

    def test_full_ros_template(self) -> None:
        text = """\
ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  ZoneId:
    Type: String
Resources:
  MyVpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 192.168.0.0/16
  MyVsw:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId: !Ref MyVpc
      ZoneId: !Ref ZoneId
      CidrBlock: 192.168.1.0/24
Outputs:
  VpcId:
    Value: !GetAtt MyVpc.VpcId
"""
        result = ros_yaml_load(text)
        assert result["ROSTemplateFormatVersion"] == "2015-09-01"
        assert result["Resources"]["MyVsw"]["Properties"]["VpcId"] == {"Ref": "MyVpc"}
        assert result["Outputs"]["VpcId"]["Value"] == {"Fn::GetAtt": ["MyVpc", "VpcId"]}

    def test_plain_yaml_still_works(self) -> None:
        result = ros_yaml_load("key: value")
        assert result == {"key": "value"}

    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(Exception):
            ros_yaml_load("key: [unclosed")
