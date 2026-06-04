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
_SED_HORIZONTAL_SPACE = " \t\r\f\v"
_SED_COMMAND_BOUNDARIES = ";\n{}"
_SED_ADDRESS_MODIFIERS = "IM"
_SED_BOUNDARY_ARGUMENT_COMMANDS = frozenset({":", "b", "t", "T", "q", "Q", "l"})
_SED_READ_FILE_COMMANDS = frozenset({"r", "R"})


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


def sed_writes_file(argv: list[str]) -> bool:
    return any(_sed_script_writes_file(script) for script in _sed_script_args(argv))


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
        if sed_writes_file(argv):
            return "sed file write"
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
    script_read_paths: list[str] = []
    flags_with_value = _flags_with_value_for("sed")
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positionals.extend(argv[i + 1 :])
            break

        if arg.startswith("--expression="):
            script_from_option = True
            script_read_paths.extend(_sed_script_read_paths(arg.split("=", 1)[1]))
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
            if i + 1 < len(argv):
                script_read_paths.extend(_sed_script_read_paths(argv[i + 1]))
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
            script_read_paths.extend(_sed_script_read_paths(arg[2:]))
            i += 1
            continue
        attached_script = _sed_attached_short_expression(arg)
        if attached_script is not None:
            script_from_option = True
            script_read_paths.extend(_sed_script_read_paths(attached_script))
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

    if not script_from_option and positionals:
        script_read_paths.extend(_sed_script_read_paths(positionals[0]))

    return paths + script_read_paths + (positionals if script_from_option else positionals[1:])


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
        attached_script = _sed_attached_short_expression(arg)
        if attached_script is not None:
            has_script_option = True
            scripts.append(attached_script)
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


def _sed_attached_short_expression(arg: str) -> str | None:
    if not arg.startswith("-") or arg.startswith("--") or len(arg) <= 2:
        return None
    option_body = arg[1:]
    expression_index = option_body.find("e")
    if expression_index == -1 or expression_index == len(option_body) - 1:
        return None
    file_index = option_body.find("f")
    if file_index != -1 and file_index < expression_index:
        return None
    return option_body[expression_index + 1 :]


def _sed_script_executes_shell(script: str) -> bool:
    return _sed_script_has_danger(script, "e")


def _sed_script_writes_file(script: str) -> bool:
    return _sed_script_has_danger(script, "w")


def _sed_script_has_danger(script: str, danger: str) -> bool:
    i = 0
    while i < len(script):
        i = _sed_next_command_start(script, i)
        if i >= len(script):
            return False

        command_index = _sed_skip_command_prefix(script, i)
        if command_index >= len(script):
            return False

        command = script[command_index]
        if command in _SED_COMMAND_BOUNDARIES:
            i = command_index + 1
            continue
        if command == "#":
            i = _sed_skip_to_line_end(script, command_index)
            continue
        if command == "e":
            if danger == "e":
                return True
            i = _sed_skip_to_command_boundary(script, command_index + 1)
            continue
        if command in {"w", "W"}:
            if danger == "w":
                return True
            i = _sed_skip_to_command_boundary(script, command_index + 1)
            continue
        if command in _SED_READ_FILE_COMMANDS:
            i = _sed_skip_to_line_end(script, command_index + 1)
            continue
        if command == "s":
            has_flag, next_index = _sed_substitute_has_flag_at(script, command_index, danger)
            if has_flag:
                return True
            i = next_index
            continue
        if command in {"a", "i", "c"}:
            i = _sed_skip_text_command(script, command_index)
            continue
        if command == "y":
            i = _sed_skip_delimited_command(script, command_index)
            continue
        if command in _SED_BOUNDARY_ARGUMENT_COMMANDS:
            i = _sed_skip_to_command_boundary(script, command_index + 1)
            continue

        i = command_index + 1

    return False


def _sed_script_read_paths(script: str) -> list[str]:
    paths: list[str] = []
    i = 0
    while i < len(script):
        i = _sed_next_command_start(script, i)
        if i >= len(script):
            return paths

        command_index = _sed_skip_command_prefix(script, i)
        if command_index >= len(script):
            return paths

        command = script[command_index]
        if command in _SED_COMMAND_BOUNDARIES:
            i = command_index + 1
            continue
        if command == "#":
            i = _sed_skip_to_line_end(script, command_index)
            continue
        if command in _SED_READ_FILE_COMMANDS:
            read_path, i = _sed_read_file_argument(script, command_index)
            if read_path and read_path != "-":
                paths.append(read_path)
            continue
        if command in {"e", "w", "W"}:
            i = _sed_skip_to_command_boundary(script, command_index + 1)
            continue
        if command == "s":
            i = _sed_skip_substitute_command(script, command_index)
            continue
        if command in {"a", "i", "c"}:
            i = _sed_skip_text_command(script, command_index)
            continue
        if command == "y":
            i = _sed_skip_delimited_command(script, command_index)
            continue
        if command in _SED_BOUNDARY_ARGUMENT_COMMANDS:
            i = _sed_skip_to_command_boundary(script, command_index + 1)
            continue

        i = command_index + 1

    return paths


def _sed_next_command_start(script: str, start: int) -> int:
    i = start
    while i < len(script):
        if script[i] in _SED_HORIZONTAL_SPACE or script[i] in _SED_COMMAND_BOUNDARIES:
            i += 1
            continue
        return i
    return i


