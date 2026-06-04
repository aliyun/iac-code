"""Shared argv-level safety helpers for bash command classification."""

from __future__ import annotations

import os
import re

_PIP_BASE_RE = re.compile(r"pip\d+(?:\.\d+)*")
_FIND_DANGEROUS_ARGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})
_FD_DANGEROUS_ARGS = frozenset({"-x", "-X", "--exec", "--exec-batch"})
_SORT_DANGEROUS_ARGS = frozenset({"-o", "--output"})
_READ_PATH_COMMANDS = frozenset(
    {
        "ls",
        "ll",
        "la",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "tree",
        "realpath",
        "readlink",
        "md5sum",
        "sha256sum",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "find",
        "fd",
        "sed",
        "sort",
        "uniq",
        "cut",
        "diff",
        "comm",
        "jq",
        "yq",
    }
)
_GREP_LIKE_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
_FIRST_POSITIONAL_IS_PATTERN_COMMANDS = frozenset({"fd", "sed", "jq", "yq"})
_DIRECT_FILE_COMMANDS = _READ_PATH_COMMANDS - _GREP_LIKE_COMMANDS - _FIRST_POSITIONAL_IS_PATTERN_COMMANDS - {"find"}
_IMPLICIT_CURRENT_DIRECTORY_READ_COMMANDS = frozenset({"ls", "ll", "la", "tree", "du", "rg", "ag", "ack", "fd", "find"})
_FIND_EXPRESSION_FLAGS = frozenset(
    {
        "-amin",
        "-anewer",
        "-atime",
        "-cmin",
        "-cnewer",
        "-ctime",
        "-depth",
        "-empty",
        "-exec",
        "-execdir",
        "-false",
        "-gid",
        "-group",
        "-iname",
        "-inum",
        "-ipath",
        "-iregex",
        "-links",
        "-maxdepth",
        "-mindepth",
        "-mtime",
        "-name",
        "-newer",
        "-nogroup",
        "-nouser",
        "-path",
        "-perm",
        "-print",
        "-print0",
        "-prune",
        "-regex",
        "-samefile",
        "-size",
        "-true",
        "-type",
        "-uid",
        "-user",
    }
)
_FIND_GLOBAL_OPTIONS = frozenset({"-H", "-L", "-P"})
_FIND_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"-D"})
_GREP_NON_PATH_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-g",
        "-m",
        "-t",
        "--after-context",
        "--before-context",
        "--context",
        "--glob",
        "--iglob",
        "--max-count",
        "--type",
        "--type-not",
    }
)
_LS_NON_PATH_VALUE_FLAGS = frozenset(
    {
        "--block-size",
        "--format",
        "--hide",
        "--ignore",
        "--indicator-style",
        "--quoting-style",
        "--sort",
        "--time-style",
    }
)
_NON_PATH_VALUE_FLAGS_BY_COMMAND = {
    "head": frozenset({"-c", "-n", "--bytes", "--lines"}),
    "tail": frozenset({"-c", "-n", "-s", "--bytes", "--lines", "--pid", "--sleep-interval"}),
    "du": frozenset({"-B", "-d", "-t", "--block-size", "--exclude", "--max-depth", "--threshold"}),
    "df": frozenset({"-B", "-t", "-x", "--block-size", "--exclude-type", "--output", "--type"}),
    "ls": _LS_NON_PATH_VALUE_FLAGS,
    "ll": _LS_NON_PATH_VALUE_FLAGS,
    "la": _LS_NON_PATH_VALUE_FLAGS,
    "stat": frozenset({"-c", "--format", "--printf"}),
    "sort": frozenset(
        {"-k", "-S", "-T", "--batch-size", "--buffer-size", "--key", "--parallel", "--temporary-directory"}
    ),
    "uniq": frozenset({"-f", "-s", "-w", "--check-chars", "--skip-chars", "--skip-fields"}),
    "cut": frozenset(
        {"-b", "-c", "-d", "-f", "--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"}
    ),
    "fd": frozenset(
        {
            "-d",
            "-e",
            "-E",
            "-g",
            "-j",
            "-t",
            "--changed-before",
            "--changed-within",
            "--exclude",
            "--extension",
            "--glob",
            "--max-depth",
            "--min-depth",
            "--threads",
            "--type",
        }
    ),
    "jq": frozenset({"--arg", "--argjson", "--indent", "--tab"}),
    "yq": frozenset({"--indent", "--input-format", "--output-format"}),
    "grep": _GREP_NON_PATH_VALUE_FLAGS,
    "egrep": _GREP_NON_PATH_VALUE_FLAGS,
    "fgrep": _GREP_NON_PATH_VALUE_FLAGS,
    "rg": _GREP_NON_PATH_VALUE_FLAGS,
    "ag": _GREP_NON_PATH_VALUE_FLAGS,
    "ack": _GREP_NON_PATH_VALUE_FLAGS,
}
_READ_PATH_VALUE_FLAGS_BY_COMMAND = {
    "file": frozenset({"-f", "--files-from"}),
    "grep": frozenset({"-f", "--file"}),
    "egrep": frozenset({"-f", "--file"}),
    "fgrep": frozenset({"-f", "--file"}),
    "rg": frozenset({"-f", "--file"}),
    "ag": frozenset({"-f", "--file"}),
    "ack": frozenset({"-f", "--file"}),
    "fd": frozenset({"--base-directory", "--search-path"}),
    "sed": frozenset({"-f", "--file"}),
    "jq": frozenset({"-f", "--from-file"}),
    "yq": frozenset({"-f", "--from-file"}),
}
_SED_E_COMMAND_RE = re.compile(
    r"(?:^|[;\n])\s*"
    r"(?:(?:\d+|\$|/[^/\n]*(?:\\.[^/\n]*)*/)\s*)?"
    r"(?:,\s*(?:\d+|\$|/[^/\n]*(?:\\.[^/\n]*)*/)\s*)?"
    r"!?\s*"
    r"e(?:\s|$)"
)


