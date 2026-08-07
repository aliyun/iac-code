# iac-code Desktop Privacy Notice

Effective date: {{EFFECTIVE_DATE}}

Data controller/operator: {{LEGAL_ENTITY}}

Privacy contact: {{PRIVACY_CONTACT}}

This notice describes the Desktop-specific data handling added by the installed iac-code application. The Desktop app
reuses the same iac-code business runtime, provider integrations, permissions, and project data formats as the Web and
CLI versions. It does not introduce a hosted relay for local WebView traffic.

## Data stored on the device

The app stores settings, provider and cloud credential references, sessions, project memory, logs, task state, and
download/install recovery records under the configured iac-code data directory. The default business configuration
directory is `~/.iac-code/`; `IAC_CODE_CONFIG_DIR` can select another directory. Desktop Host state, including the most
recent project, preferred loopback port, window/update state, and generation counters, is stored in the operating
system's application-local data directory.

The local Desktop UI talks to a sidecar bound to `127.0.0.1`. Project files and generated IaC artifacts remain on the
device unless a feature selected by the user sends their content to a configured model, cloud, MCP, OAuth, or other
external service.

Local application data remains until the user deletes it, uses an applicable in-product removal action, or removes it
according to the operating system and enterprise retention policy. Uninstalling the application may leave user data in
the configuration or application-data directory so that a later installation can reuse it.

## External services

When the user configures and invokes an LLM provider, Alibaba Cloud service, MCP server, OAuth provider, updater
endpoint, or a link opened in the system browser, the app sends the data required for that request directly to that
service. Those services process data under their own terms and privacy notices. Users and administrators are
responsible for selecting appropriate endpoints and credentials.

Automatic update checks send the installed version, target platform, architecture, and updater protocol request data
to the configured iac-code Desktop update endpoint. Update artifacts are verified before installation.

## Telemetry and diagnostics

Desktop uses the existing iac-code telemetry lifecycle. By default, message/tool content capture for OpenTelemetry is
disabled unless explicitly enabled; debug mode or an explicit content-capture setting can include additional diagnostic
content. `DISABLE_TELEMETRY=1` disables telemetry. `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` selects the more
restrictive essential-traffic mode. Local development builds do not export telemetry unless explicitly opted in to a
loopback telemetry endpoint.

Telemetry retained by {{LEGAL_ENTITY}} is kept for {{TELEMETRY_RETENTION}}. Local logs and diagnostic captures can
contain paths, provider/model identifiers, errors, and user-requested debug content. Users should review diagnostics
before sharing them and should not send credential files or secret values.

## Credentials

Depending on platform and configuration, credentials can be stored in the operating-system credential store or in the
iac-code configuration directory. The Desktop reveal action requires a native confirmation before displaying a secret.
The app does not provide an application-level copy button for revealed secrets. Users remain responsible for device,
account, backup, and configuration-directory access controls.

## User choices and requests

Users can stop using external integrations, remove their credentials and local project/session data, disable telemetry,
and uninstall the Desktop app. Requests about data controlled by {{LEGAL_ENTITY}} should be sent to
{{PRIVACY_CONTACT}}. Requests concerning a separately configured provider or enterprise service should be directed to
that service's operator.

This notice must be reviewed and approved for every stable release when the legal entity, telemetry endpoint, retention
period, external services, or Desktop data handling changes.