def _sed_skip_command_prefix(script: str, start: int) -> int:
    i = _sed_skip_horizontal_space(script, start)
    first_address_end = _sed_skip_address(script, i)
    if first_address_end is not None:
        i = _sed_skip_horizontal_space(script, first_address_end)
        if i < len(script) and script[i] == ",":
            second_start = _sed_skip_horizontal_space(script, i + 1)
            second_address_end = _sed_skip_address(script, second_start)
            i = _sed_skip_horizontal_space(script, second_address_end or second_start)
        if i < len(script) and script[i] == "!":
            i = _sed_skip_horizontal_space(script, i + 1)
        return i

    if i < len(script) and script[i] == "!":
        return _sed_skip_horizontal_space(script, i + 1)
    return i


def _sed_skip_horizontal_space(script: str, start: int) -> int:
    i = start
    while i < len(script) and script[i] in _SED_HORIZONTAL_SPACE:
        i += 1
    return i


def _sed_skip_address(script: str, start: int) -> int | None:
    if start >= len(script):
        return None

    char = script[start]
    if char.isdigit():
        i = start
        while i < len(script) and script[i].isdigit():
            i += 1
        if i < len(script) and script[i] == "~":
            step_start = i + 1
            while i + 1 < len(script) and script[i + 1].isdigit():
                i += 1
            if i + 1 == step_start:
                return step_start - 1
            return i + 1
        return i
    if char == "$":
        return start + 1
    if char in {"+", "~"}:
        i = start + 1
        if i >= len(script) or not script[i].isdigit():
            return None
        while i < len(script) and script[i].isdigit():
            i += 1
        return i
    if char == "/":
        end = _skip_to_unescaped_delimiter(script, start + 1, char)
        if end is None:
            return None
        return _sed_skip_address_modifiers(script, end + 1)
    if char == "\\" and start + 1 < len(script) and script[start + 1] != "\n":
        delimiter = script[start + 1]
        if delimiter == "\\":
            return None
        end = _skip_to_unescaped_delimiter(script, start + 2, delimiter)
        if end is None:
            return None
        return _sed_skip_address_modifiers(script, end + 1)

    return None


def _sed_skip_address_modifiers(script: str, start: int) -> int:
    i = start
    while i < len(script) and script[i] in _SED_ADDRESS_MODIFIERS:
        i += 1
    return i


def _sed_substitute_has_flag_at(script: str, command_index: int, flag: str) -> tuple[bool, int]:
    if command_index + 1 >= len(script):
        return False, command_index + 1

    delimiter = script[command_index + 1]
    if delimiter == "\\" or delimiter == "\n":
        return False, command_index + 1

    pattern_end = _skip_to_unescaped_delimiter(script, command_index + 2, delimiter)
    if pattern_end is None:
        return False, _sed_skip_to_command_boundary(script, command_index + 1)

    replacement_end = _skip_to_unescaped_delimiter(script, pattern_end + 1, delimiter)
    if replacement_end is None:
        return False, _sed_skip_to_command_boundary(script, pattern_end + 1)

    i = replacement_end + 1
    while i < len(script) and script[i] not in _SED_COMMAND_BOUNDARIES and script[i] not in _SED_HORIZONTAL_SPACE:
        if script[i] == flag:
            return True, i + 1
        i += 1

    return False, _sed_skip_to_command_boundary(script, i)


def _sed_skip_substitute_command(script: str, command_index: int) -> int:
    return _sed_substitute_has_flag_at(script, command_index, "\0")[1]


def _sed_read_file_argument(script: str, command_index: int) -> tuple[str, int]:
    start = _sed_skip_horizontal_space(script, command_index + 1)
    line_end = _sed_find_line_end(script, start)
    return script[start:line_end].strip(), _sed_after_line_end(script, line_end)


def _sed_skip_text_command(script: str, command_index: int) -> int:
    i = command_index + 1
    while i < len(script) and script[i] in _SED_HORIZONTAL_SPACE:
        i += 1

    line_end = _sed_find_line_end(script, i)
    uses_literal_lines = i < len(script) and script[i] == "\\" and _sed_only_horizontal_space(script, i + 1, line_end)
    i = _sed_after_line_end(script, line_end)
    if not uses_literal_lines:
        return i

    while i < len(script):
        line_start = i
        line_end = _sed_find_line_end(script, line_start)
        continues = _sed_line_ends_with_unescaped_backslash(script, line_start, line_end)
        i = _sed_after_line_end(script, line_end)
        if not continues:
            break

    return i


def _sed_skip_delimited_command(script: str, command_index: int) -> int:
    if command_index + 1 >= len(script):
        return command_index + 1

    delimiter = script[command_index + 1]
    if delimiter == "\\" or delimiter == "\n":
        return command_index + 1

    first_end = _skip_to_unescaped_delimiter(script, command_index + 2, delimiter)
    if first_end is None:
        return _sed_skip_to_command_boundary(script, command_index + 1)

    second_end = _skip_to_unescaped_delimiter(script, first_end + 1, delimiter)
    if second_end is None:
        return _sed_skip_to_command_boundary(script, first_end + 1)

    return second_end + 1


def _sed_skip_to_command_boundary(script: str, start: int) -> int:
    i = start
    while i < len(script) and script[i] not in _SED_COMMAND_BOUNDARIES:
        i += 1
    return i


def _sed_skip_to_line_end(script: str, start: int) -> int:
    return _sed_after_line_end(script, _sed_find_line_end(script, start))


def _sed_find_line_end(script: str, start: int) -> int:
    newline = script.find("\n", start)
    return len(script) if newline == -1 else newline


def _sed_after_line_end(script: str, line_end: int) -> int:
    if line_end < len(script) and script[line_end] == "\n":
        return line_end + 1
    return line_end


def _sed_only_horizontal_space(script: str, start: int, end: int) -> bool:
    return all(char in _SED_HORIZONTAL_SPACE for char in script[start:end])


def _sed_line_ends_with_unescaped_backslash(script: str, start: int, end: int) -> bool:
    count = 0
    i = end - 1
    while i >= start and script[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


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