def basename(argv0: str) -> str:
    return os.path.basename(argv0)


def pip_like_base(base: str) -> bool:
    return base == "pip" or _PIP_BASE_RE.fullmatch(base) is not None


def sed_inplace_edit(argv: list[str]) -> bool:
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg.startswith("--in-place"):
            return True
        if arg == "-i":
            return True
        if len(arg) > 2 and arg.startswith("-i"):
            return True
        if not arg.startswith("-") or arg.startswith("--"):
            i += 1
            continue
        if arg in {"-e", "-f"}:
            i += 2
            continue
        if arg.startswith("-e") or arg.startswith("-f"):
            i += 1
            continue
        if "i" in arg[1:]:
            return True
        i += 1
    return False


def sed_executes_shell(argv: list[str]) -> bool:
    return any(_sed_script_executes_shell(script) for script in _sed_script_args(argv))


def sed_uses_script_file(argv: list[str]) -> bool:
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg in {"-e", "--expression"}:
            i += 2
            continue
        if arg.startswith("--expression="):
            i += 1
            continue
        if arg.startswith("-e") and not arg.startswith("--") and len(arg) > 2:
            i += 1
            continue
        if arg in {"-f", "--file"} or arg.startswith("--file="):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return True
        if not arg.startswith("-") or arg == "-":
            break
        i += 1
    return False


