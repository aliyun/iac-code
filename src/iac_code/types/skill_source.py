"""Skill source enum — shared by commands/ and skills/ to avoid circular imports."""

from enum import Enum


class SkillSource(str, Enum):
    """Where a skill was loaded from."""

    BUNDLED = "bundled"
    USER = "user"
    PROJECT = "project"
