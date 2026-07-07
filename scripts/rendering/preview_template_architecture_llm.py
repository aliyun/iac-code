#!/usr/bin/env python3
"""Preview a ROS template architecture diagram with a real LLM semantic pass."""

from __future__ import annotations

from iac_code.pipeline.engine import architecture_semantic_planning as _impl

globals().update(
    {
        name: getattr(_impl, name)
        for name in dir(_impl)
        if not (name.startswith("__") and name.endswith("__"))
    }
)


def _call_with_patchable_hooks(function, *args, **kwargs):
    hook_names = ("_render_terminal_rich", "_svg_to_png_command")
    original_hooks = {name: getattr(_impl, name) for name in hook_names}
    try:
        for name in hook_names:
            if name in globals():
                setattr(_impl, name, globals()[name])
        return function(*args, **kwargs)
    finally:
        for name, value in original_hooks.items():
            setattr(_impl, name, value)


def write_terminal_svg(*args, **kwargs):
    return _call_with_patchable_hooks(_impl.write_terminal_svg, *args, **kwargs)


def write_terminal_png(*args, **kwargs):
    return _call_with_patchable_hooks(_impl.write_terminal_png, *args, **kwargs)


def convert_svg_to_png(*args, **kwargs):
    return _call_with_patchable_hooks(_impl.convert_svg_to_png, *args, **kwargs)


if __name__ == "__main__":
    _impl.main()
