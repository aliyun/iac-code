#!/usr/bin/env python3
"""Convert a Terraform directory to a ROS Terraform-type template file."""

import json
import os
import sys
from pathlib import Path

import yaml


class _BlockStr(str):
    """String subclass that forces YAML block scalar (|) style."""


def _block_str_representer(dumper: yaml.Dumper, data: _BlockStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_BlockStr, _block_str_representer)


# Directories that must never be bundled into Workspace.
_SKIP_DIRS = {".terraform", ".git", "__pycache__"}
# File suffixes that must never be bundled into Workspace.
_SKIP_SUFFIXES = (".tfstate", ".tfstate.backup")


def convert(tf_dir: str, output_path: str) -> None:
    tf_path = Path(tf_dir)
    if not tf_path.is_dir():
        print(f"Error: {tf_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    rel_paths: list[Path] = []
    for root, dirs, files in os.walk(tf_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if fname.endswith(_SKIP_SUFFIXES):
                continue
            rel_paths.append(Path(root, fname).relative_to(tf_path))

    workspace: dict[str, _BlockStr] = {}
    for rel in sorted(rel_paths):
        full = tf_path / rel
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"Warning: skipping non-text file {rel.as_posix()}", file=sys.stderr)
            continue
        workspace[rel.as_posix()] = _BlockStr(text)

    if not any(k.endswith(".tf") for k in workspace):
        print(f"Error: no .tf files found in {tf_dir}", file=sys.stderr)
        sys.exit(1)

    template: dict = {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Transform": "Aliyun::OpenTofu-v1.8",
        "Workspace": workspace,
    }

    out = Path(output_path)
    if out.suffix in (".yml", ".yaml"):
        out.write_text(
            yaml.dump(template, allow_unicode=True, default_flow_style=False, sort_keys=False), encoding="utf-8"
        )
    else:
        out.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Template written to {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <terraform_dir> <output_file>", file=sys.stderr)
        print("  output_file: .yml/.yaml for YAML, .json for JSON", file=sys.stderr)
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
