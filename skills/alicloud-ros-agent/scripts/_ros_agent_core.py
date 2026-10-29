# Core state, policy, transport, and stream parsing source shard.
# Loaded by ros_agent.py into its shared module namespace; do not execute directly.
# ruff: noqa: F821 -- names are provided by the shared ros_agent.py namespace.

class BridgeError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _session_backup_not_ready_error(value: Any) -> Optional[BridgeError]:
    if not isinstance(value, dict):
        return None
    raw_error = value.get("error") if isinstance(value.get("error"), dict) else value
    data = raw_error.get("data") if isinstance(raw_error.get("data"), dict) else {}
    code = data.get("code") or raw_error.get("code") or raw_error.get("Code")
    if code != SESSION_BACKUP_NOT_READY_CODE:
        return None
    message = raw_error.get("message") or raw_error.get("Message")
    if not isinstance(message, str) or not message:
        message = "Session backup is still synchronizing. Retry after 3 seconds."
    return BridgeError(SESSION_BACKUP_NOT_READY_CODE, sanitize_text(message, 2000), True)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _resolve_user_owned_path(raw_path: str, code: str, label: str) -> pathlib.Path:
    """Resolve a local path and confine it to the user's home or temp tree."""

    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    normalized = os.path.normcase(os.path.realpath(expanded))
    allowed_roots = (
        os.path.normcase(os.path.realpath(str(pathlib.Path.home()))),
        os.path.normcase(os.path.realpath(tempfile.gettempdir())),
    )
    for allowed_root in allowed_roots:
        prefix = allowed_root.rstrip(os.sep) + os.sep
        if normalized.startswith(prefix):
            return pathlib.Path(normalized)
    raise BridgeError(code, "{} must be inside the current user's home or temporary directory.".format(label))


def _state_root() -> pathlib.Path:
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return _resolve_user_owned_path(configured, "invalid_config", "The ROS Agent state directory")
    return pathlib.Path(os.path.expanduser("~/.cache/alicloud-ros-agent")).resolve()


def _secure_directory(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(str(path), 0o700)


def _atomic_json(path: pathlib.Path, value: Dict[str, Any], mode: int = 0o600) -> None:
    _atomic_bytes(path, _json_bytes(value), mode)


def _atomic_bytes(path: pathlib.Path, value: bytes, mode: int = 0o600) -> None:
    _secure_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, mode)
        for attempt in range(10):
            try:
                os.replace(temporary, str(path))
                break
            except PermissionError:
                # Windows readers/antivirus can briefly deny delete sharing.
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.02)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _load_state_json(path: pathlib.Path, code: str = "job_not_found") -> Dict[str, Any]:
    try:
        for attempt in range(10):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    value = json.load(handle)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.02)
    except (OSError, ValueError) as exc:
        raise BridgeError(code, "Local ROS Agent bridge state is unavailable or invalid.") from exc
    if not isinstance(value, dict):
        raise BridgeError(code, "Local ROS Agent bridge state is unavailable or invalid.")
    return value


