#!/usr/bin/env python3
"""Fail when a Desktop branch changes a path outside its explicit scope."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "desktop/scope-allowlist.txt"
DESKTOP_ANCHORS = (
    "desktop/**",
    "src/iac_code/desktop/**",
    ".github/workflows/desktop.yml",
    ".github/workflows/desktop-release.yml",
    ".github/workflows/desktop-signed-package.yml",
    "docs/desktop-app-design.md",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="HEAD^", help="revision used to calculate the merge base")
    parser.add_argument("--head", default="HEAD", help="revision containing the Desktop changes")
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="include HEAD commits plus staged, unstaged, and untracked working-tree paths",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="enforce the Desktop allowlist even when only shared integration paths changed",
    )
    return parser.parse_args()


def _allowed(path: str, entries: list[str]) -> bool:
    return any(
        path == entry or (entry.endswith("/**") and (path == entry[:-3] or path.startswith(entry[:-2])))
        for entry in entries
    )


def _git_lines(*args: str) -> list[str]:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).splitlines()


def _changed_paths(merge_base: str, head: str, *, working_tree: bool) -> list[str]:
    groups = [_git_lines("diff", "--name-only", "--diff-filter=ACMRD", merge_base, head)]
    if working_tree:
        groups.extend(
            (
                _git_lines("diff", "--name-only", "--diff-filter=ACMRD"),
                _git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMRD"),
                _git_lines("ls-files", "--others", "--exclude-standard"),
            )
        )
    return list(dict.fromkeys(path for group in groups for path in group if path))


def main() -> int:
    args = _parse_args()
    entries = [
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    head = "HEAD" if args.working_tree else args.head
    merge_base = _git_lines("merge-base", args.base, head)[0]
    changed = _changed_paths(merge_base, head, working_tree=args.working_tree)
    if not args.enforce and not any(_allowed(path, list(DESKTOP_ANCHORS)) for path in changed):
        print("Desktop scope audit skipped: no Desktop-owned path changed")
        return 0
    rejected = [path for path in changed if not _allowed(path, entries)]
    print("Desktop scope audit (base {}):".format(merge_base))
    for path in changed:
        print("{} {}".format("REJECT" if path in rejected else "ALLOW", path))
    if rejected:
        raise SystemExit("Desktop scope audit rejected {} path(s)".format(len(rejected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
