# Selling Pipeline Local Console Design

## Purpose

Build a local website under `scripts/` for the Alibaba Cloud selling pipeline. The site should present the full A2A pipeline interaction as a product console, matching the provided Alibaba Cloud console screenshots: top console navigation, a left AI workflow panel, a right "Your purchase plans" area, and a floating utility rail.

The existing `scripts/a2a/debugger.py` remains the protocol reference. The new site reuses its A2A request, SSE, task identity, snapshot, pause, cancel, logging, and replay lessons, but changes the primary experience from a raw debugger to a selling pipeline workflow.

## Goals

- Add a new local entry point at `scripts/a2a/selling_console.py`.
- Add static web assets under `scripts/a2a/selling_console_web/`.
- Support the complete selling pipeline loop:
  - Start from a deployment requirement prompt.
  - Stream A2A pipeline events.
  - Render requirement understanding, architecture planning, candidate evaluation, candidate selection, deployment, rollback, and handoff states.
  - Handle `input_required` pauses for clarification, candidate choice, deployment confirmation, and permission-related waits.
  - Continue normal chat after `pipeline_handoff_ready`.
  - Cancel active tasks and fetch current pipeline state.
- Match the screenshots closely enough for product review:
  - Alibaba Cloud-style top bar.
  - Left workflow cards with green completion checks.
  - Expanded "Plan selection" section with candidate radio cards.
  - Bottom composer with "Deep thinking", attachment, and send controls.
  - Right plan cards with orange monthly price, summary, and blue highlights.
  - Desktop layout with a narrow left rail and right utility rail.
- Keep the tool local-only and unauthenticated, following the debugger model.

## Non-Goals

- Do not replace `scripts/a2a/debugger.py`.
- Do not introduce a frontend framework or package manager.
- Do not call real Alibaba Cloud APIs from the console itself.
- Do not add authentication, account switching, or real console navigation behavior.
- Do not rely on real LLM, real cloud credentials, or network calls in tests.

## Architecture

The new Python server follows the debugger's self-contained pattern:

- `SellingConsoleConfig`
  - host, port, default A2A server URL, default cwd, log directory, optional replay export.
- Protocol helpers
  - Reuse or mirror the debugger semantics for:
    - URL normalization.
    - JSON fetch with A2A headers.
    - `SendStreamingMessage`.
    - `GetTask`.
    - `CancelTask`.
    - Pipeline state fetch.
    - SSE line parsing.
    - debug log append/load.
- HTTP routes
  - `/` serves `index.html`.
  - `/styles.css` and `/app.js` serve static assets safely from `selling_console_web`.
  - `/api/health` proxies server health and agent card.
  - `/api/message/stream` proxies A2A streaming responses.
  - `/api/pipeline/state` proxies `iac-code/pipeline/state`.
  - `/api/task/get` proxies `GetTask`.
  - `/api/task/cancel` proxies `CancelTask`.

The frontend owns the selling-specific rendering. It should not depend on hidden globals from `debugger.py`; instead, it receives defaults from a small JSON bootstrap script and uses browser-native JavaScript.

## Frontend Layout

The page uses three primary bands:

- Top console bar
  - Menu icon, Alibaba Cloud mark, workspace button, account resources dropdown, region dropdown, search box, docs/cost/ticket links, language and notification icons, user badge.
  - These controls are visual affordances only.
- Main shell
  - Left narrow assistant rail with the robot avatar.
  - Left workflow panel, fixed around the screenshot proportions on desktop.
  - Right content area titled "Your purchase plans".
  - Right floating utility rail.
- Bottom composer inside the left panel
  - Placeholder text for continuing or refining requirements.
  - "Deep thinking" toggle-style button.
  - Attachment icon and send icon button.
  - Disclaimer text matching the screenshot tone.

The layout must remain usable on narrow screens. Below desktop widths, the plan area stacks under the workflow panel and all text must fit without overlap.

## Pipeline State Model

The frontend reducer translates A2A events and snapshots into a UI model:

- Session identity
  - `contextId`, `pipelineTaskId`, `activeTaskId`, `lastSequence`, `status`, `normalHandoffReady`.