class StateLock(object):
    def __init__(self, path: pathlib.Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self.handle = None  # type: Any

    def __enter__(self) -> "StateLock":
        _secure_directory(self.path.parent)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (IOError, OSError) as exc:
                if getattr(exc, "errno", None) not in {None, errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise BridgeError("state_locked", "Another ROS Agent bridge process is updating this state.", True)
                time.sleep(0.05)

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _job_paths(job_id: str) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    try:
        canonical_job_id = uuid.UUID(job_id).hex
    except (AttributeError, ValueError) as exc:
        raise BridgeError("job_not_found", "The requested ROS Agent job does not exist.") from exc
    if canonical_job_id != job_id:
        raise BridgeError("job_not_found", "The requested ROS Agent job does not exist.")
    root = _state_root() / "jobs" / canonical_job_id
    return root, root / "job.json", root / "events.jsonl"


def _preferred_language(text: str) -> str:
    if re.search(r"[\u3400-\u9fff]", text):
        return "zh"
    return "en"


def _endpoint_kind(endpoint: str, error_code: str = "invalid_input") -> str:
    if len(endpoint) <= 253 and endpoint.endswith(".aliyuncs.com"):
        labels = endpoint.split(".")
        if all(
            label
            and label.isascii()
            and len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        ):
            return "aliyun"
    host, separator, port_text = endpoint.rpartition(":")
    if separator and host in {"localhost", "127.0.0.1"} and port_text.isascii() and port_text.isdigit():
        port = int(port_text)
        if 1 <= port <= 65535:
            return "loopback"
    raise BridgeError(
        error_code,
        "The endpoint must be an aliyuncs.com hostname or a loopback host and port, without a URL scheme or path.",
    )


def load_skill_config(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    config_path = path if path is not None else SKILL_CONFIG_PATH
    try:
        data = config_path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BridgeError("invalid_config", "The Skill config.json could not be read.") from exc
    if len(data) > MAX_CONFIG_BYTES:
        raise BridgeError("invalid_config", "The Skill config.json is too large.")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BridgeError("invalid_config", "The Skill config.json must contain valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise BridgeError("invalid_config", "The Skill config.json must contain a JSON object.")
    unknown = set(value) - {
        "endpoint",
        "allowedAgentModes",
        "managerIdleSeconds",
        "transport",
        "aliyunCLIExecutionMode",
        "aliyunCLIForwardEnv",
        "enableThinking",
        "aliyunCLIProfile",
        "debug",
    }
    if unknown:
        raise BridgeError("invalid_config", "The Skill config.json contains unsupported fields.")

    result = {}  # type: Dict[str, Any]
    if "debug" in value:
        if not isinstance(value["debug"], bool):
            raise BridgeError("invalid_config", "debug must be true or false.")
        result["debug"] = value["debug"]
    if "transport" in value:
        transport = value["transport"]
        if not isinstance(transport, str) or transport not in SUPPORTED_TRANSPORTS:
            raise BridgeError("invalid_config", "transport must be code or aliyun_cli.")
        result["transport"] = transport

    if "aliyunCLIExecutionMode" in value:
        execution_mode = value["aliyunCLIExecutionMode"]
        if not isinstance(execution_mode, str) or execution_mode not in SUPPORTED_ALIYUN_CLI_EXECUTION_MODES:
            raise BridgeError("invalid_config", "aliyunCLIExecutionMode must be local or remote.")
        result["aliyunCLIExecutionMode"] = execution_mode

    if "aliyunCLIForwardEnv" in value:
        result["aliyunCLIForwardEnv"] = _validate_remote_cli_forward_env_names(
            value["aliyunCLIForwardEnv"],
            "invalid_config",
        )

    if "endpoint" in value:
        endpoint = value["endpoint"]
        if not isinstance(endpoint, str) or not endpoint.strip() or endpoint != endpoint.strip():
            raise BridgeError("invalid_config", "The config endpoint must be a non-empty string without padding.")
        _endpoint_kind(endpoint, "invalid_config")
        result["endpoint"] = endpoint

    if "allowedAgentModes" in value:
        modes = value["allowedAgentModes"]
        if not isinstance(modes, list) or not modes:
            raise BridgeError("invalid_config", "allowedAgentModes must be a non-empty JSON array.")
        if any(not isinstance(mode, str) or mode not in SUPPORTED_AGENT_MODES for mode in modes):
            raise BridgeError("invalid_config", "allowedAgentModes may contain only normal and pipeline.")
        if len(set(modes)) != len(modes):
            raise BridgeError("invalid_config", "allowedAgentModes must not contain duplicates.")
        result["allowedAgentModes"] = modes

    if "managerIdleSeconds" in value:
        idle_seconds = value["managerIdleSeconds"]
        if (
            isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, int)
            or not 1 <= idle_seconds <= MAX_MANAGER_IDLE_SECONDS
        ):
            raise BridgeError(
                "invalid_config",
                "managerIdleSeconds must be an integer from 1 through {}.".format(MAX_MANAGER_IDLE_SECONDS),
            )
        result["managerIdleSeconds"] = idle_seconds

    if "enableThinking" in value:
        enable_thinking = value["enableThinking"]
        if not isinstance(enable_thinking, bool):
            raise BridgeError("invalid_config", "enableThinking must be true or false.")
        result["enableThinking"] = enable_thinking

    if "aliyunCLIProfile" in value:
        profile = value["aliyunCLIProfile"]
        if (
            not isinstance(profile, str)
            or profile != profile.strip()
            or len(profile.encode("utf-8")) > 200
            or any(character in profile for character in "\r\n\0")
        ):
            raise BridgeError(
                "invalid_config",
                "aliyunCLIProfile must be an empty or non-padded Profile name of at most 200 bytes.",
            )
        result["aliyunCLIProfile"] = profile

    execution_mode = result.get("aliyunCLIExecutionMode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE)
    transport = result.get("transport", DEFAULT_TRANSPORT)
    if "aliyunCLIExecutionMode" in result and transport != "aliyun_cli":
        raise BridgeError(
            "invalid_config",
            "aliyunCLIExecutionMode may be configured only when transport is aliyun_cli.",
        )
    if "aliyunCLIForwardEnv" in result and not (transport == "aliyun_cli" and execution_mode == "remote"):
        raise BridgeError(
            "invalid_config",
            "aliyunCLIForwardEnv may be configured only for remote aliyun CLI execution.",
        )
    if execution_mode == "remote":
        if result.get("aliyunCLIProfile"):
            raise BridgeError(
                "invalid_config",
                "aliyunCLIProfile is not available when aliyunCLIExecutionMode is remote.",
            )
        endpoint = result.get("endpoint")
        if isinstance(endpoint, str) and _endpoint_kind(endpoint, "invalid_config") != "aliyun":
            raise BridgeError(
                "invalid_config",
                "Remote aliyun CLI execution requires a public aliyuncs.com endpoint.",
            )
    return result


def apply_skill_config(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    configured_endpoint = config.get("endpoint")
    allowed_modes = config.get("allowedAgentModes", sorted(SUPPORTED_AGENT_MODES))
    transport = config.get("transport", DEFAULT_TRANSPORT)
    cli_execution_mode = config.get("aliyunCLIExecutionMode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE)
    enable_thinking = config.get("enableThinking", True)
    configured_profile = config.get("aliyunCLIProfile", "")
    forwarded_environment = config.get("aliyunCLIForwardEnv", list(DEFAULT_REMOTE_CLI_FORWARD_ENV))
    args.manager_idle_seconds = config.get("managerIdleSeconds", MANAGER_IDLE_SECONDS)
    args.debug = config.get("debug", False)
    args.enable_thinking = enable_thinking
    args.aliyun_cli_profile = configured_profile
    args.aliyun_cli_execution_mode = cli_execution_mode
    args.aliyun_cli_forward_env = (
        list(forwarded_environment) if transport == "aliyun_cli" and cli_execution_mode == "remote" else []
    )
    args.profile_pinned = bool(configured_profile)
    if args.command == "check":
        args.endpoint = configured_endpoint or DEFAULT_ENDPOINT
        args.allowed_agent_modes = list(allowed_modes)
        args.transport = transport
        args.profile = configured_profile or None
        return
    if args.command not in {"chat", "start"}:
        return
    requested_endpoint = args.endpoint
    if configured_endpoint and requested_endpoint and configured_endpoint != requested_endpoint:
        raise BridgeError("config_conflict", "--endpoint conflicts with the endpoint fixed by Skill config.json.")
    args.endpoint = configured_endpoint or requested_endpoint or DEFAULT_ENDPOINT
    args.transport = transport
    endpoint_kind = _endpoint_kind(args.endpoint, "invalid_config" if configured_endpoint else "invalid_input")
    if transport == "aliyun_cli" and cli_execution_mode == "remote" and endpoint_kind != "aliyun":
        raise BridgeError("invalid_input", "Remote aliyun CLI execution requires a public aliyuncs.com endpoint.")
    if args.mode not in allowed_modes:
        raise BridgeError("mode_not_allowed", "Agent mode {} is not allowed by Skill config.json.".format(args.mode))
    requested_profile = getattr(args, "profile", None)
    if transport == "aliyun_cli" and cli_execution_mode == "remote" and requested_profile:
        raise BridgeError("config_conflict", "--profile is not available with remote aliyun CLI execution.")
    if configured_profile and requested_profile and requested_profile != configured_profile:
        raise BridgeError("config_conflict", "--profile conflicts with aliyunCLIProfile fixed by Skill config.json.")
    args.profile = configured_profile or requested_profile
    if transport == "aliyun_cli" and getattr(args, "client_context_file", None):
        raise BridgeError("unsupported_input", "The ROS CLI plugin does not support ClientContext.")
    if getattr(args, "no_thinking", False) and enable_thinking:
        raise BridgeError("config_conflict", "--no-thinking conflicts with enableThinking fixed by Skill config.json.")
    args.no_thinking = not enable_thinking


def _validate_remote_cli_forward_env_names(value: Any, error_code: str = "invalid_input") -> List[str]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REMOTE_CLI_FORWARD_ENV_NAMES:
        raise BridgeError(
            error_code,
            "aliyunCLIForwardEnv must be an array of at most {} environment variable names.".format(
                MAX_REMOTE_CLI_FORWARD_ENV_NAMES
            ),
        )
    result = []  # type: List[str]
    for item in value:
        if not isinstance(item, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", item) is None:
            raise BridgeError(error_code, "aliyunCLIForwardEnv contains an invalid environment variable name.")
        normalized = re.sub(r"[^a-z0-9]", "", item.lower())
        if any(part in normalized for part in SENSITIVE_ENV_NAME_PARTS):
            raise BridgeError(error_code, "aliyunCLIForwardEnv must not include credential or secret variables.")
        if item in result:
            raise BridgeError(error_code, "aliyunCLIForwardEnv must not contain duplicates.")
        result.append(item)
    return result


def _capture_remote_cli_environment(names: Iterable[str]) -> Dict[str, str]:
    result = {}  # type: Dict[str, str]
    total = 0
    for name in _validate_remote_cli_forward_env_names(list(names)):
        value = os.environ.get(name)
        if value is None:
            continue
        size = len(value.encode("utf-8"))
        if "\0" in value or size > MAX_REMOTE_CLI_ENV_VALUE_BYTES:
            raise BridgeError("invalid_input", "A forwarded remote CLI environment value is invalid or too large.")
        total += size
        if total > MAX_REMOTE_CLI_ENV_BYTES:
            raise BridgeError("invalid_input", "The forwarded remote CLI environment is too large.")
        result[name] = value
    return result


def _remote_cli_environment_from_payload(job: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, str]:
    legacy_default = (
        list(DEFAULT_REMOTE_CLI_FORWARD_ENV)
        if job.get("transport", "aliyun_cli") == "aliyun_cli"
        and job.get("aliyunCLIExecutionMode") == "remote"
        else []
    )
    allowed = _validate_remote_cli_forward_env_names(job.get("aliyunCLIForwardEnv", legacy_default))
    value = payload.get("transientEnvironment", {})
    if not isinstance(value, dict):
        raise BridgeError("invalid_input", "The transient remote CLI environment is invalid.")
    if set(value) - set(allowed):
        raise BridgeError("invalid_input", "The transient remote CLI environment contains an unapproved name.")
    result = {}  # type: Dict[str, str]
    total = 0
    for name, item in value.items():
        if not isinstance(item, str) or "\0" in item:
            raise BridgeError("invalid_input", "A transient remote CLI environment value is invalid.")
        size = len(item.encode("utf-8"))
        if size > MAX_REMOTE_CLI_ENV_VALUE_BYTES:
            raise BridgeError("invalid_input", "A transient remote CLI environment value is too large.")
        total += size
        if total > MAX_REMOTE_CLI_ENV_BYTES:
            raise BridgeError("invalid_input", "The transient remote CLI environment is too large.")
        result[name] = item
    return result


def _remote_cli_subprocess_environment(names: Iterable[str], values: Dict[str, str]) -> Dict[str, str]:
    environment = dict(os.environ)
    for name in _validate_remote_cli_forward_env_names(list(names)):
        environment.pop(name, None)
    environment.update(values)
    return environment


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", "ignore")


def sanitize_text(value: Any, maximum: int = 4000, preserve_lines: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = SECRET_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = "".join(character for character in value if character in "\n\r\t" or ord(character) >= 32)
    if not preserve_lines:
        value = " ".join(value.split())
    return _truncate_utf8(value, maximum)


def _workspace(raw_path: Optional[str] = None) -> pathlib.Path:
    path = (
        _resolve_user_owned_path(raw_path, "invalid_input", "The workspace")
        if raw_path is not None
        else pathlib.Path.cwd().resolve()
    )
    if not path.is_dir():
        raise BridgeError("invalid_input", "The workspace must be an existing directory.")
    return path


def _trusted_manager_workspace(raw_path: str) -> pathlib.Path:
    """Resolve a manager workspace under the same user-owned roots as the CLI."""

    path = _resolve_user_owned_path(raw_path, "invalid_input", "The workspace")
    if not path.is_dir():
        raise BridgeError("invalid_input", "The workspace must be an existing directory.")
    return path


def _read_workspace_file(workspace: pathlib.Path, raw_path: str, maximum: int, label: str) -> str:
    workspace_path = os.path.normcase(os.path.realpath(str(workspace)))
    resolved_path = os.path.normcase(os.path.realpath(os.path.expanduser(raw_path)))
    if not resolved_path.startswith(workspace_path.rstrip(os.sep) + os.sep):
        raise BridgeError("invalid_input", "{} must be inside the workspace.".format(label))
    path = pathlib.Path(resolved_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BridgeError("invalid_input", "{} could not be read.".format(label)) from exc
    if len(data) > maximum:
        raise BridgeError("invalid_input", "{} is too large.".format(label))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError("invalid_input", "{} must be UTF-8.".format(label)) from exc


def read_prompt(workspace: pathlib.Path, raw_path: str) -> str:
    prompt = _read_workspace_file(workspace, raw_path, MAX_PROMPT_BYTES, "The prompt file")
    if not prompt.strip():
        raise BridgeError("invalid_input", "The prompt file must not be empty.")
    return prompt


def _load_json_file(workspace: pathlib.Path, raw_path: str, maximum: int, label: str) -> Any:
    text = _read_workspace_file(workspace, raw_path, maximum, label)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BridgeError("invalid_input", "{} must contain valid JSON.".format(label)) from exc


def _contains_sensitive_client_context_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(part in normalized for part in SENSITIVE_CLIENT_CONTEXT_KEY_PARTS):
                return True
            if _contains_sensitive_client_context_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_client_context_key(item) for item in value)
    return False


def load_client_context(workspace: pathlib.Path, raw_path: Optional[str]) -> Optional[str]:
    if not raw_path:
        return None
    value = _load_json_file(workspace, raw_path, MAX_CONTEXT_BYTES, "The client context file")
    if not isinstance(value, dict):
        raise BridgeError("invalid_input", "The client context must be a JSON object.")
    if _contains_sensitive_client_context_key(value):
        raise BridgeError("invalid_input", "The client context must not contain credential or secret fields.")
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(compact.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise BridgeError("invalid_input", "The compact client context is too large.")
    return compact


def _attachment_value(value: Dict[str, Any], *names: str) -> Optional[str]:
    for name in names:
        item = value.get(name)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def load_attachments(workspace: pathlib.Path, raw_path: Optional[str]) -> List[Dict[str, str]]:
    if not raw_path:
        return []
    value = _load_json_file(workspace, raw_path, MAX_CONTEXT_BYTES, "The attachments file")
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise BridgeError("invalid_input", "Attachments must be a JSON array with at most five items.")
    result = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise BridgeError("invalid_input", "Attachment {} must be an object.".format(index))
        attachment_type = _attachment_value(item, "Type", "type") or "image"
        mime_type = _attachment_value(item, "MimeType", "mimeType", "mime_type")
        object_key = _attachment_value(item, "OssObjectKey", "ossObjectKey", "oss_object_key")
        name = _attachment_value(item, "Name", "name")
        if attachment_type != "image":
            raise BridgeError("invalid_input", "Attachment {} must have Type image.".format(index))
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise BridgeError("invalid_input", "Attachment {} has an unsupported MimeType.".format(index))
        if not object_key:
            raise BridgeError("invalid_input", "Attachment {} requires OssObjectKey.".format(index))
        projected = {"Type": attachment_type, "MimeType": mime_type, "OssObjectKey": object_key}
        if name:
            projected["Name"] = name
        result.append(projected)
    return result


def load_permission_query(
    workspace: pathlib.Path,
    raw_path: str,
    decision: str,
    session_id: str,
    mode: str,
) -> Tuple[str, Dict[str, str]]:
    value = _load_json_file(workspace, raw_path, MAX_CONTEXT_BYTES, "The permission input file")
    return build_permission_query(value, decision, session_id, mode)


def build_permission_query(
    value: Any,
    decision: str,
    session_id: str,
    mode: str,
) -> Tuple[str, Dict[str, str]]:
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("kind") != "permission":
        raise BridgeError("invalid_input", "The pending input must be a schemaVersion 1 permission.")
    if decision not in PERMISSION_DECISIONS:
        raise BridgeError("invalid_input", "The permission decision must be allow_once or deny.")
    correlation = {}
    for key in ("requestTaskId", "contextId", "inputId", "toolUseId"):
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise BridgeError("invalid_input", "The permission input file requires {}.".format(key))
        correlation[key] = item
    if correlation["contextId"] != session_id:
        raise BridgeError("invalid_input", "The permission contextId must match --session-id.")
    permission_class = value.get("permissionClass")
    allowed_classes = {"pipeline", "sub_pipeline"} if mode == "pipeline" else {"normal"}
    if permission_class is not None and permission_class not in allowed_classes:
        raise BridgeError("invalid_input", "The permissionClass does not match --mode.")
    payload = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": correlation["requestTaskId"],
        "contextId": correlation["contextId"],
        "inputId": correlation["inputId"],
        "toolUseId": correlation["toolUseId"],
        "decision": decision,
    }
    query = "{} {}".format(
        PERMISSION_QUERY_PREFIX,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )
    return query, {**correlation, "decision": decision}


def resolve_aliyun(raw_path: str) -> str:
    expanded = os.path.expanduser(raw_path)
    resolved = shutil.which(expanded)
    if not resolved:
        raise BridgeError("cli_not_found", "Alibaba Cloud CLI is not installed or is not on PATH.")
    return os.path.abspath(resolved)


def build_start_chat_parameters(
    args: argparse.Namespace,
    prompt: str,
    client_context: Optional[str],
    attachments: List[Dict[str, str]],
) -> Dict[str, str]:
    parameters = {
        "Query": prompt,
        "AgentVersion": "V2",
        "EnablePartialMessage": "true",
        "EnableThinking": "false" if args.no_thinking else "true",
        "Mode": "IaCCodePipeline" if args.mode == "pipeline" else "IaCCodeNormal",
    }
    if args.session_id:
        parameters["SessionId"] = args.session_id
    if args.region_id:
        parameters["RegionId"] = args.region_id
    if client_context is not None:
        parameters["ClientContext"] = client_context
    for index, attachment in enumerate(attachments, start=1):
        for field in ("Type", "MimeType", "Name", "OssObjectKey"):
            if field in attachment:
                parameters["Attachments.{}.{}".format(index, field)] = attachment[field]
    return parameters


def build_reconnect_start_chat_parameters(session_id: str, cursor: str) -> Dict[str, str]:
    return {
        "AgentVersion": "V2",
        "SessionId": session_id,
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": cursor,
    }


def build_command(
    args: argparse.Namespace,
    prompt: str,
    client_context: Optional[str],
    attachments: List[Dict[str, str]],
) -> List[str]:
    if client_context is not None:
        raise BridgeError("unsupported_input", "The ROS CLI plugin does not support ClientContext.")
    endpoint_kind = _endpoint_kind(args.endpoint or "")
    execution_mode = getattr(args, "aliyun_cli_execution_mode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE)
    if execution_mode == "remote" and endpoint_kind != "aliyun":
        raise BridgeError("invalid_input", "Remote aliyun CLI execution requires a public aliyuncs.com endpoint.")
    if execution_mode == "remote" and args.profile:
        raise BridgeError("invalid_input", "Remote aliyun CLI execution does not accept a local Profile.")
    command = [
        resolve_aliyun(args.aliyun_path),
        "ros",
        "start-chat",
        "--endpoint",
        args.endpoint,
        "--connect-timeout",
        str(args.connect_timeout),
        "--read-timeout",
        str(args.read_timeout),
        "--user-agent",
        USER_AGENT,
    ]
    if endpoint_kind == "loopback":
        command.extend(["--secure", "--skip-secure-verify"])
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.region_id:
        command.extend(["--region", args.region_id])
    if execution_mode == "remote":
        # Host command validators may reject literal line breaks inside an argument.
        prompt = re.sub(r"\r\n|\r|\n", " ", prompt.rstrip("\r\n"))
    command.extend(
        [
            "--query",
            prompt,
            "--agent-version",
            "V2",
            "--enable-partial-message",
            "true",
            "--enable-thinking",
            "false" if args.no_thinking else "true",
            "--biz-mode",
            "IaCCodePipeline" if args.mode == "pipeline" else "IaCCodeNormal",
        ]
    )
    if args.session_id:
        command.extend(["--session-id", args.session_id])
    elif execution_mode == "remote" and getattr(args, "client_reference_id", None):
        command.extend(["--body", _json_bytes({"ClientReferenceId": args.client_reference_id}).decode("utf-8")])
    if args.region_id:
        command.extend(["--biz-region-id", args.region_id])
    for attachment in attachments:
        values = []
        for field in ("Type", "MimeType", "Name", "OssObjectKey"):
            if field in attachment:
                values.append("{}={}".format(field, attachment[field]))
        command.extend(["--attachments", *values])
    return command


def build_session_list_command(args: argparse.Namespace, next_token: Optional[str] = None) -> List[str]:
    # V2 routing and pagination must use the ROS plugin's published CLI flags.
    # Putting them only in --body can silently return the legacy V1 list.
    command = [
        resolve_aliyun(args.aliyun_path), "ros", "list-chat-sessions",
        "--endpoint", args.endpoint,
        "--connect-timeout", str(min(int(args.connect_timeout), 10)),
        "--read-timeout", str(min(int(args.read_timeout), 30)),
        "--user-agent", USER_AGENT,
        "--agent-version", "V2",
        "--biz-mode", "IaCCodePipeline" if args.mode == "pipeline" else "IaCCodeNormal",
        "--max-results", "20",
        "--body", _json_bytes({"SessionScopeOption": "CurrentCaller"}).decode("utf-8"),
    ]
    if next_token:
        command.extend(["--next-token", next_token])
    if args.region_id:
        command.extend(["--region", args.region_id])
    return command


def parse_first_cursor(value: Any) -> str:
    # A server-provided replay origin is not a business event ID. Keep .0
    # separate from parse_stream_event_id's strictly positive event sequences.
    if not isinstance(value, str) or len(value) > 4096:
        raise BridgeError("invalid_first_cursor", "ROS returned an invalid FirstCursor.")
    identity, separator, sequence = value.rpartition(".")
    if not separator or sequence != "0" or not identity.startswith("v1.") or len(identity) <= 3:
        raise BridgeError("invalid_first_cursor", "ROS returned an invalid FirstCursor.")
    return identity


def build_reconnect_command(args: argparse.Namespace, session_id: str, cursor: str) -> List[str]:
    endpoint_kind = _endpoint_kind(args.endpoint or "")
    execution_mode = getattr(args, "aliyun_cli_execution_mode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE)
    if execution_mode == "remote" and endpoint_kind != "aliyun":
        raise BridgeError("invalid_input", "Remote aliyun CLI execution requires a public aliyuncs.com endpoint.")
    if execution_mode == "remote" and args.profile:
        raise BridgeError("invalid_input", "Remote aliyun CLI execution does not accept a local Profile.")
    command = [
        resolve_aliyun(args.aliyun_path),
        "ros",
        "start-chat",
        "--endpoint",
        args.endpoint,
        "--connect-timeout",
        str(args.connect_timeout),
        "--read-timeout",
        str(args.read_timeout),
        "--user-agent",
        USER_AGENT,
    ]
    if endpoint_kind == "loopback":
        command.extend(["--secure", "--skip-secure-verify"])
    if args.profile:
        command.extend(["--profile", args.profile])
    if args.region_id:
        command.extend(["--region", args.region_id])
    command.extend(
        [
            "--agent-version",
            "V2",
            "--session-id",
            session_id,
            "--body",
            _json_bytes(
                {
                    "StreamOptions.Action": "Reconnect",
                    "StreamOptions.Cursor": cursor,
                }
            ).decode("utf-8"),
        ]
    )
    return command


def build_stop_command(job: Dict[str, Any], session_id: str) -> List[str]:
    endpoint = str(job.get("endpoint") or "")
    endpoint_kind = _endpoint_kind(endpoint)
    execution_mode = job.get("aliyunCLIExecutionMode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE)
    if execution_mode == "remote" and endpoint_kind != "aliyun":
        raise BridgeError("invalid_input", "Remote aliyun CLI execution requires a public aliyuncs.com endpoint.")
    if execution_mode == "remote" and job.get("profile"):
        raise BridgeError("invalid_input", "Remote aliyun CLI execution does not accept a local Profile.")
    command = [
        resolve_aliyun(str(job.get("aliyunPath") or "aliyun")),
        "ros",
        "stop-chat",
        "--endpoint",
        endpoint,
        "--connect-timeout",
        str(max(1, min(int(job.get("connectTimeout") or 10), 30))),
        "--read-timeout",
        "45",
        "--user-agent",
        USER_AGENT,
    ]
    if endpoint_kind == "loopback":
        command.extend(["--secure", "--skip-secure-verify"])
    profile = job.get("profile")
    if isinstance(profile, str) and profile:
        command.extend(["--profile", profile])
    region_id = job.get("regionId")
    if isinstance(region_id, str) and region_id:
        command.extend(["--region", region_id])
    command.extend(["--agent-version", "V2", "--session-id", session_id])
    return command


def _load_code_sdk() -> Dict[str, Any]:
    try:
        return {
            "CredentialClient": getattr(importlib.import_module("alibabacloud_credentials.client"), "Client"),
            "CLIProfileCredentialsProvider": getattr(
                importlib.import_module("alibabacloud_credentials.provider.cli_profile"),
                "CLIProfileCredentialsProvider",
            ),
            "DaraRequest": getattr(importlib.import_module("darabonba.request"), "DaraRequest"),
            "OpenApiUtils": getattr(importlib.import_module("alibabacloud_tea_openapi.utils"), "Utils"),
            "requests": importlib.import_module("requests"),
        }
    except (ImportError, AttributeError) as exc:
        raise BridgeError(
            "sdk_not_installed",
            "The configured code transport requires the packages listed in {} for the Python interpreter running "
            "this bridge. Do not switch transports; install them and run check again.".format(REQUIREMENTS_FILE),
        ) from exc


def _first_nonempty_env(names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _environment_region() -> Optional[str]:
    region_id = _first_nonempty_env(REGION_ENV_NAMES)
    if region_id and re.fullmatch(r"[A-Za-z0-9-]+", region_id):
        return region_id
    return None


def _cli_config_path() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~/.aliyun/config.json")).resolve()


def _read_cli_configuration() -> Dict[str, Any]:
    path = _cli_config_path()
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CLI_CONFIG_BYTES + 1)
    except OSError as exc:
        raise BridgeError("credential_failed", "The Alibaba Cloud CLI configuration is unavailable.") from exc
    if len(raw) > MAX_CLI_CONFIG_BYTES:
        raise BridgeError("credential_failed", "The Alibaba Cloud CLI configuration file is too large.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BridgeError("credential_failed", "The Alibaba Cloud CLI configuration file is invalid.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), list):
        raise BridgeError("credential_failed", "The Alibaba Cloud CLI configuration file is invalid.")
    return value


def _ros_plugin_supports_reconnect(version: Any) -> bool:
    if not isinstance(version, str):
        return False
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version)
    if match is None:
        return False
    return tuple(int(part) for part in match.groups()) >= MIN_ROS_PLUGIN_VERSION


def _local_ros_plugin_status() -> Dict[str, Any]:
    configured_root = os.environ.get("ALIBABA_CLOUD_CLI_PLUGINS_DIR")
    root = (
        pathlib.Path(os.path.expanduser(configured_root))
        if configured_root
        else pathlib.Path.home() / ".aliyun" / "plugins"
    )
    manifest_path = root / "manifest.json"
    try:
        with manifest_path.open("rb") as handle:
            raw = handle.read(MAX_PLUGIN_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return {"installed": False, "ready": False}
    except OSError as exc:
        raise BridgeError("cli_check_failed", "The Alibaba Cloud CLI plugin manifest could not be read.") from exc
    if len(raw) > MAX_PLUGIN_MANIFEST_BYTES:
        raise BridgeError("cli_check_failed", "The Alibaba Cloud CLI plugin manifest is too large.")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BridgeError("cli_check_failed", "The Alibaba Cloud CLI plugin manifest is invalid.") from exc
    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    plugin = plugins.get("aliyun-cli-ros") if isinstance(plugins, dict) else None
    if not isinstance(plugin, dict):
        return {"installed": False, "ready": False}

    commands = plugin.get("cmdNames")
    command_names = {value for value in commands if isinstance(value, str)} if isinstance(commands, list) else set()
    raw_path = plugin.get("path")
    executable_exists = False
    if isinstance(raw_path, str) and raw_path:
        plugin_root = pathlib.Path(os.path.expanduser(raw_path))
        candidates = (plugin_root / "aliyun-cli-ros", plugin_root / "aliyun-cli-ros.exe")
        executable_exists = any(
            candidate.is_file() and (os.name == "nt" or os.access(str(candidate), os.X_OK)) for candidate in candidates
        )
    result: Dict[str, Any] = {
        "installed": True,
        "ready": executable_exists and ROS_PLUGIN_COMMANDS.issubset(command_names),
    }
    version = plugin.get("version")
    if isinstance(version, str) and version:
        result["version"] = sanitize_text(version, 80)
    result["reconnectReady"] = result["ready"] and _ros_plugin_supports_reconnect(version)
    return result


def _selected_cli_profile_record(profile: Optional[str]) -> Dict[str, Any]:
    value = _read_cli_configuration()
    profile_name = profile or _first_nonempty_env(PROFILE_ENV_NAMES) or value.get("current")
    if not isinstance(profile_name, str) or not profile_name:
        raise BridgeError("credential_failed", "The selected Alibaba Cloud CLI Profile is not configured.")
    selected = next(
        (item for item in value["profiles"] if isinstance(item, dict) and item.get("name") == profile_name),
        None,
    )
    mode = selected.get("mode") if isinstance(selected, dict) else None
    if not isinstance(mode, str) or not mode:
        raise BridgeError("credential_failed", "The selected Alibaba Cloud CLI Profile is not configured.")
    assert isinstance(selected, dict)
    result: Dict[str, Any] = {"name": profile_name, "mode": mode}
    region_id = selected.get("region_id")
    if isinstance(region_id, str) and re.fullmatch(r"[A-Za-z0-9-]+", region_id):
        result["regionId"] = region_id
    language = selected.get("language")
    if isinstance(language, str) and language:
        result["language"] = sanitize_text(language, 50)
    result["autoPluginInstall"] = bool(selected.get("auto_plugin_install")) or (
        os.environ.get("ALIBABA_CLOUD_CLI_PLUGIN_AUTO_INSTALL") == "true"
    )
    return result


def _resolve_start_identity(args: argparse.Namespace) -> None:
    if (
        args.transport == "aliyun_cli"
        and getattr(args, "aliyun_cli_execution_mode", DEFAULT_ALIYUN_CLI_EXECUTION_MODE) == "remote"
    ):
        args.profile = None
        args.credential_source = "remote"
        return
    profile = None  # type: Optional[Dict[str, Any]]
    if args.transport == "code" and not getattr(args, "profile_pinned", False):
        args.profile = None
        args.credential_source = None
    else:
        profile = _selected_cli_profile_record(args.profile)
        args.profile = profile["name"]
        args.credential_source = "profile"

    if not args.region_id:
        args.region_id = _environment_region()
    if not args.region_id and profile is not None:
        args.region_id = profile.get("regionId")
    if not args.region_id:
        args.region_id = "cn-hangzhou"


def _code_credentials(
    sdk: Dict[str, Any],
    aliyun_path: str,
    profile: Optional[str],
    region_id: Optional[str],
    credential_source: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    if credential_source not in {None, "profile"}:
        raise BridgeError("credential_failed", "The managed Alibaba Cloud credential source is invalid.")
    if credential_source == "profile":
        selected = _selected_cli_profile_record(profile)
        provider = sdk["CLIProfileCredentialsProvider"](profile_name=selected["name"])
        client = sdk["CredentialClient"](provider=provider)
    else:
        client = sdk["CredentialClient"]()
    credential = client.get_credential()
    access_key_id = credential.access_key_id
    access_key_secret = credential.access_key_secret
    security_token = credential.security_token
    if not access_key_id or not access_key_secret:
        raise ValueError("empty credentials")
    return access_key_id, access_key_secret, security_token or None


def _canonical_query_string(parameters: Dict[str, str]) -> str:
    return "&".join(
        "{}={}".format(name, urllib.parse.quote(value, safe="~", encoding="utf-8"))
        for name, value in sorted(parameters.items())
    )


def _build_v3_request(
    sdk: Dict[str, Any],
    operation: str,
    parameters: Dict[str, str],
    endpoint: str,
    credentials: Tuple[str, str, Optional[str]],
) -> Tuple[str, Dict[str, str]]:
    access_key_id, access_key_secret, security_token = credentials
    signature_algorithm = "ACS3-HMAC-SHA256"
    utils = sdk["OpenApiUtils"]
    payload_hash = utils.hash(b"", signature_algorithm).hex()
    headers = {
        "accept": "text/event-stream" if operation == "StartChat" else "application/json",
        "accept-encoding": "identity",
        "host": endpoint,
        "user-agent": USER_AGENT,
        "x-acs-action": operation,
        "x-acs-content-sha256": payload_hash,
        "x-acs-date": utils.get_timestamp(),
        "x-acs-signature-nonce": utils.get_nonce(),
        "x-acs-version": "2019-09-10",
    }
    if security_token:
        headers["x-acs-accesskey-id"] = access_key_id
        headers["x-acs-security-token"] = security_token

    request = sdk["DaraRequest"]()
    request.protocol = "https"
    request.method = "POST"
    request.pathname = "/"
    request.query = dict(parameters)
    request.headers = headers
    headers["Authorization"] = utils.get_authorization(
        request,
        signature_algorithm,
        payload_hash,
        access_key_id,
        access_key_secret,
    )
    query = _canonical_query_string(parameters)
    return "https://{}/{}".format(endpoint, "?{}".format(query) if query else ""), headers


class _CodeHttpResponse:
    def __init__(self, response: Any, session: Any):
        self._response = response
        self._session = session
        self.headers = response.headers

    def __iter__(self) -> Iterator[bytes]:
        # A connection-close SSE response can otherwise buffer complete events
        # until the requested chunk fills or the stream ends.
        for line in self._response.iter_lines(chunk_size=1, decode_unicode=False):
            yield line + b"\n"

    def read(self, maximum: int) -> bytes:
        return self._response.raw.read(maximum, decode_content=True)

    def close(self) -> None:
        self._response.close()
        self._session.close()


def _open_code_request(
    operation: str,
    parameters: Dict[str, str],
    endpoint: str,
    profile: Optional[str],
    region_id: Optional[str],
    aliyun_path: str,
    connect_timeout: int,
    read_timeout: int,
    credential_source: Optional[str] = None,
    error_code: str = "start_chat_failed",
) -> Any:
    sdk = _load_code_sdk()
    try:
        credentials = _code_credentials(sdk, aliyun_path, profile, region_id, credential_source)
        url, headers = _build_v3_request(sdk, operation, parameters, endpoint, credentials)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(
            "credential_failed",
            "Alibaba Cloud SDK could not load or refresh the selected CLI Profile.",
            True,
        ) from exc

    session = sdk["requests"].Session()
    try:
        response = session.request(
            method="POST",
            url=url,
            data=None,
            headers=headers,
            timeout=(connect_timeout, read_timeout),
            allow_redirects=False,
            verify=_endpoint_kind(endpoint) != "loopback",
            stream=True,
        )
    except Exception as exc:
        session.close()
        raise BridgeError(error_code, "Alibaba Cloud ROS {} could not be reached.".format(operation), True) from exc

    wrapped = _CodeHttpResponse(response, session)
    if 400 <= response.status_code < 600:
        raw = wrapped.read(MAX_DIAGNOSTIC_BYTES + 1)
        wrapped.close()
        message = "Alibaba Cloud ROS rejected the request."
        if len(raw) <= MAX_DIAGNOSTIC_BYTES:
            with contextlib.suppress(UnicodeError, ValueError):
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict):
                    backup_not_ready = _session_backup_not_ready_error(value)
                    if backup_not_ready is not None:
                        raise backup_not_ready
                    code = value.get("Code", value.get("code"))
                    detail = value.get("Message", value.get("message"))
                    if isinstance(code, str) or isinstance(detail, str):
                        message = "{}: {}".format(code or "{}Failed".format(operation), detail or "Request failed")
        retryable = response.status_code == 429 or response.status_code >= 500
        raise BridgeError(error_code, sanitize_text(message, 2000), retryable)
    return wrapped


def _response_text_lines(response: Any) -> Iterator[str]:
    for raw_line in response:
        if len(raw_line) > MAX_SSE_LINE_BYTES:
            raise BridgeError("stream_failed", "A StartChat SSE line exceeded the bridge limit.")
        yield raw_line.decode("utf-8", "replace")


def parse_stream_event_id(value: Any) -> Tuple[str, int]:
    if not isinstance(value, str) or not value:
        raise BridgeError("invalid_stream_event_id", "StartChat returned an invalid SSE event ID.")
    identity, separator, raw_sequence = value.rpartition(".")
    if (
        not separator
        or not identity.startswith("v1.")
        or len(identity) <= 3
        or not raw_sequence.isdigit()
    ):
        raise BridgeError("invalid_stream_event_id", "StartChat returned an invalid SSE event ID.")
    try:
        sequence = int(raw_sequence)
    except ValueError as exc:
        raise BridgeError("invalid_stream_event_id", "StartChat returned an invalid SSE event ID.") from exc
    if sequence <= 0:
        raise BridgeError("invalid_stream_event_id", "StartChat returned an invalid SSE event ID.")
    return identity, sequence


def iter_sse_payloads(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    data_lines = []  # type: List[str]
    raw_lines = []  # type: List[str]
    event_id = None  # type: Optional[str]
    event_bytes = 0

    def decode(data: List[str], raw: List[str], current_event_id: Optional[str]) -> Dict[str, Any]:
        payload_text = "\n".join(data).strip() if data else "\n".join(raw).strip()
        if len(payload_text.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
            raise BridgeError("stream_event_too_large", "A StartChat SSE event exceeded the bridge limit.")
        try:
            value = json.loads(payload_text)
        except ValueError:
            value = None
        return {"id": current_event_id, "payload": value if isinstance(value, dict) else None, "raw": payload_text}

    for raw_line in lines:
        event_bytes += len(raw_line.encode("utf-8"))
        if event_bytes > MAX_SSE_EVENT_BYTES:
            raise BridgeError("stream_failed", "A StartChat SSE event exceeded the bridge limit.")
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines or raw_lines:
                yield decode(data_lines, raw_lines, event_id)
                data_lines = []
                raw_lines = []
                event_id = None
            event_bytes = 0
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("id:"):
            event_id = line[3:].lstrip()
        elif not data_lines:
            raw_lines.append(line)
    if data_lines or raw_lines:
        yield decode(data_lines, raw_lines, event_id)


_DEBUG_SECRET_KEY = re.compile(
    r"(?i)(access[-_ ]?key|secret|token|password|passwd|passphrase|authorization|cookie|credential|"
    r"private[-_ ]?key|signature|starops[-_ ]?hil|prompt|query|client[-_ ]?context|history)"
)
_DEBUG_TEXT_SECRET = re.compile(
    r"(?i)((?:access[-_ ]?key(?:[-_ ]?id|[-_ ]?secret)?|security[-_ ]?token|token|password|passwd|passphrase|"
    r"authorization|cookie|credential|private[-_ ]?key|signature|starops[-_ ]?hil|"
    r"prompt|query|client[-_ ]?context|history|text)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,;&}\]]+)"
)


class _RemoteCLIDebugLog(object):
    """Best-effort, bounded and redacted JSONL diagnostics for the remote CLI boundary."""

    MAX_FILE_BYTES = 4 * 1024 * 1024
    MAX_RAW_LINE_BYTES = 64 * 1024
    MAX_STDOUT_BYTES = 256 * 1024
    MAX_TEXT_BYTES = 12 * 1024

    def __init__(self, workspace, invocation_id, reconnecting, worker_role, args, *, record_start=True):
        self.path = workspace / ".ros-agent-debug" / "remote-aliyun-results.jsonl"
        self.context = {
            "schemaVersion": 1,
            "invocationId": invocation_id,
            "jobId": getattr(args, "debug_job_id", None),
            "requestSeq": getattr(args, "debug_request_seq", None),
            "workerRole": worker_role,
            "pid": os.getpid(),
        }
        self.stdout_bytes = 0
        self.stdout_limited = False
        self.file_limited = False
        self.warned = False
        self.pending_records = None
        self.pending_bytes = 0
        self.elapsed = {name: 0.0 for name in ("outputRead", "parse", "eventProcess", "projection", "parsedEventLog")}
        self.complete_event_count = 0
        self.committed_event_count = 0
        self.replayed_event_count = 0
        self.secret_values = []
        for name, value in os.environ.items():
            if len(value) >= 8 and _DEBUG_SECRET_KEY.search(name):
                for variant in (
                    value,
                    json.dumps(value, ensure_ascii=False)[1:-1],
                    json.dumps(value, ensure_ascii=True)[1:-1],
                    urllib.parse.quote(value, safe=""),
                ):
                    if variant not in self.secret_values:
                        self.secret_values.append(variant)
        self.secret_values.sort(key=len, reverse=True)
        if record_start:
            self.record("start", operation="start-chat", reconnect=reconnecting, hasQuery=not reconnecting)

    def _text(self, value):
        for secret in self.secret_values:
            value = value.replace(secret, "[REDACTED]")
        value = SECRET_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", value)
        value = _DEBUG_TEXT_SECRET.sub(lambda match: match.group(1) + '"[REDACTED]"', value)
        value = re.sub(r"\bLTAI[A-Za-z0-9]{12,}\b", "[REDACTED]", value)
        return value

    def _value(self, value, depth=0):
        if depth > 16:
            return "[DEPTH-LIMIT]"
        if isinstance(value, dict):
            return {
                self._text(str(key)): "[REDACTED]"
                if _DEBUG_SECRET_KEY.search(str(key))
                else self._value(child, depth + 1)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self._value(child, depth + 1) for child in value]
        if isinstance(value, str):
            try:
                nested = json.loads(value)
            except (ValueError, RecursionError):
                nested = None
            if isinstance(nested, (dict, list)):
                return json.dumps(self._value(nested, depth + 1), ensure_ascii=False, separators=(",", ":"))
            return self._text(value)
        return value

    def record(self, kind, **fields):
        if self.file_limited:
            return
        try:
            value = dict(self.context, timeUTC=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind=kind)
            value.update(fields)
            encoded = (json.dumps(self._value(value), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if self.pending_records is not None:
                if self.pending_bytes + len(encoded) > self.MAX_FILE_BYTES - 1024:
                    encoded = self._file_limit_record()
                    self.file_limited = True
                self.pending_records.append(encoded)
                self.pending_bytes += len(encoded)
            else:
                self._append_records([encoded])
        except Exception:
            self._warn()

    def _file_limit_record(self):
        return (json.dumps(dict(self.context, kind="file-limit", maxBytes=self.MAX_FILE_BYTES)) + "\n").encode("utf-8")

    def _warn(self):
        if not self.warned:
            print("ROS Agent debug log unavailable; executor handling is unchanged.", file=sys.stderr)
            self.warned = True

    def _append_records(self, records):
        if not records:
            return
        try:
            lock_path = self.path.with_suffix(".lock")
            if self.path.parent.is_symlink() or self.path.is_symlink() or lock_path.is_symlink():
                raise OSError("debug log symlinks are not allowed")
            _secure_directory(self.path.parent)
            # Separate CLI owners and the parser share this file; append may need multiple writes.
            with StateLock(lock_path):
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(str(self.path), flags, 0o600)
                try:
                    if os.name != "nt":
                        os.fchmod(descriptor, 0o600)
                    size = os.fstat(descriptor).st_size
                    output = bytearray()
                    for encoded in records:
                        if size + len(output) + len(encoded) > self.MAX_FILE_BYTES - 1024:
                            encoded = self._file_limit_record()
                            self.file_limited = True
                            if size + len(output) + len(encoded) <= self.MAX_FILE_BYTES:
                                output.extend(encoded)
                            break
                        output.extend(encoded)
                    remaining = memoryview(output)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("debug log write made no progress")
                        remaining = remaining[written:]
                finally:
                    os.close(descriptor)
        except Exception:
            self._warn()

    @contextlib.contextmanager
    def batch(self):
        """Keep redacted records bounded in memory and append them in one batch."""
        self.pending_records = []
        self.pending_bytes = 0
        try:
            yield
        finally:
            records = self.pending_records
            self.pending_records = None
            self.pending_bytes = 0
            self._append_records(records)

    def output(self, kind, text):
        size = len(text.encode("utf-8"))
        if size > self.MAX_RAW_LINE_BYTES:
            self.record(kind, bytes=size, textOmitted="line-size-limit")
            return
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            redacted = json.dumps(self._value(parsed), ensure_ascii=False, separators=(",", ":"))
            format_name = "json"
        else:
            redacted = self._text(text)
            format_name = "text"
        preview = _truncate_utf8(redacted, self.MAX_TEXT_BYTES)
        self.record(
            kind,
            bytes=size,
            format=format_name,
            text=preview,
            truncated=len(redacted.encode("utf-8")) > self.MAX_TEXT_BYTES,
        )

    def lines(self, lines):
        for line in lines:
            self.stdout_bytes += len(line.encode("utf-8"))
            if self.stdout_bytes <= self.MAX_STDOUT_BYTES:
                with contextlib.suppress(Exception):
                    self.output("stdout", line)
            elif not self.stdout_limited:
                self.record("stdout-limit", maxBytes=self.MAX_STDOUT_BYTES)
                self.stdout_limited = True
            yield line

    def parsed_event(self, event):
        payload = event.get("payload")
        raw = event.get("raw")
        envelope = None
        if isinstance(raw, str) and len(raw.encode("utf-8")) <= self.MAX_RAW_LINE_BYTES:
            with contextlib.suppress(ValueError, RecursionError):
                envelope = json.loads(raw)
        self.record(
            "parsed-event",
            id=event.get("id"),
            idPresent=event.get("id") is not None,
            payloadKeys=sorted(str(key) for key in payload)[:32] if isinstance(payload, dict) else [],
            envelopeKeys=sorted(str(key) for key in envelope)[:32] if isinstance(envelope, dict) else [],
            bootstrapPresent=isinstance(event.get("bootstrap"), dict),
            cliFailure=event.get("cliFailure") is True,
        )

    def parse_events(self, lines):
        """Time parser work separately from waiting for its next input line."""
        source = iter(lines)

        def measured_lines():
            while True:
                started = time.monotonic()
                try:
                    line = next(source)
                except StopIteration:
                    return
                finally:
                    self.elapsed["outputRead"] += time.monotonic() - started
                yield line

        events = iter_cli_plugin_payloads(measured_lines())
        while True:
            read_before = self.elapsed["outputRead"]
            started = time.monotonic()
            try:
                event = next(events)
            except StopIteration:
                return
            finally:
                self.elapsed["parse"] += max(
                    0.0, time.monotonic() - started - (self.elapsed["outputRead"] - read_before)
                )
            payload = event.get("payload")
            if (isinstance(event.get("id"), str) and isinstance(payload, dict)
                    and str(payload.get("object", "")).lower() not in {"heartbeat", "keepalive"}):
                self.complete_event_count += 1
            yield event

    def metrics(self):
        return dict(
            {name + "ElapsedMs": round(seconds * 1000) for name, seconds in self.elapsed.items()},
            completeEventCount=self.complete_event_count,
            committedEventCount=self.committed_event_count,
            replayedEventCount=self.replayed_event_count,
        )


def _cli_plugin_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _is_cli_stream_interruption(value: Any) -> bool:
    if isinstance(value, dict):
        # Only inspect transport envelopes, never text inside a business event.
        if any(key in value for key in ("id", "data", "event", "result", "parts", "object")):
            return False
        code = value.get("code", value.get("Code"))
        status = value.get("statusCode", value.get("httpStatus"))
        if code is not None or (isinstance(status, int) and 400 <= status < 500):
            return False  # Structured auth, expiry and cursor errors stay authoritative.
        if (value.get("summary") == "Failed to call tool" and isinstance(value.get("detail"), str)
                and re.fullmatch(r"Async task polling timeout after [0-9]+ seconds", value["detail"])):
            return True  # MCP stopped polling; this does not prove the cloud task failed.
        return any(_is_cli_stream_interruption(value.get(key)) for key in ("detail", "message", "Message", "error"))
    if not isinstance(value, str):
        return False
    return re.fullmatch(
        r"\s*(?:(?:error|aliyun cli call failed|failed to call tool):\s*)*SSE connection lost[.!]?\s*",
        value,
        re.IGNORECASE,
    ) is not None


def is_remote_cli_stream_interruption(*diagnostics: str) -> bool:
    candidates = []
    for diagnostic in diagnostics:
        diagnostic = diagnostic.strip()
        if diagnostic.startswith("MCP Returned Error Result "):
            diagnostic = diagnostic[len("MCP Returned Error Result "):]
        try:
            value = json.loads(diagnostic)
        except ValueError:
            value = diagnostic
        # A precise structured error must not be overridden by a generic
        # disconnect message elsewhere in the same invocation's diagnostics.
        nested = value
        while isinstance(nested, dict) and not any(key in nested for key in ("id", "data", "event", "result")):
            status = nested.get("statusCode", nested.get("httpStatus"))
            if (nested.get("code", nested.get("Code")) is not None
                    or (isinstance(status, int) and 400 <= status < 500)):
                return False
            nested = nested.get("error")
        candidates.append(value)
    return any(_is_cli_stream_interruption(value) for value in candidates)


def _is_cli_failure_envelope(value: Any) -> bool:
    if not isinstance(value, dict) or any(key in value for key in ("id", "data", "event")):
        return False
    # Host-provided remote executors can return an MCP error instead of ROS events.
    if value.get("summary") == "Failed to call tool" and isinstance(value.get("detail"), str):
        return True
    if _is_cli_stream_interruption(value):
        return True
    candidates = [value]
    nested = value.get("error")
    if isinstance(nested, dict):
        candidates.append(nested)
    return any(
        isinstance(candidate.get("statusCode", candidate.get("httpStatus")), int)
        or isinstance(candidate.get("code", candidate.get("Code")), str)
        for candidate in candidates
    )


def iter_cli_plugin_payloads(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    buffer = ""
    raw_bytes = 0
    decoded_bytes = 0
    array_state = None  # type: Optional[str]

    def projected(value: Any, raw: str) -> Iterator[Dict[str, Any]]:
        nonlocal decoded_bytes
        if isinstance(value, list):
            for item in value:
                item_raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                yield from projected(item, item_raw)
            return
        if not isinstance(value, dict):
            yield {"id": None, "payload": None, "raw": raw}
            return
        if _is_cli_failure_envelope(value):
            yield {"id": None, "payload": None, "raw": raw, "cliFailure": True}
            return
        bootstrap = value if isinstance(value.get("event"), dict) and "invocationId" in value else None
        envelope = value.get("event") if isinstance(bootstrap, dict) else value
        assert isinstance(envelope, dict)
        payload = _cli_plugin_payload(envelope)
        payload_bytes = len(_json_bytes(payload))
        if payload_bytes > MAX_SSE_EVENT_BYTES:
            raise BridgeError("stream_event_too_large", "A StartChat CLI event exceeded the bridge limit.")
        if bootstrap is None:
            decoded_bytes += payload_bytes
            if decoded_bytes > MAX_SERVER_REPLAY_BYTES:
                raise BridgeError("server_replay_too_large", "StartChat replay exceeded the server replay limit.")
        yield {
            "id": envelope.get("id"),
            "payload": payload,
            "raw": raw,
            "bootstrap": bootstrap,
        }

    for raw_line in lines:
        line_bytes = len(raw_line.encode("utf-8"))
        if line_bytes > MAX_CLI_BATCH_BYTES:
            raise BridgeError("cli_output_too_large", "A StartChat CLI output line exceeded the batch limit.")
        raw_bytes += line_bytes
        if raw_bytes > MAX_CLI_BATCH_BYTES:
            raise BridgeError("cli_output_too_large", "StartChat CLI output exceeded the batch limit.")
        buffer += raw_line
        if len(buffer.encode("utf-8")) > MAX_CLI_BATCH_BYTES:
            raise BridgeError("cli_output_too_large", "Buffered StartChat CLI output exceeded the batch limit.")
        while buffer.strip():
            leading = len(buffer) - len(buffer.lstrip())
            first = buffer[leading]
            if array_state is None and first == "[":
                # Commit complete array members without waiting for the closing
                # bracket: remote executors can truncate a buffered CLI result.
                buffer = buffer[leading + 1:]
                array_state = "first"
                continue
            if array_state == "separator":
                if first == ",":
                    buffer = buffer[leading + 1:]
                    array_state = "item"
                    continue
                if first != "]":
                    raise BridgeError("invalid_cli_output", "StartChat CLI returned an invalid JSON array separator.")
            if array_state is not None and first == "]":
                if array_state == "item":
                    raise BridgeError(
                        "invalid_cli_output", "StartChat CLI returned a JSON array with a trailing comma."
                    )
                buffer = buffer[leading + 1:]
                array_state = None
                continue
            try:
                value, end = decoder.raw_decode(buffer, leading)
            except ValueError:
                if array_state is not None:
                    break
                first_line, separator, remainder = buffer.partition("\n")
                if not separator or not first_line.strip() or not remainder.strip():
                    break
                remainder_start = len(remainder) - len(remainder.lstrip())
                try:
                    _remainder_value, remainder_end = decoder.raw_decode(remainder, remainder_start)
                except ValueError:
                    break
                if remainder[remainder_end:].strip():
                    break
                yield {"id": None, "payload": None, "raw": first_line.strip()}
                buffer = remainder
                continue
            if array_state is not None:
                # A numeric token can still grow in the next chunk (e.g. 1e2).
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    remainder = buffer[end:].lstrip()
                    if not remainder or remainder[0] not in ",]":
                        break
                array_state = "separator"
            raw = buffer[leading:end]
            buffer = buffer[end:]
            yield from projected(value, raw)
    if array_state is not None:
        raise BridgeError(
            "cli_output_truncated",
            "StartChat CLI output ended inside a JSON array; only complete events were committed.",
            True,
        )
    if buffer.strip():
        yield {"id": None, "payload": None, "raw": buffer.strip()}


def is_retryable_cli_failure(return_code: int, *diagnostics: str) -> bool:
    if return_code == 0:
        return False
    stable_timeout_codes = {
        "executortimeout",
        "readtimeout",
        "connectiontimeout",
        "connectionreset",
    }

    def values(item: Any) -> Iterator[Tuple[Optional[int], Optional[str]]]:
        if isinstance(item, dict):
            raw_status = item.get("statusCode", item.get("httpStatus"))
            status = raw_status if isinstance(raw_status, int) else None
            raw_code = item.get("code", item.get("Code"))
            code = raw_code.lower() if isinstance(raw_code, str) else None
            yield status, code
            for nested in item.values():
                yield from values(nested)
        elif isinstance(item, list):
            for nested in item:
                yield from values(nested)

    for text in diagnostics:
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        for status, code in values(value):
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                return True
            if code in stable_timeout_codes:
                return True
    return False


def _read_session_list_page(args, workspace, next_token, worker_role, can_reconnect):
    command = build_session_list_command(args, next_token)
    process = None
    with tempfile.TemporaryFile(mode="w+b") as stderr_file:
        try:
            if getattr(args, "managed_job_id", None):
                process = _ForegroundCLIProcess(
                    args.managed_job_id, command, {REMOTE_BOOTSTRAP_INVOCATION_ENV: uuid.uuid4().hex},
                    stderr_file, worker_role, getattr(args, "debug", False), MAX_SESSION_LIST_BYTES,
                )
            else:
                process = subprocess.Popen(
                    command, cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=stderr_file, text=True, encoding="utf-8", errors="replace",
                )
            output = bytearray()
            for line in process.stdout:
                if can_reconnect is not None and can_reconnect() is False:
                    raise BridgeError("session_lookup_stopped", "The session lookup was stopped.")
                output.extend(line.encode("utf-8"))
                if len(output) > MAX_SESSION_LIST_BYTES:
                    raise BridgeError("session_lookup_failed", "The session list exceeded its response limit.")
            if process.wait() != 0:
                raise BridgeError("session_lookup_failed", "ROS ListChatSessions failed; the cloud outcome is unknown.")
            page = json.loads(output.decode("utf-8"))
            for _ in range(4):
                if isinstance(page, dict) and isinstance(page.get("Sessions"), list):
                    return page
                if not isinstance(page, dict):
                    break
                page = page.get("body", page.get("data", page.get("Data")))
                if isinstance(page, str):
                    page = json.loads(page)
            raise BridgeError("session_lookup_failed", "ROS did not return a complete session list.")
        except (OSError, ValueError) as exc:
            raise BridgeError("session_lookup_failed", "ROS did not return a complete session list.") from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    _stop_process(process)
                process.stdout.close()


def _lookup_first_cursor(args, workspace, session_id, worker_role, can_reconnect):
    reference = getattr(args, "client_reference_id", None)
    if not session_id and not reference:
        return None
    resolved_session_id = None
    for scan in range(MAX_SESSION_LOOKUP_SCANS):
        token = None
        seen_tokens = set()
        matches = {}  # type: Dict[str, Dict[str, Any]]
        for _ in range(MAX_SESSION_LOOKUP_PAGES):
            if can_reconnect is not None and can_reconnect() is False:
                return None
            page = _read_session_list_page(args, workspace, token, worker_role, can_reconnect)
            for item in page["Sessions"]:
                if not isinstance(item, dict):
                    raise BridgeError("session_lookup_failed", "ROS returned an invalid session list item.")
                candidate = item.get("SessionId")
                if (candidate == session_id if session_id else item.get("ClientReferenceId") == reference):
                    if not isinstance(candidate, str) or not candidate:
                        raise BridgeError("session_lookup_failed", "ROS returned an invalid SessionId.")
                    matches[candidate] = item
            if len(matches) > 1:
                raise BridgeError("ambiguous_session_reference", "Multiple ROS sessions match the ClientReferenceId.")
            token = page.get("NextToken")
            if token is not None and not isinstance(token, str):
                raise BridgeError("session_lookup_failed", "ROS returned an invalid session page token.")
            # SessionId is already the server's unique identity. Reference-based
            # lookup must finish pagination before treating a match as unique.
            if not token or (session_id and matches):
                break
            if token in seen_tokens:
                raise BridgeError("session_lookup_failed", "ROS session pagination made no progress.")
            seen_tokens.add(token)
        else:
            raise BridgeError("session_lookup_failed", "ROS session pagination exceeded its lookup budget.")
        if matches:
            candidate, item = next(iter(matches.items()))
            resolved_session_id = candidate
            cursor = item.get("FirstCursor")
            if cursor not in (None, ""):
                parse_first_cursor(cursor)
                return candidate, cursor
        if scan + 1 < MAX_SESSION_LOOKUP_SCANS:
            time.sleep(0.2 * (2 ** scan))
    return (resolved_session_id, None) if resolved_session_id else None
