"""ROS-aware YAML loader that handles !Ref, !GetAtt, !Sub and other intrinsic function tags."""

from __future__ import annotations

from typing import Any

import yaml

from iac_code.tools.cloud.aliyun.ros_validation.function_specs import yaml_short_function_names

_ROS_FUNCTIONS = yaml_short_function_names()


def _make_fn_constructor(fn_name: str):
    tag_name = f"Fn::{fn_name}"

    def constructor(loader: yaml.SafeLoader, node: yaml.Node) -> dict:
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        elif isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node)
        else:
            value = loader.construct_object(node)
        return {tag_name: value}

    return constructor


def _ref_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> dict:
    return {"Ref": loader.construct_scalar(node)}


def _getatt_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        parts = value.split(".")
        if len(parts) == 2:
            resource, attribute = parts
        elif len(parts) >= 3:
            if parts[-2] == "Outputs":
                resource = ".".join(parts[:-2])
                attribute = ".".join(parts[-2:])
            else:
                resource = ".".join(parts[:-1])
                attribute = parts[-1]
        else:
            return {"Fn::GetAtt": value}
        return {"Fn::GetAtt": [resource, attribute]}
    elif isinstance(node, yaml.SequenceNode):
        return {"Fn::GetAtt": loader.construct_sequence(node)}
    else:
        return {"Fn::GetAtt": loader.construct_object(node)}


class _RosYamlLoader(yaml.SafeLoader):
    pass


def _timestamp_as_string(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return loader.construct_scalar(node)


_RosYamlLoader.add_constructor("!Ref", _ref_constructor)
_RosYamlLoader.add_constructor("!GetAtt", _getatt_constructor)
_RosYamlLoader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp_as_string)
for _fn in _ROS_FUNCTIONS:
    if _fn == "GetAtt":
        # The scalar short form needs the ROS-specific dotted-name split above.
        # Re-registering it with the generic constructor would silently turn
        # ``!GetAtt Resource.Attribute`` into a raw String argument.
        continue
    _RosYamlLoader.add_constructor(f"!{_fn}", _make_fn_constructor(_fn))


def ros_yaml_load(text: str) -> Any:
    """Load YAML text with ROS intrinsic function tag support."""
    return yaml.load(text, Loader=_RosYamlLoader)