def dangerous_readonly_argument(argv: list[str]) -> str | None:
    if not argv:
        return None

    base = basename(argv[0])
    if base == "find":
        return _first_matching_arg(argv[1:], _FIND_DANGEROUS_ARGS)

    if base == "fd":
        for arg in argv[1:]:
            if (
                arg in _FD_DANGEROUS_ARGS
                or arg.startswith("--exec=")
                or arg.startswith("--exec-batch=")
                or (len(arg) > 2 and (arg.startswith("-x") or arg.startswith("-X")))
            ):
                return arg
        return None

    if base == "sed":
        if sed_inplace_edit(argv):
            return "sed in-place edit"
        if sed_uses_script_file(argv):
            return "sed script file"
        if sed_executes_shell(argv):
            return "sed shell execution"
        return None

    if base == "rg":
        return _option_or_assignment(argv[1:], "--pre")

    if base == "sort":
        dangerous = _option_or_assignment(argv[1:], "--compress-program")
        if dangerous is not None:
            return dangerous
        dangerous = _option_or_assignment(argv[1:], "--output")
        if dangerous is not None:
            return dangerous
        for arg in argv[1:]:
            if arg in _SORT_DANGEROUS_ARGS or (arg.startswith("-o") and len(arg) > 2):
                return arg
        return None

    return None


def extract_read_paths(argv: list[str]) -> list[str]:
    """Extract likely read path operands from readonly-ish commands."""
    if not argv:
        return []

    base = basename(argv[0])
    if base not in _READ_PATH_COMMANDS:
        return []

    if base in _DIRECT_FILE_COMMANDS:
        return _read_positionals(argv, base)
    if base in _GREP_LIKE_COMMANDS:
        return _grep_read_paths(argv, base)
    if base == "sed":
        return _sed_read_paths(argv)
    if base in {"jq", "yq"}:
        return _jq_read_paths(argv, base)
    if base == "fd":
        return _fd_read_paths(argv)
    if base == "find":
        return _find_read_paths(argv)
    return []


def command_implicitly_reads_current_directory(argv: list[str]) -> bool:
    if not argv:
        return False
    base = basename(argv[0])
    return base in _IMPLICIT_CURRENT_DIRECTORY_READ_COMMANDS and not extract_read_paths(argv)


def _looks_like_path(token: str) -> bool:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    return (
        os.path.isabs(token)
        or token in {".", ".."}
        or token.startswith("~/")
        or token.startswith("./")
        or token.startswith("../")
        or "/" in token
    )


def _read_positionals(argv: list[str], base: str) -> list[str]:
    positionals: list[str] = []
    flags_with_value = _flags_with_value_for(base)
    read_path_flags = _read_path_value_flags_for(base)
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        option = _long_option_name(arg)
        if option in read_path_flags:
            value = arg.split("=", 1)[1]
            if value != "-":
                positionals.append(value)
            i += 1
            continue
        if option in flags_with_value:
            i += 1
            continue

        attached_read_path = _attached_short_option_value(arg, read_path_flags)
        if attached_read_path is not None:
            if attached_read_path != "-":
                positionals.append(attached_read_path)
            i += 1
            continue

        short_option = _attached_short_option(arg, flags_with_value)
        if short_option is not None:
            i += 1
            continue

        if arg in read_path_flags:
            if i + 1 < len(argv) and argv[i + 1] != "-":
                positionals.append(argv[i + 1])
            i += 2
            continue

        if arg in flags_with_value:
            i += 2
            continue

        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        positionals.append(arg)
        i += 1

    return positionals


def _find_read_paths(argv: list[str]) -> list[str]:
    roots: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in _FIND_GLOBAL_OPTIONS:
            i += 1
            continue
        if arg in _FIND_GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-O") and len(arg) > 2:
            i += 1
            continue
        break

    for arg in argv[i:]:
        if arg == "--":
            continue
        if _is_find_expression_token(arg):
            break
        roots.append(arg)
    return roots


def _is_find_expression_token(arg: str) -> bool:
    return arg in {"!", "(", ")"} or arg in _FIND_EXPRESSION_FLAGS or arg.startswith("-")


