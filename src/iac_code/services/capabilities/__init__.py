"""Capability registry (multimodal and future capability flags)."""

from __future__ import annotations

from iac_code.services.capabilities.multimodal import (
    MultiModalSpec,
    get_multimodal_spec,
    is_model_multimodal,
)

__all__ = [
    "MultiModalSpec",
    "get_multimodal_spec",
    "is_model_multimodal",
]