- Steps
  - `intent_parsing` -> "Requirement understanding".
  - `architecture_planning` -> "Architecture planning".
  - `evaluate_candidates` and candidate sub-steps -> "Plan evaluation".
  - `confirm_and_select` -> "Plan selection".
  - `deploying` -> "Deployment".
- Candidate data
  - Candidate name, zero-based index, summary, cost items, total monthly cost, diagram/artifact metadata, source raw event.
  - Prefer `display.candidateDetails` and `candidate_detail` events.
  - Fall back to `complete_step.conclusion.options` and candidate snapshot data.
- Pending input
  - Question prompt, options, free-text allowance, related step/candidate coordinates.
- Permission state
  - Tool name, safe summary, decision status, guidance text.
- Raw diagnostics
  - Keep a collapsible debug drawer for requests, SSE events, and snapshots so protocol problems remain inspectable.

The reducer must be tolerant of both camelCase and snake_case fields, because existing debugger code handles both.

## Interaction Flow

1. User enters a requirement in the composer and sends it.
2. The frontend posts `/api/message/stream` with `serverUrl`, `cwd`, current `contextId`, current stream task id, and prompt.
3. The stream parser reads SSE chunks incrementally, parses `data:` lines, appends diagnostics, and applies pipeline envelopes as they arrive.
4. If an `input_required` event or A2A input-required task status arrives, the stream reader cancels the browser reader and shows the pending question in the workflow panel.
5. When candidates are available, the console renders:
   - radio cards in the left "Plan selection" section;
   - larger plan cards in the right content area.
6. Choosing a candidate sets local selection state. Sending the selection emits a natural-language follow-up, such as `选择方案0` for candidate index 0.
7. Deployment confirmation and permission waits use the same pending-input path. The user response is sent as the next message in the same context.
8. When `pipeline_handoff_ready` switches to normal mode, clear the active pipeline task id and keep the context id so follow-up chat starts a new normal task.

## Visual Details

Colors and spacing should approximate the screenshots:

- White page background with subtle blue/pink glow behind the left panel.
- Thin borders around workflow cards and plan cards.
- Alibaba Cloud orange for the logo and price.
- Bright green status checks.
- Blue highlight text for plan advantages.
- Rounded corners around cards, no nested decorative cards beyond the repeated workflow and plan cards.
- Compact but readable Chinese-first copy.

Icons should be implemented as inline buttons using simple CSS or Unicode where no icon library exists. The implementation should avoid external CDNs.

## Error Handling

- Invalid server URL, missing cwd, and missing prompt show inline composer errors.
- Proxy failures show an alert row in the workflow panel and a diagnostic row in the debug drawer.
- Empty streams append a diagnostic event and leave the page interactive.
- Cancel failure displays an inline error without clearing the current state.
- Snapshot fetch failures do not destroy existing UI state.

## Tests

Add focused pytest coverage under `tests/a2a/`:

- Script help exits successfully and describes the local selling console.
- Static index route serves HTML with default server URL and cwd.
- Static asset serving rejects path traversal.
- Defaults JSON is safe in script context.
- API payload builders preserve A2A v1 method names and cwd metadata.
- Existing debugger proxy behavior is not changed.
- Embedded or external JavaScript passes `node --check` when Node is installed.

Manual/browser verification:

- Start the local server.
- Open the page in the in-app browser.
- Verify desktop screenshot fit: top bar, workflow panel, plan cards, and utility rail are visible without overlap.
- Verify narrow viewport fit: content stacks and text remains readable.
- Exercise a mocked or replayed candidate-selection state if a real A2A server is unavailable.

## Acceptance Criteria

- `scripts/a2a/selling_console.py` can run locally with `uv run python scripts/a2a/selling_console.py`.
- The page visually matches the provided screenshots in structure, spacing, colors, and key copy.
- The console can start a selling pipeline request against a local A2A server.
- The console can render pipeline progress, candidate options, right-side plan cards, pending input, deployment status, and normal handoff.
- Tests for the new script and assets pass.
- Relevant existing A2A debugger tests still pass.
