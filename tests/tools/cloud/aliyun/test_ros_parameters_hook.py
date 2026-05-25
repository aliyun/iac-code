"""Tests for ROS Parameters dict-to-flat conversion hook."""

from __future__ import annotations

from iac_code.tools.cloud.aliyun.hooks.ros_parameters import expand_parameters


class TestExpandParameters:
    def test_dict_to_flat(self) -> None:
        params = {
            "TemplateURL": "/tmp/t.yml",
            "Parameters": {
                "zone_id": "cn-hangzhou-k",
                "instance_type": "ecs.g7.large",
            },
        }
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert "Parameters" not in params
        assert params["Parameters.1.ParameterKey"] == "zone_id"
        assert params["Parameters.1.ParameterValue"] == "cn-hangzhou-k"
        assert params["Parameters.2.ParameterKey"] == "instance_type"
        assert params["Parameters.2.ParameterValue"] == "ecs.g7.large"
        assert params["TemplateURL"] == "/tmp/t.yml"

    def test_list_of_dicts_to_flat(self) -> None:
        params = {
            "Parameters": [
                {"ParameterKey": "VpcCidrBlock", "ParameterValue": "192.168.0.0/16"},
                {"ParameterKey": "ZoneId", "ParameterValue": "cn-hangzhou-k"},
            ],
        }
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert "Parameters" not in params
        assert params["Parameters.1.ParameterKey"] == "VpcCidrBlock"
        assert params["Parameters.1.ParameterValue"] == "192.168.0.0/16"
        assert params["Parameters.2.ParameterKey"] == "ZoneId"
        assert params["Parameters.2.ParameterValue"] == "cn-hangzhou-k"

    def test_list_of_dicts_with_non_string_value(self) -> None:
        params = {
            "Parameters": [
                {"ParameterKey": "Count", "ParameterValue": 3},
                {"ParameterKey": "Enable", "ParameterValue": True},
            ],
        }
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == "3"
        assert params["Parameters.2.ParameterValue"] == "true"

    def test_list_of_dicts_missing_value_uses_empty(self) -> None:
        params = {
            "Parameters": [
                {"ParameterKey": "ZoneId"},
            ],
        }
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterKey"] == "ZoneId"
        assert params["Parameters.1.ParameterValue"] == ""

    def test_list_invalid_item_no_change(self) -> None:
        params = {"Parameters": ["not_a_dict"]}
        original = dict(params)
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params == original

    def test_list_missing_parameter_key_no_change(self) -> None:
        params = {"Parameters": [{"ParameterValue": "v"}]}
        original = dict(params)
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params == original

    def test_already_flat_no_change(self) -> None:
        params = {
            "Parameters.1.ParameterKey": "zone_id",
            "Parameters.1.ParameterValue": "cn-hangzhou-k",
        }
        original = dict(params)
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params == original

    def test_no_parameters_no_change(self) -> None:
        params = {"TemplateURL": "/tmp/t.yml"}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params == {"TemplateURL": "/tmp/t.yml"}

    def test_parameters_not_dict_or_list_no_change(self) -> None:
        params = {"Parameters": "not_a_dict"}
        original = dict(params)
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params == original

    def test_empty_dict(self) -> None:
        params = {"Parameters": {}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert "Parameters" not in params

    def test_empty_list(self) -> None:
        params = {"Parameters": []}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert "Parameters" not in params

    def test_single_parameter(self) -> None:
        params = {"Parameters": {"zone_id": "cn-hangzhou-k"}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterKey"] == "zone_id"
        assert params["Parameters.1.ParameterValue"] == "cn-hangzhou-k"

    def test_value_none_becomes_empty(self) -> None:
        params = {"Parameters": {"key": None}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == ""

    def test_value_bool(self) -> None:
        params = {"Parameters": {"a": True, "b": False}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == "true"
        assert params["Parameters.2.ParameterValue"] == "false"

    def test_value_number(self) -> None:
        params = {"Parameters": {"count": 5, "ratio": 0.8}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == "5"
        assert params["Parameters.2.ParameterValue"] == "0.8"

    def test_value_dict_becomes_json(self) -> None:
        params = {"Parameters": {"tags": {"env": "prod"}}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == '{"env": "prod"}'

    def test_value_list_becomes_json(self) -> None:
        params = {"Parameters": {"zones": ["a", "b"]}}
        result = expand_parameters("ros", "CreateStack", params)
        assert result is None
        assert params["Parameters.1.ParameterValue"] == '["a", "b"]'
