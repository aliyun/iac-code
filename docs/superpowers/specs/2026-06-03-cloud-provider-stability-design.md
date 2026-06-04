# Cloud Provider Stability Design

Date: 2026-06-03
Branch: `codex/cloud-provider-stability`
Issues: [#77](https://github.com/aliyun/iac-code/issues/77), [#81](https://github.com/aliyun/iac-code/issues/81)

## Purpose

Fix two related groups of cloud provider stability bugs without broad refactoring:

- ROS stack lifecycle reporting must distinguish real deployment success from rollback, cleanup, or merely accepted asynchronous API requests.
- Aliyun credential loading and OAuth flows must tolerate malformed local CLI config and close HTTP clients they create.

The implementation will keep the existing `src/iac_code/tools/cloud/`, `src/iac_code/services/providers/`, and test module boundaries.

## ROS Stack Status Semantics

`StackStatus.is_success` will represent genuine successful stack operation states only:

- `CREATE_COMPLETE`
- `UPDATE_COMPLETE`
- `IMPORT_CREATE_COMPLETE`
- `IMPORT_UPDATE_COMPLETE`
- `CHECK_COMPLETE`

Statuses containing rollback, such as `CREATE_ROLLBACK_COMPLETE`, `ROLLBACK_COMPLETE`, `IMPORT_CREATE_ROLLBACK_COMPLETE`, and `IMPORT_UPDATE_ROLLBACK_COMPLETE`, will remain terminal but not successful. This prevents `BaseCloudStack.execute()` from returning `ToolResult.success` for deployments that failed and then rolled back.

`DELETE_COMPLETE` needs action-aware handling. It is not a deployment success state, but it is a successful result for an explicit `DeleteStack` action. The stack execution path will distinguish deployment success from action success so a successful deletion is not shown as a failed tool call, while also not being counted as `DEPLOYMENT_SUCCEEDED`.

## ROS Telemetry

`CreateStack` and `UpdateStack` API calls only start asynchronous ROS operations. Their request success must not emit `DEPLOYMENT_SUCCEEDED`.

The design is:

- Keep `DEPLOYMENT_STARTED` in the ROS create/update handlers before the API call.
- Keep API-call failure, cancellation, and timeout telemetry around the initial request.
- Move final success/failure telemetry to the polling terminal state.
- Emit `DEPLOYMENT_SUCCEEDED` only after polling confirms a genuine success status.
- Emit deployment failure telemetry for terminal failure or rollback states.
- Do not emit deployment success telemetry for `DeleteStack`/`DELETE_COMPLETE`.

To avoid a broad cloud abstraction rewrite, `BaseCloudStack.execute()` will call a small overridable hook when a terminal status is reached. `RosStack` will implement the hook using metadata captured when the operation starts, such as IaC kind, region, resource count, resource types, and start time. Other cloud stack subclasses remain unaffected unless they opt in.

## ROS SDK Async Behavior

The Alibaba Cloud ROS SDK methods used by `RosStack` are synchronous. The async tool methods will wrap blocking SDK calls with `asyncio.to_thread()`:

- `create_stack`
- `update_stack`
- `continue_create_stack`
- `delete_stack`
- `get_stack`
- `list_stack_resources`

Request construction and lightweight local preprocessing stay on the event loop. Only the blocking SDK method calls need the thread hop. Cancellation propagates through the existing `asyncio.CancelledError` paths.

## Aliyun CLI Credential Loading

`AliyunCredentials._load_from_aliyun_cli()` will treat malformed CLI config as unavailable rather than fatal:

- If the file is missing, invalid JSON, unreadable, or not a JSON object, return `None`.
- If `profiles` is not a list, treat it as empty.
- Skip profile entries that are not dictionaries.
- Skip profile dictionaries without a non-empty string `name`.
- Load the `default` profile if present; otherwise return `None`.

This preserves the existing source priority chain: environment variables, then iac-code config, then aliyun CLI config.

## OAuth HTTP Client Lifecycle

`AliyunOAuthClient` will track whether it owns the `httpx.Client`:

- If `http_client` is injected, the caller owns it and `AliyunOAuthClient.close()` will not close it.
- If `AliyunOAuthClient` creates the client internally, `close()` will close it.
- `AliyunOAuthClient` will support context manager usage with `__enter__` and `__exit__`.
- `close()` will be idempotent.

Callers that create an OAuth client implicitly must close it:

- `run_browser_oauth_flow()` will close only the client it creates internally.
- `AliyunCredentials.refresh_oauth_if_needed()` will close only the client it creates internally.
- `_aliyun_oauth_login_flow()` creates one explicit client and closes it after both token exchange and STS exchange complete.

Injected fake or externally managed clients in tests remain open.

## Testing

Add focused regression tests without real network calls, real Alibaba Cloud accounts, or real user config:

- `tests/tools/cloud/test_types.py`: success statuses, rollback statuses, and `DELETE_COMPLETE` semantics.
- `tests/tools/cloud/test_base_stack.py`: rollback terminal status returns `ToolResult.error`; explicit delete completion is treated as action success.
- `tests/tools/cloud/aliyun/test_ros_stack.py`: create/update do not emit premature success telemetry; final polling status controls success/failure telemetry; SDK calls are routed through `asyncio.to_thread()`.
- `tests/services/providers/test_aliyun.py`: malformed CLI profiles with missing `name`, non-dict entries, non-list `profiles`, and non-object top-level JSON do not crash.
- `tests/services/providers/test_aliyun_oauth.py`: internally owned HTTP clients are closed, injected clients are not closed, and the browser OAuth flow and refresh path close owned clients.
- `tests/commands/test_auth_flows.py` if needed: the explicit OAuth login flow closes its client after login.

## Verification

Run targeted tests first:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest \
  tests/tools/cloud/test_types.py \
  tests/tools/cloud/test_base_stack.py \
  tests/tools/cloud/aliyun/test_ros_stack.py \
  tests/services/providers/test_aliyun.py \
  tests/services/providers/test_aliyun_oauth.py \
  tests/commands/test_auth_flows.py
```

Run lint after implementation:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH make lint
```

The current full baseline has a known pre-existing failure: `make test` fails four i18n tests because `src/iac_code/i18n/messages.pot` is absent and ignored by `.gitignore`. That baseline issue is outside this design unless the user asks to address it.

## Out Of Scope

- Reworking the full cloud tool telemetry abstraction for all future providers.
- Changing credential source priority.
- Adding real cloud integration tests.
- Committing generated translations or build artifacts.
