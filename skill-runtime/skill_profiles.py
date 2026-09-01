#!/usr/bin/env python3
"""Render the external Skill sources for one immutable publication profile."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CONDITIONAL_PATTERN = re.compile(r"\{\{#(?P<name>[A-Z0-9_]+)\}\}(?P<body>.*?)\{\{/(?P=name)\}\}", re.DOTALL)
_PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z0-9_]+\}\}")


@dataclass(frozen=True)
class SkillProfile:
    name: str
    source_directory: str
    archive_root: str
    title: str
    requires_runtime: bool
    agenthub: bool
    files: tuple[tuple[str, str], ...]
    bridge_path: str
    user_agent_template: str
    requirements_file: str = ""

    @property
    def output_files(self) -> tuple[str, ...]:
        return tuple(destination for _source, destination in self.files)


PROFILES = {
    "iac-code": SkillProfile(
        name="iac-code",
        source_directory="skills/iac-code",
        archive_root="iac-code",
        title="iac-code",
        requires_runtime=True,
        agenthub=False,
        files=(
            ("SKILL.md.template", "SKILL.md"),
            ("agents/openai.yaml", "agents/openai.yaml"),
            ("scripts/iac_code.py", "scripts/iac_code.py"),
        ),
        bridge_path="scripts/iac_code.py",
        user_agent_template="iac-code-skill/1",
    ),
    "alibabacloud-iac-code": SkillProfile(
        name="alibabacloud-iac-code",
        source_directory="skills/iac-code",
        archive_root="alibabacloud-iac-code",
        title="alibabacloud-iac-code",
        requires_runtime=True,
        agenthub=True,
        files=(
            ("SKILL.md.template", "SKILL.md"),
            ("references/ram-policies.md", "references/ram-policies.md"),
            ("scripts/iac_code.py", "scripts/iac_code.py"),
        ),
        bridge_path="scripts/iac_code.py",
        user_agent_template="AlibabaCloud-Agent-Skills/alibabacloud-iac-code/{session-id}",
    ),
    "alibabacloud-ros-agent": SkillProfile(
        name="alibabacloud-ros-agent",
        source_directory="skills/alicloud-ros-agent",
        archive_root="alibabacloud-ros-agent",
        title="Alibaba Cloud ROS Agent",
        requires_runtime=False,
        agenthub=True,
        files=(
            ("SKILL.md.template", "SKILL.md"),
            ("references/ram-policies.md", "references/ram-policies.md"),
            ("scripts/_ros_agent_core.py", "scripts/_ros_agent_core.py"),
            ("scripts/_ros_agent_projection.py", "scripts/_ros_agent_projection.py"),
            ("scripts/_ros_agent_runtime.py", "scripts/_ros_agent_runtime.py"),
            ("requirements-code.txt", "scripts/requirements.txt"),
            ("scripts/ros_agent.py", "scripts/ros_agent.py"),
        ),
        bridge_path="scripts/ros_agent.py",
        user_agent_template="AlibabaCloud-Agent-Skills/alibabacloud-ros-agent/{session-id}",
        requirements_file="scripts/requirements.txt",
    ),
}

SOURCE_DEFAULTS = (
    PROFILES["iac-code"],
    replace(
        PROFILES["alibabacloud-ros-agent"],
        name="alicloud-ros-agent",
        agenthub=False,
        user_agent_template="AlibabaCloud-Agent-Skills/alicloud-ros-agent",
        requirements_file="requirements-code.txt",
    ),
)


def skill_profile(name: str) -> SkillProfile:
    try:
        return PROFILES[str(name)]
    except KeyError as error:
        raise SystemExit("unsupported Skill profile: {}".format(name)) from error


def render_markdown(source: str, profile: SkillProfile) -> str:
    flags = {"AGENTHUB": profile.agenthub, "PUBLIC": not profile.agenthub}

    def conditional(match: re.Match[str]) -> str:
        return match.group("body") if flags.get(match.group("name"), False) else ""

    rendered = _CONDITIONAL_PATTERN.sub(conditional, source)
    rendered = rendered.replace("{{SKILL_NAME}}", profile.name).replace("{{SKILL_TITLE}}", profile.title)
    if profile.requirements_file:
        rendered = rendered.replace("{{REQUIREMENTS_FILE}}", profile.requirements_file)
    unresolved = _PLACEHOLDER_PATTERN.findall(rendered)
    if unresolved:
        raise SystemExit(
            "Skill template contains unresolved placeholders: {}".format(", ".join(sorted(set(unresolved))))
        )
    return rendered.rstrip() + "\n"


def _replace_constant(source: str, name: str, value: str) -> str:
    pattern = re.compile(r'^{} = "[^"]*"$'.format(re.escape(name)), re.MULTILINE)
    updated, count = pattern.subn("{} = {}".format(name, json.dumps(value)), source)
    if count != 1:
        raise SystemExit("Skill bridge must contain exactly one {} constant".format(name))
    return updated


def render_profile(profile_name: str, destination: Path) -> Path:
    profile = skill_profile(profile_name)
    source_root = ROOT / profile.source_directory
    destination.mkdir(parents=True, exist_ok=True)
    for source_name, destination_name in profile.files:
        source = source_root / source_name
        target = destination / destination_name
        if not source.is_file() or source.is_symlink():
            raise SystemExit("Skill profile source is missing or invalid: {}".format(source_name))
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_name == "SKILL.md.template":
            target.write_text(
                render_markdown(source.read_text(encoding="utf-8"), profile),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copyfile(source, target)

    bridge = destination / profile.bridge_path
    bridge_text = bridge.read_text(encoding="utf-8")
    bridge_text = _replace_constant(bridge_text, "SKILL_DISTRIBUTION", "agenthub" if profile.agenthub else "public")
    bridge_text = _replace_constant(bridge_text, "SKILL_NAME", profile.name)
    bridge_text = _replace_constant(bridge_text, "USER_AGENT_TEMPLATE", profile.user_agent_template)
    if profile.requirements_file:
        bridge_text = _replace_constant(bridge_text, "REQUIREMENTS_FILE", profile.requirements_file)
    ast.parse(bridge_text, filename=str(bridge), feature_version=(3, 8))
    bridge.write_text(bridge_text, encoding="utf-8", newline="\n")

    actual = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file())
    if actual != sorted(profile.output_files):
        raise SystemExit("Skill profile output contains files outside the profile whitelist")
    return destination


def sync_source_markdown(*, check: bool) -> None:
    for profile in SOURCE_DEFAULTS:
        source_root = ROOT / profile.source_directory
        template = source_root / "SKILL.md.template"
        destination = source_root / "SKILL.md"
        rendered = render_markdown(template.read_text(encoding="utf-8"), profile)
        if check:
            if not destination.is_file() or destination.read_text(encoding="utf-8") != rendered:
                raise SystemExit("generated Skill source is stale: {}".format(destination.relative_to(ROOT)))
        else:
            destination.write_text(rendered, encoding="utf-8", newline="\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--profile", choices=sorted(PROFILES))
    operation.add_argument("--sync-defaults", action="store_true")
    operation.add_argument("--check-defaults", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sync_defaults or args.check_defaults:
        if args.output is not None:
            raise SystemExit("--output is not valid with source synchronization")
        sync_source_markdown(check=args.check_defaults)
        return 0
    if args.output is None:
        raise SystemExit("--output is required with --profile")
    render_profile(args.profile, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