def _grep_read_paths(argv: list[str], base: str) -> list[str]:
    pattern_from_option = False
    no_pattern_mode = False
    positionals: list[str] = []
    paths: list[str] = []
    flags_with_value = _flags_with_value_for(base)
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        if base == "rg" and arg == "--files":
            no_pattern_mode = True
            i += 1
            continue
        if arg.startswith("--file="):
            pattern_from_option = True
            paths.append(arg.split("=", 1)[1])
            i += 1
            continue
        if arg.startswith("--regexp="):
            pattern_from_option = True
            i += 1
            continue

        option = _long_option_name(arg)
        if option in flags_with_value:
            i += 1
            continue

        if arg in {"-f", "--file"}:
            pattern_from_option = True
            if i + 1 < len(argv):
                paths.append(argv[i + 1])
            i += 2
            continue
        if arg in {"-e", "--regexp"}:
            pattern_from_option = True
            i += 2
            continue
        if arg.startswith("-f") and len(arg) > 2:
            pattern_from_option = True
            paths.append(arg[2:])
            i += 1
            continue
        if arg.startswith("-e") and len(arg) > 2:
            pattern_from_option = True
            i += 1
            continue

        short_option = _attached_short_option(arg, flags_with_value)
        if short_option is not None:
            i += 1
            continue

        if arg in flags_with_value:
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        positionals.append(arg)
        i += 1

    return paths + (positionals if pattern_from_option or no_pattern_mode else positionals[1:])


def _fd_read_paths(argv: list[str]) -> list[str]:
    positionals: list[str] = []
    paths: list[str] = []
    flags_with_value = _flags_with_value_for("fd")
    read_path_flags = _read_path_value_flags_for("fd")
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        option = _long_option_name(arg)
        if option in read_path_flags:
            value = arg.split("=", 1)[1]
            if value != "-":
                paths.append(value)
            i += 1
            continue
        if option in flags_with_value:
            i += 1
            continue

        if arg in read_path_flags:
            if i + 1 < len(argv) and argv[i + 1] != "-":
                paths.append(argv[i + 1])
            i += 2
            continue

        short_option = _attached_short_option(arg, flags_with_value)
        if short_option is not None:
            i += 1
            continue

        if arg in flags_with_value:
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        positionals.append(arg)
        i += 1

    return paths + positionals[1:]


def _sed_read_paths(argv: list[str]) -> list[str]:
    script_from_option = False
    positionals: list[str] = []
    paths: list[str] = []
    flags_with_value = _flags_with_value_for("sed")
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        if arg.startswith("--expression="):
            script_from_option = True
            i += 1
            continue
        if arg.startswith("--file="):
            script_from_option = True
            paths.append(arg.split("=", 1)[1])
            i += 1
            continue

        option = _long_option_name(arg)
        if option in flags_with_value:
            i += 1
            continue

        if arg in {"-e", "--expression"}:
            script_from_option = True
            i += 2
            continue
        if arg in {"-f", "--file"}:
            script_from_option = True
            if i + 1 < len(argv):
                paths.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("-e") and len(arg) > 2:
            script_from_option = True
            i += 1
            continue
        if arg.startswith("-f") and len(arg) > 2:
            script_from_option = True
            paths.append(arg[2:])
            i += 1
            continue

        short_option = _attached_short_option(arg, flags_with_value)
        if short_option is not None:
            i += 1
            continue

        if arg in flags_with_value:
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        positionals.append(arg)
        i += 1

    return paths + (positionals if script_from_option else positionals[1:])


def _jq_read_paths(argv: list[str], base: str) -> list[str]:
    script_from_option = False
    positionals: list[str] = []
    paths: list[str] = []
    flags_with_value = _flags_with_value_for(base)
    read_path_flags = _read_path_value_flags_for(base)
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        option = _long_option_name(arg)
        if option in read_path_flags:
            script_from_option = True
            paths.append(arg.split("=", 1)[1])
            i += 1
            continue
        if option in flags_with_value:
            i += 1
            continue

        attached_read_path = _attached_short_option_value(arg, read_path_flags)
        if attached_read_path is not None:
            script_from_option = True
            paths.append(attached_read_path)
            i += 1
            continue

        if arg in read_path_flags:
            script_from_option = True
            if i + 1 < len(argv):
                paths.append(argv[i + 1])
            i += 2
            continue

        short_option = _attached_short_option(arg, flags_with_value)
        if short_option is not None:
            i += 1
            continue

        if arg in flags_with_value:
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        positionals.append(arg)
        i += 1

    return paths + (positionals if script_from_option else positionals[1:])


