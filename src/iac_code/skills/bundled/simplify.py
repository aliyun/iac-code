"""Built-in simplify skill — review changed code for reuse, quality, and efficiency."""

from iac_code.i18n import _
from iac_code.skills.bundled import register_bundled_skill

SIMPLIFY_PROMPT = """\
Review the recently changed code for:

1. **Reuse** — Are there existing functions, utilities, or patterns in the codebase that \
could replace newly added code? Search broadly.
2. **Quality** — Are there bugs, edge cases, or logic errors?
3. **Efficiency** — Can the code be simplified without losing clarity?

For each issue found:
- Explain the problem
- Show the fix (edit the file directly)

If no issues are found, say so briefly.
"""


def register_simplify_skill() -> None:
    register_bundled_skill(
        name="simplify",
        description=_("Review changed code for reuse, quality, and efficiency, then fix issues found."),
        prompt=SIMPLIFY_PROMPT,
        user_invocable=True,
    )