def _flags_with_value_for(base: str) -> frozenset[str]:
    return _NON_PATH_VALUE_FLAGS_BY_COMMAND.get(base, frozenset())


def _read_path_value_flags_for(base: str) -> frozenset[str]:
    return _READ_PATH_VALUE_FLAGS_BY_COMMAND.get(base, frozenset())


def _long_option_name(arg: str) -> str | None:
    if not arg.startswith("--") or "=" not in arg:
        return None
    return arg.split("=", 1)[0]


def _attached_short_option(arg: str, flags_with_value: frozenset[str]) -> str | None:
    if not arg.startswith("-") or arg.startswith("--") or len(arg) <= 2:
        return None
    for flag in flags_with_value:
        if len(flag) == 2 and arg.startswith(flag):
            return flag
    return None


def _attached_short_option_value(arg: str, flags_with_value: frozenset[str]) -> str | None:
    if not arg.startswith("-") or arg.startswith("--") or len(arg) <= 2:
        return None
    for flag in flags_with_value:
        if len(flag) == 2 and arg.startswith(flag):
            return arg[len(flag) :]
    return None


def _first_matching_arg(args: list[str], dangerous: frozenset[str]) -> str | None:
    for arg in args:
        if arg in dangerous:
            return arg
    return None


def _option_or_assignment(args: list[str], option: str) -> str | None:
    for arg in args:
        if arg == option or arg.startswith("{}=".format(option)):
            return arg
    return None


def _sed_script_args(argv: list[str]) -> list[str]:
    scripts: list[str] = []
    has_script_option = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            if not has_script_option and i + 1 < len(argv):
                scripts.append(argv[i + 1])
            break

        if arg in {"-e", "--expression"}:
            has_script_option = True
            if i + 1 < len(argv):
                scripts.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--expression="):
            has_script_option = True
            scripts.append(arg.split("=", 1)[1])
            i += 1
            continue
        if arg.startswith("-e") and len(arg) > 2:
            has_script_option = True
            scripts.append(arg[2:])
            i += 1
            continue

        if arg in {"-f", "--file"}:
            has_script_option = True
            i += 2
            continue
        if arg.startswith("--file=") or (arg.startswith("-f") and len(arg) > 2):
            has_script_option = True
            i += 1
            continue

        if arg.startswith("-") and arg != "-":
            i += 1
            continue

        if not has_script_option:
            scripts.append(arg)
        break

    return scripts


def _sed_script_executes_shell(script: str) -> bool:
    return bool(_SED_E_COMMAND_RE.search(script) or _sed_substitute_has_exec_flag(script))


def _sed_substitute_has_exec_flag(script: str) -> bool:
    for i, char in enumerate(script):
        if char != "s" or i + 1 >= len(script):
            continue
        delimiter = script[i + 1]
        if delimiter.isalnum() or delimiter.isspace() or delimiter == "\\":
            continue
        pattern_end = _skip_to_unescaped_delimiter(script, i + 2, delimiter)
        if pattern_end is None:
            continue
        replacement_end = _skip_to_unescaped_delimiter(script, pattern_end + 1, delimiter)
        if replacement_end is None:
            continue
        flags = []
        j = replacement_end + 1
        while j < len(script) and script[j].isalpha():
            flags.append(script[j])
            j += 1
        if "e" in flags:
            return True
    return False


def _skip_to_unescaped_delimiter(text: str, start: int, delimiter: str) -> int | None:
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == delimiter:
            return i
    return None
