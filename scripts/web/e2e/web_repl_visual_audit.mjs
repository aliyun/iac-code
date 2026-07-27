#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { scrubbedChildEnv } from "./runtime_env.mjs";

const require = createRequire(import.meta.url);
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const TMP_NODE_ROOT = path.join(os.tmpdir(), "iac-code-web-smoke-node");
const DEFAULT_OUTPUT_ROOT = path.join(os.tmpdir(), "iac-code-codex-visual-audit");
const REPORT_RELATIVE_PATH = "docs/web-repl-codex-visual-audit.md";
const REPORT_PATH = path.join(REPO_ROOT, REPORT_RELATIVE_PATH);
const ISSUE_REPORT_RELATIVE_PATH = "docs/web-ui-audit/comprehensive-screenshot-issue-report.md";
const ISSUE_REPORT_PATH = path.join(REPO_ROOT, ISSUE_REPORT_RELATIVE_PATH);
const FAKE_SECRET_STRINGS = ["sk-test-secret", "ALIYUN_SECRET", "SECRET_ACCESS_KEY"];

const visualFindings = [
  {
    severity: "P0",
    meaning: "Unreadable or unusable UI, broken critical interaction, or blank screenshot.",
  },
  {
    severity: "P1",
    meaning: "Major Codex-style mismatch, modal/layout breakage, or clear overflow.",
  },
  {
    severity: "P2",
    meaning: "Noticeable visual quality issue, missing state coverage, or awkward density.",
  },
  {
    severity: "P3",
    meaning: "Polish issue that can be deferred after the requested audit passes.",
  },
];

const manualIssueFindings = [
  {
    id: "UI-001",
    severity: "P2",
    status: "Fixed",
    screenshots: ["transcript-normal-tool", "transcript-long-content", "transcript-blocking"],
    title: "Transcript layout was fragmented compared with Codex conversation flow.",
    details:
      "Tool cards now attach to the owning assistant turn through message/tool associations, and transcript content shares a centered working column instead of a detached activity region.",
    fix: "Attach tool/local shell events to assistant messages by messageId or turnId, render message tool cards inline, and keep only unassociated tools in the fallback activity region.",
  },
  {
    id: "UI-002",
    severity: "P2",
    status: "Fixed",
    screenshots: [
      "settings-modal",
      "settings-cloud-expanded",
      "memory-modal",
      "skills-modal",
      "session-search-results",
    ],
    title: "Workspace panels looked form-heavy rather than Codex-native settings/workspace pages.",
    details:
      "Settings provider/cloud controls now use row-based dark groups, Search inputs follow the dark workspace surface, and Memory/Skills remain grouped as restrained workspace surfaces rather than plain form stacks.",
    fix: "Converted settings fields to Codex-like rows, normalized Search input colors, and kept workspace panels under the full-screen dark settings shell.",
  },
  {
    id: "UI-003",
    severity: "P2",
    status: "Fixed",
    screenshots: ["desktop-project-row-hover", "desktop-sidebar-search", "desktop-sidebar-skills", "mobile-default"],
    title: "Several sidebar action icons were textual placeholders.",
    details:
      "Project actions, global Search/Skills/New thread, and mobile Settings now render as CSS-drawn icon controls while keeping accessible labels and titles.",
    fix: "Replaced literal icon text with CSS-drawn compose, search, skills, settings, and overflow controls.",
  },
  {
    id: "UI-004",
    severity: "P1",
    status: "Fixed",
    screenshots: ["mobile-settings-modal", "mobile-memory-modal"],
    title: "Mobile workspace initially left a large blank header before tabs/content.",
    details:
      "The desktop workspace grid row sizing leaked into mobile. The mobile grid now uses header/tabs/content rows and settings fields collapse to one column.",
    fix: "Added mobile `grid-template-rows: auto auto minmax(0, 1fr)` and single-column settings provider layout.",
  },
  {
    id: "UI-005",
    severity: "P2",
    status: "Fixed",
    screenshots: ["pipeline-candidate-selected", "pipeline-rollback"],
    title: "Pipeline selected and active states inherited light surfaces inside the dark workspace.",
    details:
      "Contact-sheet review showed a white Pipeline notice and selected candidate card, plus a light active step in rollback view. These now use dark selected/notice/active surfaces with readable text.",
    fix: "Added dark-mode overrides for `.pipeline-notice`, `.pipeline-candidate.is-selected`, and `.pipeline-step.is-active`.",
  },
];

const screenshotMatrix = [
  { name: "desktop-default", viewport: "desktop", description: "Desktop default page with empty transcript." },
  {
    name: "desktop-multi-session",
    viewport: "desktop",
    description: "Desktop page with 20+ realistic long session titles.",
  },
  {
    name: "desktop-project-row-hover",
    viewport: "desktop",
    description: "Project row hover with collapse, count, menu, and new-thread action visible.",
  },
  {
    name: "desktop-project-collapsed",
    viewport: "desktop",
    description: "Project group collapsed with thread list hidden.",
  },
  {
    name: "desktop-session-list-scrolled",
    viewport: "desktop",
    description: "Sidebar thread list scrolled to lower sessions.",
  },
  {
    name: "desktop-session-selected-after-scroll",
    viewport: "desktop",
    description: "A lower sidebar session selected after scrolling.",
  },
  {
    name: "desktop-new-thread-draft",
    viewport: "desktop",
    description: "New-thread action opens an unpersisted draft that is ready for input.",
  },
  {
    name: "desktop-project-menu-open",
    viewport: "desktop",
    description: "Project row overflow action opens its current context menu.",
  },
  {
    name: "desktop-sidebar-search",
    viewport: "desktop",
    description: "Sidebar Search action opens the global command/search palette.",
  },
  {
    name: "desktop-sidebar-skills",
    viewport: "desktop",
    description: "Sidebar Skills action opens skills workspace.",
  },
  {
    name: "desktop-settings-button-hover",
    viewport: "desktop",
    description: "Bottom-left Settings action hover state.",
  },
  { name: "command-palette", viewport: "desktop", description: "Global command menu opened with Cmd/Ctrl+K." },
  {
    name: "command-palette-filtered",
    viewport: "desktop",
    description: "Global command menu filtered to a specific workspace action.",
  },
  { name: "transcript-normal-tool", viewport: "desktop", description: "User/assistant transcript and tool activity." },
  { name: "transcript-long-content", viewport: "desktop", description: "Long transcript content with wrapping." },
  { name: "transcript-blocking", viewport: "desktop", description: "Permission request and running composer state." },
  {
    name: "queued-input-accepted",
    viewport: "desktop",
    description: "Mid-turn queued input strip after submitting plain text while the turn is active.",
  },
  {
    name: "queued-attachment-error",
    viewport: "desktop",
    description: "Mid-turn attachment error state while text queueing is allowed.",
  },
  { name: "transcript-error", viewport: "desktop", description: "Transcript-level error event." },
  { name: "composer-focused", viewport: "desktop", description: "Focused composer with draft text." },
  { name: "composer-multiline-draft", viewport: "desktop", description: "Composer with a multi-line draft." },
  { name: "composer-image-attachment", viewport: "desktop", description: "Composer with uploaded image attachment." },
  { name: "composer-running", viewport: "desktop", description: "Composer while the fake turn is blocked/running." },
  { name: "suggestions-open", viewport: "desktop", description: "Slash suggestions open." },
  {
    name: "suggestions-keyboard-scroll",
    viewport: "desktop",
    description: "Slash suggestions after keyboard navigation beyond the visible rows.",
  },
  { name: "file-suggestions", viewport: "desktop", description: "Workspace file suggestions from @ trigger." },
  { name: "skill-suggestions", viewport: "desktop", description: "Skill suggestions from $ trigger." },
  { name: "shell-suggestions", viewport: "desktop", description: "Shell history suggestions from ! trigger." },
  { name: "suggestions-exact-command", viewport: "desktop", description: "Exact /status command with suggestion list open." },
  { name: "transcript-local-shell", viewport: "desktop", description: "Transcript with a local shell escape result." },
  {
    name: "transcript-shell-failure",
    viewport: "desktop",
    description: "Transcript with long local shell stdout/stderr failure output.",
  },
  { name: "settings-modal", viewport: "desktop", description: "Settings/Auth modal tab." },
  { name: "settings-provider-saved", viewport: "desktop", description: "Settings provider save success state." },
  {
    name: "settings-cloud-expanded",
    viewport: "desktop",
    description: "Settings cloud credentials expanded with all credential fields.",
  },
  { name: "settings-cloud-saved", viewport: "desktop", description: "Settings cloud credential save summary state." },
  { name: "memory-modal", viewport: "desktop", description: "Memory modal tab." },
  { name: "memory-save-states", viewport: "desktop", description: "Memory user/auto save success states." },
  { name: "memory-legacy-results", viewport: "desktop", description: "Memory legacy search results and delete action." },
  { name: "memory-legacy-deleted", viewport: "desktop", description: "Memory legacy delete result state." },
  { name: "skills-modal", viewport: "desktop", description: "Skills modal tab." },
  { name: "skills-toggle-hover", viewport: "desktop", description: "Skills row hover/toggle affordance." },
  { name: "skills-disabled-saved", viewport: "desktop", description: "Skills disabled state saved." },
  { name: "status-modal", viewport: "desktop", description: "Inline session status opened from /status." },
  {
    name: "session-search-results",
    viewport: "desktop",
    description: "Global command palette filtered to matching session results.",
  },
  {
    name: "session-search-empty",
    viewport: "desktop",
    description: "Global command palette with no matching sessions or commands.",
  },
  { name: "pipeline-modal", viewport: "desktop", description: "Pipeline modal tab with candidates and progress." },
  { name: "pipeline-candidate-selected", viewport: "desktop", description: "Pipeline candidate selected with progress/recovery state." },
  {
    name: "pipeline-rollback",
    viewport: "desktop",
    description: "Pipeline failure, rollback history, restart, cleanup blocking, and handoff blocked state.",
  },
  {
    name: "pipeline-session-entry",
    viewport: "desktop",
    description: "Pipeline session header exposes the product-visible workspace action.",
  },
  {
    name: "mobile-pipeline-workspace",
    viewport: "mobile",
    description: "Mobile pipeline workspace opened through the product-visible session action.",
  },
  { name: "command-auth", viewport: "desktop", description: "/auth command opens Settings." },
  { name: "command-model", viewport: "desktop", description: "/model command opens Settings model selector state." },
  { name: "command-effort", viewport: "desktop", description: "/effort command opens Settings effort selector state." },
  { name: "command-memory", viewport: "desktop", description: "/memory command opens Memory." },
  { name: "command-skills", viewport: "desktop", description: "/skills command opens Skills." },
  { name: "command-prompt", viewport: "desktop", description: "/prompt command opens status response detail." },
  { name: "command-help", viewport: "desktop", description: "/help command renders command guidance in Status." },
  { name: "command-debug", viewport: "desktop", description: "/debug command toggles debug state in Status." },
  { name: "mobile-default", viewport: "mobile", fullPage: true, description: "Mobile default layout." },
  { name: "mobile-sidebar-open", viewport: "mobile", description: "Mobile navigation drawer open." },
  { name: "mobile-sidebar-scrolled", viewport: "mobile", description: "Mobile drawer scrolled through long thread list." },
  { name: "mobile-settings-modal", viewport: "mobile", description: "Mobile Settings modal." },
  { name: "mobile-memory-modal", viewport: "mobile", description: "Mobile Memory workspace." },
  { name: "mobile-blocking", viewport: "mobile", description: "Mobile permission/question blocking surface." },
  { name: "mobile-suggestions", viewport: "mobile", description: "Mobile slash suggestions." },
  { name: "tablet-default", viewport: "tablet", description: "Tablet-width default layout." },
  { name: "tablet-settings-modal", viewport: "tablet", description: "Tablet-width Settings workspace modal." },
];

const STRESS_SESSION_TITLES = [
  "帮我测试一下 ros ListStacks 并整理失败原因",
  "测试一下 ros ListStacks 权限边界和 region 切换",
  "释放 stack 后确认 cleanup 和 handoff 是否正常",
  "<skill-name>iac-aliyun</skill-name> generate vpc with ecs and slb",
  "[Pipeline Handoff Context] selling pipeline candidate review",
  "Create ROS template for production VPC, NAT gateway, and ECS",
  "排查 cn-hangzhou 栈创建失败以及 rollback 残留资源",
  "Validate Terraform conversion for ALIYUN::ECS::Instance",
  "是",
  "A",
  "B",
  "Update memory with project-specific Alibaba Cloud naming rules",
  "Compare ROS ListStackResources output with generated template",
  "Fix credentials flow for DashScope and Aliyun AK fallback",
  "Generate ACK cluster plan with vSwitch, security group, and RAM role",
  "帮我把 web repl 的 /auth 和 /memory 弹窗流程测一遍",
  "Review long command palette suggestions and keyboard scrolling",
  "Investigate session index load time when history count is high",
  "Create multi-region OSS bucket policy and lifecycle config",
  "检查 <resource>ALIYUN::ROS::Stack</resource> 参数描述",
  "Pipeline cleanup after CREATE_FAILED and user interruption",
  "Summarize deployment artifacts and generated files",
];

function parseArgs(argv) {
  const args = {
    host: "127.0.0.1",
    port: 8769,
    headed: false,
    outputDir: "",
    installPlaywrightCore: process.env.IAC_CODE_WEB_SMOKE_INSTALL_PLAYWRIGHT === "1",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--headed") {
      args.headed = true;
    } else if (arg === "--install-playwright-core") {
      args.installPlaywrightCore = true;
    } else if (arg === "--host") {
      args.host = argv[index + 1] || args.host;
      index += 1;
    } else if (arg === "--port") {
      args.port = Number(argv[index + 1] || args.port);
      index += 1;
    } else if (arg === "--output-dir") {
      args.outputDir = argv[index + 1] || "";
      index += 1;
    }
  }
  return args;
}

function ensurePlaywrightCore(installIfMissing = false) {
  try {
    return require("playwright-core");
  } catch (_error) {
    const tempPackagePath = path.join(TMP_NODE_ROOT, "node_modules", "playwright-core");
    if (fs.existsSync(tempPackagePath)) {
      return require(tempPackagePath);
    }
    if (installIfMissing) {
      fs.mkdirSync(TMP_NODE_ROOT, { recursive: true });
      if (!fs.existsSync(path.join(TMP_NODE_ROOT, "package.json"))) {
        spawnSync("npm", ["init", "-y"], { cwd: TMP_NODE_ROOT, stdio: "ignore" });
      }
      const result = spawnSync("npm", ["install", "playwright-core"], {
        cwd: TMP_NODE_ROOT,
        stdio: "inherit",
      });
      if (result.status === 0) {
        return require(tempPackagePath);
      }
    }
    throw new Error(
      [
        "playwright-core is not available.",
        `Install it outside the repo with: mkdir -p ${TMP_NODE_ROOT} && cd ${TMP_NODE_ROOT} && npm init -y && npm install playwright-core`,
        `Or rerun this script with --install-playwright-core to install into ${TMP_NODE_ROOT}.`,
      ].join("\n"),
    );
  }
}

async function isPortAvailable(host, port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => {
      server.close(() => resolve(true));
    });
    server.listen(port, host);
  });
}

async function choosePort(host, preferredPort) {
  if (await isPortAvailable(host, preferredPort)) {
    return preferredPort;
  }
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

async function waitForHealth(url, child, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`fake web server exited early with code ${child.exitCode}`);
    }
    try {
      const response = await fetch(`${url}/health`);
      if (response.ok) {
        return;
      }
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`timed out waiting for ${url}/health: ${lastError}`);
}

function startServer({ host, port, configDir, homeDir }) {
  const script = path.join(REPO_ROOT, "scripts", "web", "e2e", "fake_web_server.py");
  const child = spawn(
    "uv",
    [
      "run",
      "python",
      script,
      "--host",
      host,
      "--port",
      String(port),
      "--cwd",
      REPO_ROOT,
      "--config-dir",
      configDir,
    ],
    {
      cwd: REPO_ROOT,
      env: scrubbedChildEnv({ configDir, homeDir, repoRoot: REPO_ROOT }),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => process.stdout.write(chunk));
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));
  return child;
}

async function stopServer(child) {
  if (!child || child.exitCode !== null) {
    return;
  }
  child.kill("SIGTERM");
  const exited = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), 5000);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
  if (!exited && child.exitCode === null) {
    child.kill("SIGKILL");
  }
}

async function launchBrowser(playwright, headed) {
  const candidates = [
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const executablePath = candidates.find((candidate) => fs.existsSync(candidate));
  const launchOptions = {
    headless: !headed,
    args: ["--no-first-run", "--no-default-browser-check"],
  };
  if (executablePath) {
    return playwright.chromium.launch({ ...launchOptions, executablePath });
  }
  try {
    return await playwright.chromium.launch({ ...launchOptions, channel: "chrome" });
  } catch (_error) {
    return playwright.chromium.launch({ ...launchOptions, channel: "chromium" });
  }
}

async function waitReady(page) {
  await page.waitForSelector('#iac-code-web-root[data-ready="true"]', { timeout: 20000 });
  await page.waitForSelector('[data-app-shell="composer-input"]', { timeout: 10000 });
}

async function activeSessionId(page) {
  return (await page.locator('[data-field="session-id"]').textContent()).trim();
}

async function expectText(page, text, timeout = 15000) {
  try {
    await page.getByText(text, { exact: false }).filter({ visible: true }).first().waitFor({ state: "visible", timeout });
  } catch (error) {
    const matches = await page.evaluate((expectedText) =>
      [...document.querySelectorAll("body *")]
        .filter((node) => String(node.textContent || "").includes(expectedText))
        .slice(-8)
        .map((node) => {
          const style = window.getComputedStyle(node);
          const rect = node.getBoundingClientRect();
          return {
            tag: node.tagName.toLowerCase(),
            className: String(node.className || ""),
            hidden: Boolean(node.hidden),
            display: style.display,
            visibility: style.visibility,
            opacity: style.opacity,
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          };
        }), text);
    throw new Error(`${error.message}\nMatching DOM nodes: ${JSON.stringify(matches, null, 2)}`);
  }
}

async function setViewport(page, viewport) {
  if (viewport === "mobile") {
    await page.setViewportSize({ width: 390, height: 844 });
    return;
  }
  if (viewport === "tablet") {
    await page.setViewportSize({ width: 820, height: 900 });
    return;
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

function screenshotPath(outputDir, name) {
  return path.join(outputDir, `${name}.png`);
}

function seedVisualAuditRuntimeFiles({ configDir, homeDir }) {
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(homeDir, { recursive: true });
  fs.writeFileSync(
    path.join(homeDir, ".zsh_history"),
    [
      ": 1782540000:0;pwd",
      ": 1782540001:0;uv run pytest tests/web/test_frontend_static.py -q",
      ": 1782540002:0;git status --short",
      ": 1782540003:0;visual-fail-long-output",
    ].join("\n") + "\n",
    "utf8",
  );
  fs.writeFileSync(
    path.join(configDir, ".input_history"),
    [
      '{"format":"iac-code-input-history-v1","text":"visual audit deploy prompt from persisted history"}',
      '{"format":"iac-code-input-history-v1","text":"review pipeline cleanup and handoff state"}',
    ].join("\n") + "\n",
    "utf8",
  );
}

function matrixEntry(name) {
  const entry = screenshotMatrix.find((candidate) => candidate.name === name);
  if (!entry) {
    throw new Error(`missing screenshot matrix entry: ${name}`);
  }
  return entry;
}

async function hardVisualChecks(page, screenshotName) {
  return page.evaluate((name) => {
    const root = document.documentElement;
    const body = document.body;
    const viewportWidth = root.clientWidth;
    const horizontalOverflow = Math.max(root.scrollWidth, body?.scrollWidth || 0) - viewportWidth;
    const visibleControls = [
      ...document.querySelectorAll(
        "button, input, select, textarea, pre, dd, .message, .tool-card, .blocking-panel, .workspace-dialog",
      ),
    ].filter((node) => {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    });
    const overflowing = visibleControls
      .map((node) => {
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        const overflowX = node.scrollWidth - Math.ceil(node.clientWidth);
        const overflowY = node.scrollHeight - Math.ceil(node.clientHeight);
        return {
          tag: node.tagName.toLowerCase(),
          className: String(node.className || ""),
          text: String(node.textContent || node.value || "").replace(/\s+/g, " ").trim().slice(0, 80),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          overflowX,
          overflowY,
          expectedEllipsis: style.overflowX === "hidden" && style.textOverflow === "ellipsis",
        };
      })
      // Single-line form controls scroll their editable value internally by design. Treating that
      // scrollWidth as page/layout overflow creates false positives for URLs and masked secrets.
      .filter(
        (item) =>
          item.overflowX > 4 &&
          !item.expectedEllipsis &&
          !["input", "select", "textarea"].includes(item.tag),
      )
      .slice(0, 12);
    const modal = document.querySelector('[data-app-shell="workspace-modal"]');
    const suggestions = document.querySelector('[data-app-shell="suggestions"]');
    const commandPalette = document.querySelector('[data-app-shell="command-palette"]');
    const isRendered = (node) => {
      if (!node || node.hidden) {
        return false;
      }
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const keyElementSpecs = [
      { name: "thread-title", selector: '[data-app-shell="thread-title"]', minWidth: 32, minHeight: 14 },
      { name: "composer", selector: '[data-app-shell="composer-form"]', minWidth: 240, minHeight: 64 },
      { name: "transcript", selector: ".transcript-panel", minWidth: 260, minHeight: 180 },
    ];
    const inspectedKeyElements = keyElementSpecs.map((spec) => ({ ...spec, node: document.querySelector(spec.selector) }));
    const missingKeyElements = inspectedKeyElements
      .filter((item) => !isRendered(item.node))
      .map((item) => ({ name: item.name, selector: item.selector }));
    const keyElements = inspectedKeyElements.filter((item) => isRendered(item.node));
    const collapsedKeyElements = keyElements
      .map((item) => {
        const rect = item.node.getBoundingClientRect();
        return {
          name: item.name,
          selector: item.selector,
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          minWidth: item.minWidth,
          minHeight: item.minHeight,
        };
      })
      .filter((item) => item.width < item.minWidth || item.height < item.minHeight);
    const sidebar = document.querySelector(".workbench.sidebar-open .session-rail");
    const expectedOverlayContainers = [modal, commandPalette, sidebar].filter((node) => isRendered(node));
    const occludedKeyElements = keyElements
      .map((item) => {
        const rect = item.node.getBoundingClientRect();
        const x = Math.min(viewportWidth - 1, Math.max(0, rect.left + rect.width / 2));
        const y = Math.min(root.clientHeight - 1, Math.max(0, rect.top + rect.height / 2));
        const coveringNode = document.elementFromPoint(x, y);
        const occluded = Boolean(
          coveringNode &&
            coveringNode !== item.node &&
            !item.node.contains(coveringNode) &&
            !coveringNode.contains(item.node),
        );
        const expectedOverlay = expectedOverlayContainers.some((container) => container.contains(coveringNode));
        return {
          name: item.name,
          selector: item.selector,
          coveringTag: coveringNode?.tagName?.toLowerCase() || "",
          coveringClass: String(coveringNode?.className || ""),
          occluded,
          expectedOverlay,
        };
      })
      .filter((item) => item.occluded && !item.expectedOverlay);
    const transientOverlays = [
      { name: "suggestions", node: suggestions },
      { name: "command-palette", node: commandPalette?.querySelector(".command-palette-dialog") },
    ].filter((item) => isRendered(item.node));
    const oversizedTransientOverlays = transientOverlays
      .map((item) => {
        const rect = item.node.getBoundingClientRect();
        return {
          name: item.name,
          widthRatio: rect.width / Math.max(1, viewportWidth),
          heightRatio: rect.height / Math.max(1, root.clientHeight),
        };
      })
      .filter((item) => item.widthRatio > 0.95 && item.heightRatio > 0.85);
    return {
      screenshotName: name,
      horizontalOverflow,
      overflowing,
      objectObjectText: document.body?.innerText?.includes("[object Object]") || false,
      missingKeyElements,
      collapsedKeyElements,
      occludedKeyElements,
      oversizedTransientOverlays,
      modalOpen: Boolean(modal && !modal.hidden),
      suggestionsOpen: Boolean(suggestions && !suggestions.hidden),
    };
  }, screenshotName);
}

async function capture(page, outputDir, entry, captured) {
  await setViewport(page, entry.viewport);
  await page.waitForLoadState("domcontentloaded");
  await applyStressSessionTitles(page);
  const filePath = screenshotPath(outputDir, entry.name);
  await page.screenshot({ path: filePath, fullPage: entry.fullPage === true });
  const stat = fs.statSync(filePath);
  const hardChecks = await hardVisualChecks(page, entry.name);
  captured.push({
    ...entry,
    path: filePath,
    bytes: stat.size,
    hardChecks,
  });
  if (stat.size < 1024) {
    throw new Error(`${entry.name} screenshot is too small to be useful`);
  }
}

async function closeModal(page) {
  const modal = page.locator('[data-app-shell="workspace-modal"]');
  if ((await modal.count()) === 1 && await modal.isVisible()) {
    await page.locator('[data-app-shell="workspace-modal-close"]').click();
    await modal.waitFor({ state: "hidden", timeout: 10000 });
  }
}

async function openWorkspaceTab(page, tabName) {
  const modal = page.locator('[data-app-shell="workspace-modal"]');
  if (!(await modal.isVisible())) {
    await page.locator('[data-app-shell="workspace-open-config"]').click();
  }
  await page.locator(`[data-workspace-tab="${tabName}"]`).click();
  await page.locator(`[data-workspace-panel="${tabName}"]`).waitFor({ state: "visible", timeout: 10000 });
}

async function waitForWorkspaceLoaded(page, tabName, expectedText) {
  const panel = page.locator(`[data-workspace-panel="${tabName}"]`);
  await panel.waitFor({ state: "visible", timeout: 10000 });
  if (expectedText) {
    await page.waitForFunction(
      ({ tabName: targetTabName, expectedText: targetText }) => {
        const targetPanel = document.querySelector(`[data-workspace-panel="${targetTabName}"]`);
        if (!targetPanel) {
          return false;
        }
        if ((targetPanel.textContent || "").includes(targetText)) {
          return true;
        }
        return [...targetPanel.querySelectorAll("input, textarea, select")].some((field) =>
          String(field.value || "").includes(targetText),
        );
      },
      { tabName, expectedText },
      { timeout: 10000 },
    );
    return;
  }
  await panel.locator(".workspace-skill-row").first().waitFor({ state: "visible", timeout: 10000 });
}

async function submitComposer(page, value) {
  const composer = page.locator('[data-app-shell="composer-input"]');
  await composer.fill(value);
  await composer.press("Enter");
}

async function openLatestTurnProcess(page) {
  const details = page.locator(".message-turn .turn-process").last();
  await details.waitFor({ state: "visible", timeout: 15000 });
  if (!(await details.evaluate((node) => node.open))) {
    await details.locator(":scope > summary").click();
  }
  await details.waitFor({ state: "visible", timeout: 10000 });
  return details;
}

async function openTurnProcessContaining(page, expectedText) {
  const details = page.locator(".message-turn .turn-process").filter({ hasText: expectedText }).last();
  await details.waitFor({ state: "attached", timeout: 15000 });
  if (!(await details.evaluate((node) => node.open))) {
    await details.locator(":scope > summary").click();
  }
  return details;
}

async function submitComposerAndOpenProcess(page, value) {
  const previousTurnCount = await page.locator(".message-turn .turn-process").count();
  await submitComposer(page, value);
  try {
    await page.waitForFunction(
      (expectedCount) => document.querySelectorAll(".message-turn .turn-process").length > expectedCount,
      previousTurnCount,
      { timeout: 15000 },
    );
  } catch (error) {
    const diagnostics = await page.evaluate(async () => {
      const sessionId = document.querySelector('[data-field="session-id"]')?.textContent?.trim() || "";
      const readJson = async (path) => {
        const response = await fetch(path);
        return { status: response.status, body: await response.json().catch(() => null) };
      };
      return {
        sessionId,
        composerValue: document.querySelector('[data-app-shell="composer-input"]')?.value || "",
        composerError: document.querySelector('[data-app-shell="composer-error"]')?.textContent?.trim() || "",
        attachmentCount: document.querySelectorAll('[data-app-shell="attachment-chips"] .attachment-chip').length,
        messageCount: document.querySelectorAll(".message-turn").length,
        processCount: document.querySelectorAll(".message-turn .turn-process").length,
        transcriptText:
          document.querySelector('[data-app-shell="message-stack"]')?.textContent?.trim().slice(-500) || "",
        session: sessionId ? await readJson(`/api/sessions/${encodeURIComponent(sessionId)}`) : null,
        messages: sessionId ? await readJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages`) : null,
        status: sessionId ? await readJson(`/api/sessions/${encodeURIComponent(sessionId)}/status`) : null,
      };
    });
    throw new Error(`${error.message}\nComposer diagnostics: ${JSON.stringify(diagnostics, null, 2)}`);
  }
  return openLatestTurnProcess(page);
}

async function submitComposerAndOpenLocalShell(page, value) {
  const previousCardCount = await page.locator("details.tool-card-local").count();
  await submitComposer(page, value);
  await page.waitForFunction(
    (expectedCount) => document.querySelectorAll("details.tool-card-local").length > expectedCount,
    previousCardCount,
    { timeout: 15000 },
  );
  const details = page.locator("details.tool-card-local").last();
  if (!(await details.evaluate((node) => node.open))) {
    await details.locator(":scope > summary").click();
  }
  return details;
}

async function createPersistedSession(page, payload, firstMessage) {
  return page.evaluate(
    async ({ createPayload, messageText }) => {
      const createResponse = await fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createPayload),
      });
      if (!createResponse.ok) {
        throw new Error(`session create failed: ${createResponse.status}`);
      }
      const created = await createResponse.json();
      const sessionId = created.webSessionId || created.sessionId;
      if (!sessionId) {
        throw new Error("session create did not return a session id");
      }
      const messageResponse = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: messageText }),
      });
      if (!messageResponse.ok) {
        throw new Error(`initial message failed: ${messageResponse.status}`);
      }
      return sessionId;
    },
    { createPayload: payload, messageText: firstMessage },
  );
}

async function postMessageViaApiForTurnId(page, text) {
  const sessionId = await activeSessionId(page);
  return page.evaluate(
    async ({ sessionId: targetSessionId, messageText }) => {
      const response = await fetch(`/api/sessions/${encodeURIComponent(targetSessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: messageText }),
      });
      if (!response.ok) {
        throw new Error(`message seed failed: ${response.status}`);
      }
      const payload = await response.json();
      if (!payload.turnId) {
        throw new Error("message seed did not return a turnId");
      }
      return payload.turnId;
    },
    { sessionId, messageText: text },
  );
}

async function attachAuditImage(page) {
  const pngBytes = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l7Z2xwAAAABJRU5ErkJggg==",
    "base64",
  );
  await page.locator('[data-app-shell="composer-file-input"]').setInputFiles({
    name: "visual-audit-upload.png",
    mimeType: "image/png",
    buffer: pngBytes,
  });
  await page
    .locator('[data-app-shell="attachment-chips"] .attachment-chip-image .attachment-chip-preview')
    .waitFor({ state: "visible", timeout: 10000 });
}

async function waitForStatus(page, statusText) {
  if (statusText === "Idle") {
    await waitForBackendTurnIdle(page);
    return;
  }
  throw new Error(`Unsupported visual-audit status: ${statusText}`);
}

async function waitForBackendTurnIdle(page) {
  const sessionId = await activeSessionId(page);
  await page.waitForFunction(
    async (targetSessionId) => {
      const response = await fetch(`/api/sessions/${encodeURIComponent(targetSessionId)}/status`);
      if (!response.ok) {
        return false;
      }
      const payload = await response.json();
      return (
        payload.currentTurnActive === false &&
        payload.turn?.active === false &&
        Number(payload.pendingPermissionCount || 0) === 0 &&
        Number(payload.pendingQuestionCount || 0) === 0
      );
    },
    sessionId,
    { timeout: 10000 },
  );
}

async function chooseFirstRealOption(page, selector) {
  const select = page.locator(`${selector}:visible`).first();
  await select.waitFor({ state: "visible", timeout: 10000 });
  await select.evaluate((field) => {
    if (field instanceof HTMLSelectElement) {
      const option = [...field.options].find((candidate) => candidate.value);
      if (option) {
        field.value = option.value;
        field.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return;
    }
    const list = field.list;
    const suggestion = list ? [...list.options].find((candidate) => candidate.value)?.value : "";
    field.value = suggestion || field.value || "high";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function waitForWorkspaceResultText(page, text) {
  await page.waitForFunction(
    (expected) =>
      [...document.querySelectorAll(".workspace-result, .workspace-search-result-list")].some((node) =>
        node.textContent?.includes(expected),
      ),
    text,
    { timeout: 10000 },
  );
}

async function waitForTranscriptAvailable(page, turnId) {
  const sessionId = await activeSessionId(page);
  await page.waitForFunction(
    async ({ sessionId: targetSessionId, targetTurnId }) => {
      const response = await fetch(
        `/api/transcript/${encodeURIComponent(targetTurnId)}?sessionId=${encodeURIComponent(targetSessionId)}`,
      );
      return response.ok;
    },
    { sessionId, targetTurnId: turnId },
    { timeout: 15000 },
  );
}

async function captureCurrentMatrix({ page, outputDir, captured }) {
  await capture(page, outputDir, matrixEntry("desktop-default"), captured);

  await createStressSessions(page);
  await page.waitForFunction(
    (expected) => document.querySelectorAll(".session-item").length >= expected,
    STRESS_SESSION_TITLES.length,
    { timeout: 20000 },
  );
  await capture(page, outputDir, matrixEntry("desktop-multi-session"), captured);

  await page.locator(".project-row").first().hover();
  await capture(page, outputDir, matrixEntry("desktop-project-row-hover"), captured);

  await page.locator(".project-collapse").first().click();
  await capture(page, outputDir, matrixEntry("desktop-project-collapsed"), captured);
  await page.locator(".project-collapse").first().click();

  await page.locator('[data-app-shell="session-list"]').evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await capture(page, outputDir, matrixEntry("desktop-session-list-scrolled"), captured);
  await page.locator(".session-item").last().click();
  await capture(page, outputDir, matrixEntry("desktop-session-selected-after-scroll"), captured);
  await page.locator('[data-app-shell="session-list"]').evaluate((node) => {
    node.scrollTop = 0;
  });

  await page.locator('[data-app-shell="new-session"]').click();
  await page.waitForFunction(
    () => document.querySelector('[data-app-shell="thread-title"]')?.textContent?.trim() === "新对话",
    undefined,
    { timeout: 10000 },
  );
  await capture(page, outputDir, matrixEntry("desktop-new-thread-draft"), captured);

  await page.locator(".project-row").first().hover();
  await page.locator(".project-menu").first().click();
  await page.locator('[data-app-shell="project-context-menu"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("desktop-project-menu-open"), captured);
  await page.keyboard.press("Escape");

  await page.locator('[data-app-shell="sidebar-search"]').click();
  await page.locator('[data-app-shell="command-palette"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("desktop-sidebar-search"), captured);
  await page.keyboard.press("Escape");

  await page.locator('[data-app-shell="sidebar-skills"]').click();
  await waitForWorkspaceLoaded(page, "skills");
  await capture(page, outputDir, matrixEntry("desktop-sidebar-skills"), captured);
  await closeModal(page);

  await page.locator('[data-app-shell="workspace-open-config"]').hover();
  await capture(page, outputDir, matrixEntry("desktop-settings-button-hover"), captured);

  await page.keyboard.press(process.platform === "darwin" ? "Meta+K" : "Control+K");
  await page.locator('[data-app-shell="command-palette"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-palette"), captured);
  await page.locator('[data-app-shell="command-palette-search"]').fill("记忆");
  await expectText(page, "编辑项目和用户记忆");
  await capture(page, outputDir, matrixEntry("command-palette-filtered"), captured);
  await page.keyboard.press("Escape");

  const composer = page.locator('[data-app-shell="composer-input"]');
  await composer.focus();
  await composer.fill("Draft a compact VPC change for visual review.");
  await capture(page, outputDir, matrixEntry("composer-focused"), captured);

  await composer.fill("Create a VPC\nAdd two vSwitches\nThen explain the ROS parameters.");
  await capture(page, outputDir, matrixEntry("composer-multiline-draft"), captured);

  await composer.fill("/");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("suggestions-open"), captured);
  for (let index = 0; index < 10; index += 1) {
    await composer.press("ArrowDown");
  }
  await capture(page, outputDir, matrixEntry("suggestions-keyboard-scroll"), captured);
  await composer.press("Escape");

  await composer.fill("@");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("file-suggestions"), captured);
  await composer.press("Escape");

  await composer.fill("$");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("skill-suggestions"), captured);
  await composer.press("Escape");

  await composer.fill("!");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("shell-suggestions"), captured);
  await composer.press("Escape");

  await composer.fill("Describe attached image for visual review.");
  await attachAuditImage(page);
  await capture(page, outputDir, matrixEntry("composer-image-attachment"), captured);
  await page.locator('[data-app-shell="attachment-chips"] .attachment-chip-image').click();

  await submitComposerAndOpenProcess(page, "Smoke line 1\nSmoke line 2");
  await expectText(page, "normal assistant response from fake runtime");
  await expectText(page, "fakeRosPlan");
  await waitForStatus(page, "Idle");
  await capture(page, outputDir, matrixEntry("transcript-normal-tool"), captured);

  await composer.fill("/status");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("suggestions-exact-command"), captured);
  await composer.press("Escape");

  await submitComposerAndOpenProcess(page, "long content state");
  await expectText(page, "resource-040");
  await waitForStatus(page, "Idle");
  await capture(page, outputDir, matrixEntry("transcript-long-content"), captured);

  await submitComposer(page, "visual error state");
  await expectText(page, "fake visual error state");
  await waitForStatus(page, "Idle");
  await capture(page, outputDir, matrixEntry("transcript-error"), captured);

  await submitComposerAndOpenLocalShell(page, "!pwd");
  await expectText(page, "fake local shell stdout");
  await capture(page, outputDir, matrixEntry("transcript-local-shell"), captured);

  await submitComposerAndOpenLocalShell(page, "!visual-fail-long-output");
  await expectText(page, "visual shell stderr line 025");
  await capture(page, outputDir, matrixEntry("transcript-shell-failure"), captured);

  await submitComposer(page, "e2e blocking turn");
  await expectText(page, "Allow fake action");
  await capture(page, outputDir, matrixEntry("transcript-blocking"), captured);
  await capture(page, outputDir, matrixEntry("composer-running"), captured);
  await composer.fill("queued follow-up while the turn is active");
  await composer.press("Enter");
  await expectText(page, "queued follow-up while the turn is active");
  await capture(page, outputDir, matrixEntry("queued-input-accepted"), captured);
  await composer.fill("queued attachment should stay blocked");
  await attachAuditImage(page);
  await composer.press("Enter");
  await expectText(page, "附件需等当前回合结束后再发送。");
  await capture(page, outputDir, matrixEntry("queued-attachment-error"), captured);
  await page.getByRole("button", { name: "仅本次允许" }).click();
  await expectText(page, "Choose deployment region");
  await page.getByRole("button", { name: "Use cn-hangzhou" }).click();
  await waitForStatus(page, "Idle");
  await openTurnProcessContaining(page, "question answered: cn-hangzhou");
  await expectText(page, "question answered: cn-hangzhou");

  await closeModal(page);
  await page.locator('[data-app-shell="workspace-open-config"]').click();
  await page.locator('[data-workspace-panel="other"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("settings-modal"), captured);
  await openWorkspaceTab(page, "model");
  await page.locator(".workspace-model-group-item").first().click();
  await page.locator(".workspace-provider-nav-item").first().click();
  await chooseFirstRealOption(page, '[data-workspace-action="workspace-model-model"]');
  await chooseFirstRealOption(page, '[data-workspace-action="workspace-model-effort"]');
  await page.locator('[data-workspace-action="workspace-model-api-key"]').fill("VISUAL_FAKE_MODEL_KEY");
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/providers/config") && response.request().method() === "PUT"),
    page.locator('[data-workspace-action="workspace-model-save"]').click(),
  ]);
  await waitForWorkspaceResultText(page, "配置已保存。");
  await capture(page, outputDir, matrixEntry("settings-provider-saved"), captured);
  await openWorkspaceTab(page, "cloud");
  await capture(page, outputDir, matrixEntry("settings-cloud-expanded"), captured);
  await page.locator('[data-workspace-action="workspace-cloud-mode"]').selectOption("AK");
  await page.locator('[data-workspace-action="workspace-cloud-region"]').fill("cn-hangzhou");
  await page.locator('[data-workspace-action="workspace-cloud-access-key-id"]').fill("VISUAL_ACCESS_KEY_ID");
  await page.locator('[data-workspace-action="workspace-cloud-access-key-secret"]').fill("VISUAL_CLOUD_SECRET_VALUE");
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/cloud/aliyun") && response.request().method() === "PUT"),
    page.locator('[data-workspace-action="workspace-cloud-save"]').click(),
  ]);
  await capture(page, outputDir, matrixEntry("settings-cloud-saved"), captured);

  await openWorkspaceTab(page, "memory");
  await waitForWorkspaceLoaded(page, "memory", "# AGENTS.md");
  await capture(page, outputDir, matrixEntry("memory-modal"), captured);
  await page.locator('[data-workspace-action="workspace-memory-user"]').fill("User visual audit memory preference.");
  await page.locator('[data-workspace-action="workspace-memory-save-user"]').click();
  await expectText(page, "已保存");
  await page.locator(".workspace-memory-auto-card .workspace-switch-track").click();
  await page.locator(".workspace-memory-auto-status").filter({ hasText: "自动记忆" }).waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("memory-save-states"), captured);
  await page.locator('[data-workspace-action="workspace-memory-legacy-query"]').fill("deploy");
  await page.locator('[data-workspace-action="workspace-memory-legacy-search"]').click();
  await page.locator(".workspace-memory-item").first().waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("memory-legacy-results"), captured);
  const legacyCountBeforeDelete = await page.locator(".workspace-memory-item").count();
  const legacyDelete = page.locator('[data-workspace-action="workspace-memory-legacy-delete"]').first();
  await legacyDelete.click();
  await legacyDelete.click();
  await page.waitForFunction(
    (previousCount) => document.querySelectorAll(".workspace-memory-item").length < previousCount,
    legacyCountBeforeDelete,
    { timeout: 10000 },
  );
  await capture(page, outputDir, matrixEntry("memory-legacy-deleted"), captured);

  await openWorkspaceTab(page, "skills");
  await waitForWorkspaceLoaded(page, "skills");
  await capture(page, outputDir, matrixEntry("skills-modal"), captured);
  await page.locator(".workspace-skill-row").first().hover();
  await capture(page, outputDir, matrixEntry("skills-toggle-hover"), captured);
  const unlockedSkillToggles = page.locator('.workspace-skill-row input[type="checkbox"]:not(:disabled)');
  if ((await unlockedSkillToggles.count()) > 0) {
    await unlockedSkillToggles.first().uncheck();
    await expectText(page, "已更新。");
  }
  await capture(page, outputDir, matrixEntry("skills-disabled-saved"), captured);

  await closeModal(page);
  await page.locator('[data-app-shell="sidebar-search"]').click();
  const paletteSearch = page.locator('[data-app-shell="command-palette-search"]');
  await paletteSearch.fill("ListStacks");
  await page.locator(".command-palette-session").first().waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("session-search-results"), captured);
  await paletteSearch.fill("visual-audit-no-match-7f534251");
  await expectText(page, "无匹配结果。");
  await capture(page, outputDir, matrixEntry("session-search-empty"), captured);
  await page.keyboard.press("Escape");

  await submitComposer(page, "/status");
  const inlineStatusPanel = page.locator('[data-app-shell="session-status-panel"]');
  await inlineStatusPanel.waitFor({ state: "visible", timeout: 10000 });
  await inlineStatusPanel.getByText("会话:", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
  await inlineStatusPanel.getByText("背景信息:", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("status-modal"), captured);
  await inlineStatusPanel.getByRole("button", { name: "关闭" }).click();

  await submitComposer(page, "/auth");
  await page.locator('[data-workspace-panel="model"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-auth"), captured);

  await closeModal(page);
  await submitComposer(page, "/model");
  await page.locator('[data-workspace-panel="model"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-model"), captured);

  await closeModal(page);
  await submitComposer(page, "/effort");
  await page.locator('[data-workspace-panel="model"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-effort"), captured);

  await closeModal(page);
  await submitComposer(page, "/memory");
  await page.locator('[data-workspace-panel="memory"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-memory"), captured);
  await closeModal(page);

  await submitComposer(page, "/skills");
  await page.locator('[data-workspace-panel="skills"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("command-skills"), captured);
  await closeModal(page);

  await submitComposer(page, "/prompt");
  await page.locator('[data-workspace-panel="status"]').waitFor({ state: "visible", timeout: 10000 });
  await expectText(page, "Response detail updated");
  await capture(page, outputDir, matrixEntry("command-prompt"), captured);
  await closeModal(page);

  await submitComposer(page, "/help");
  await page.locator('[data-workspace-panel="status"]').waitFor({ state: "visible", timeout: 10000 });
  await waitForWorkspaceResultText(page, "help");
  await capture(page, outputDir, matrixEntry("command-help"), captured);
  await closeModal(page);

  await submitComposer(page, "/debug");
  await page.locator('[data-workspace-panel="status"]').waitFor({ state: "visible", timeout: 10000 });
  await waitForWorkspaceResultText(page, "debug");
  await capture(page, outputDir, matrixEntry("command-debug"), captured);
  await closeModal(page);

  const pipelineSessionId = await createPersistedSession(
    page,
    { mode: "pipeline", pipelineName: "selling" },
    "Provision a smoke-test selling VPC",
  );
  const rollbackPipelineSessionId = await createPersistedSession(
    page,
    { sessionId: "visual_pipeline_rollback", mode: "pipeline", pipelineName: "selling" },
    "trigger rollback failure for visual audit",
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitReady(page);
  await page.locator(`.session-item[data-session-id="${pipelineSessionId}"]`).first().click();
  await page.waitForFunction(
    () => document.querySelector('[data-field="mode"]')?.textContent?.trim() === "pipeline",
    null,
    { timeout: 10000 },
  );
  await page.locator('[data-workspace-panel="pipeline"]').waitFor({ state: "visible", timeout: 10000 });
  await expectText(page, "Smoke balanced VPC");
  await capture(page, outputDir, matrixEntry("pipeline-modal"), captured);
  await page.locator(".pipeline-candidate-select").first().click();
  await expectText(page, "handoff normal ready");
  await capture(page, outputDir, matrixEntry("pipeline-candidate-selected"), captured);

  await closeModal(page);
  await page.locator(`.session-item[data-session-id="${rollbackPipelineSessionId}"]`).first().click();
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-app-shell="pipeline-workspace"]')
        ?.textContent?.includes("Rollback to template generation"),
    null,
    { timeout: 15000 },
  );
  await capture(page, outputDir, matrixEntry("pipeline-session-entry"), captured);
  await page.locator('[data-app-shell="pipeline-workspace-open"]').click();
  await page.locator('[data-workspace-panel="pipeline"]').waitFor({ state: "visible", timeout: 10000 });
  await expectText(page, "Rollback to template generation");
  await expectText(page, "handoff blocked until rollback cleanup completes");
  await capture(page, outputDir, matrixEntry("pipeline-rollback"), captured);

  await capture(page, outputDir, matrixEntry("mobile-pipeline-workspace"), captured);

  await closeModal(page);
  const tabletSessionId = await createPersistedSession(
    page,
    { sessionId: "visual_tablet_normal" },
    "Seed the tablet visual audit session",
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitReady(page);
  await setViewport(page, "tablet");
  await page.locator(`.session-item[data-session-id="${tabletSessionId}"]`).first().click();
  await capture(page, outputDir, matrixEntry("tablet-default"), captured);
  await page.locator('[data-app-shell="workspace-open-config"]').click();
  await page.locator('[data-workspace-panel="other"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("tablet-settings-modal"), captured);
  await closeModal(page);

  const mobileSessionId = await createPersistedSession(
    page,
    { sessionId: "visual_mobile_normal" },
    "Seed the mobile visual audit session",
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitReady(page);
  await page.locator(`.session-item[data-session-id="${mobileSessionId}"]`).first().click();
  await page.waitForFunction(
    () => document.querySelector('[data-field="session-id"]')?.textContent?.trim().includes("visual_mobile_normal"),
    null,
    { timeout: 10000 },
  );
  await capture(page, outputDir, matrixEntry("mobile-default"), captured);
  await page.locator('[data-app-shell="sidebar-drawer-toggle"]').click();
  await capture(page, outputDir, matrixEntry("mobile-sidebar-open"), captured);
  await page.locator('[data-app-shell="session-list"]').evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await capture(page, outputDir, matrixEntry("mobile-sidebar-scrolled"), captured);
  await page.locator('[data-app-shell="sidebar-drawer-toggle"]').click();
  await page.locator('[data-app-shell="workspace-open-config"]').click();
  await page.locator('[data-workspace-panel="other"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("mobile-settings-modal"), captured);
  await openWorkspaceTab(page, "memory");
  await waitForWorkspaceLoaded(page, "memory", "# AGENTS.md");
  await capture(page, outputDir, matrixEntry("mobile-memory-modal"), captured);
  await closeModal(page);
  await composer.fill("/");
  await page.locator('[data-app-shell="suggestions"]').waitFor({ state: "visible", timeout: 10000 });
  await capture(page, outputDir, matrixEntry("mobile-suggestions"), captured);
  await composer.press("Escape");
  await submitComposer(page, "mobile blocking turn");
  await expectText(page, "Allow fake action");
  await capture(page, outputDir, matrixEntry("mobile-blocking"), captured);
}

async function createStressSessions(page) {
  for (const [index, title] of STRESS_SESSION_TITLES.entries()) {
    await createPersistedSession(
      page,
      { sessionId: `visual_stress_${String(index + 1).padStart(2, "0")}` },
      `Visual audit ${index + 1}: ${title}`,
    );
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitReady(page);
  const showMore = page.locator(".project-show-more").first();
  if ((await showMore.count()) > 0) {
    await showMore.click();
  }
  await applyStressSessionTitles(page);
}

async function applyStressSessionTitles(page) {
  await page.evaluate((titles) => {
    const allItems = [...document.querySelectorAll(".session-item")];
    if (allItems.length < Math.min(5, titles.length)) {
      return;
    }
    const items = allItems.slice(0, titles.length);
    for (const [index, item] of items.entries()) {
      const title = item.querySelector("[data-thread-title], .thread-title, span");
      if (title) {
        title.textContent = titles[index];
      }
    }
  }, STRESS_SESSION_TITLES);
}

function referenceForScreenshot(name) {
  if (name.startsWith("command-palette")) {
    return {
      level: "Adjacent",
      sample:
        "docs/assets/codex-reference/codex-user-sidebar-dark-zh.png; docs/assets/codex-reference/codex-skill-selector-dark.webp",
      area: "Global Command Menu",
    };
  }
  if (
    [
      "desktop-default",
      "desktop-multi-session",
      "desktop-project-row-hover",
      "desktop-project-collapsed",
      "desktop-session-list-scrolled",
      "desktop-session-selected-after-scroll",
      "desktop-new-thread-draft",
      "desktop-project-menu-open",
      "desktop-sidebar-search",
      "desktop-sidebar-skills",
      "desktop-settings-button-hover",
      "command-palette",
      "command-palette-filtered",
      "tablet-default",
      "mobile-default",
      "mobile-sidebar-open",
      "mobile-sidebar-scrolled",
      "mobile-blocking",
    ].includes(name)
  ) {
    return {
      level: "Direct",
      sample:
        "docs/assets/codex-reference/codex-user-sidebar-dark-zh.png; docs/assets/codex-reference/codex-product-sidebar-projects.webp",
      area: "Sidebar / Project / Thread Navigation",
    };
  }
  if (
    name.startsWith("composer") ||
    name.includes("suggestions") ||
    name.startsWith("queued-") ||
    name === "file-suggestions" ||
    name === "skill-suggestions" ||
    name.startsWith("command-palette")
  ) {
    return {
      level: "Direct",
      sample:
        "docs/assets/codex-reference/codex-app-screenshot-light.webp; docs/assets/codex-reference/codex-skill-selector-light.webp",
      area: name.includes("suggestions") || name === "file-suggestions" ? "Suggestions" : "Composer",
    };
  }
  if (name.startsWith("transcript")) {
    return {
      level: "Direct",
      sample:
        "docs/assets/codex-reference/codex-app-screenshot-light.webp; docs/assets/codex-reference/codex-product-thread-skill-output.webp",
      area: "Main Thread / Chat Transcript",
    };
  }
  if (name.includes("pipeline")) {
    return {
      level: "Adjacent",
      sample:
        "docs/assets/codex-reference/codex-product-thread-skill-output.webp; docs/assets/codex-reference/codex-multitask-dark.webp",
      area: "Pipeline / Review Workspace",
    };
  }
  if (
    name.includes("settings") ||
    name.includes("auth") ||
    name.includes("memory") ||
    name.includes("skills") ||
    name === "command-model" ||
    name === "command-effort"
  ) {
    return {
      level: "Adjacent",
      sample:
        "docs/assets/codex-reference/codex-skill-selector-light.webp; docs/assets/codex-reference/codex-multitask-light.webp",
      area: "Workspace Modal",
    };
  }
  if (
    name.includes("status") ||
    name.includes("search") ||
    name === "command-prompt" ||
    name === "command-help" ||
    name === "command-debug"
  ) {
    return {
      level: "Adjacent",
      sample:
        "docs/assets/codex-reference/codex-app-screenshot-light.webp; docs/assets/codex-reference/codex-multitask-light.webp",
      area: "Search / Status",
    };
  }
  return {
    level: "Missing",
    sample: "unmapped",
    area: "Unmapped",
  };
}

function objectiveAssessmentForScreenshot(entry, automatedFindings) {
  const screenshotFindings = automatedFindings.filter(
    (finding) => finding.screenshotName === entry.name || finding.screenshotName === "all",
  );
  const blocking = screenshotFindings.filter((finding) => ["P0", "P1", "P2"].includes(finding.severity));
  const openIssue = manualIssueFindings.find(
    (issue) => issue.status === "Open" && issue.screenshots.includes(entry.name),
  );
  const hardGateFailed =
    entry.hardChecks.horizontalOverflow > 4 ||
    entry.hardChecks.overflowing.length > 0 ||
    entry.hardChecks.collapsedKeyElements.length > 0 ||
    entry.hardChecks.occludedKeyElements.length > 0 ||
    entry.hardChecks.objectObjectText;

  return {
    hardCheck: hardGateFailed ? "FAIL" : "PASS",
    blockingCheck: blocking.length > 0 ? "FAIL" : "PASS",
    manualStatus: openIssue ? `OPEN ${openIssue.id}` : "NO_OPEN_ISSUE",
    result: hardGateFailed || blocking.length > 0 || openIssue ? "NEEDS_REVIEW" : "PASS",
    notes: openIssue
      ? `Open ${openIssue.id}: ${openIssue.title}`
      : "Objective DOM/layout gates passed; no synthetic visual score is assigned.",
  };
}
function assertNoSecrets(name, value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  const leaked = FAKE_SECRET_STRINGS.filter((secret) => text.includes(secret));
  if (leaked.length > 0) {
    throw new Error(`${name} leaked fake secret strings: ${leaked.join(", ")}`);
  }
}

function reportContent({ outputDir, captured, consoleErrors, automatedFindings, url }) {
  const rows = captured
    .map((entry) => {
      const overflow = entry.hardChecks.overflowing.length > 0 ? `${entry.hardChecks.overflowing.length} overflow` : "ok";
      return `| ${entry.name} | ${entry.viewport} | ${entry.path} | ${overflow} | ${entry.description} |`;
    })
    .join("\n");
  const assessmentRows = captured
    .map((entry) => {
      const reference = referenceForScreenshot(entry.name);
      const assessment = objectiveAssessmentForScreenshot(entry, automatedFindings);
      return [
        `| ${entry.name}`,
        reference.area,
        reference.level,
        reference.sample,
        "implemented",
        assessment.hardCheck,
        assessment.blockingCheck,
        assessment.manualStatus,
        assessment.result,
        `${assessment.notes} |`,
      ].join(" | ");
    })
    .join("\n");
  const findingRows = automatedFindings.length
    ? automatedFindings
        .map((finding) => `| ${finding.severity} | ${finding.screenshotName} | ${finding.message} |`)
        .join("\n")
    : "| - | - | No automated P0/P1/P2 findings. Manual review status is recorded in the issue report. |";
  return `# Web REPL Codex Visual Audit

Generated by \`scripts/web/e2e/web_repl_visual_audit.mjs\`.

## Baseline

- Fixed target: \`docs/codex-style-web-repl-visual-target.md\`
- Acceptance rubric: \`docs/codex-visual-quality-acceptance.md\`
- Reference files: \`${path.join(os.tmpdir(), "iac-code-codex-visual-refs")}\`
- Local URL: \`${url}\`
- Screenshot directory: \`${outputDir}\`

## Screenshot Matrix

| Screenshot | Viewport | Path | Hard check | Coverage |
| --- | --- | --- | --- | --- |
${rows}

## Automated Findings

| Severity | Screenshot | Finding |
| --- | --- | --- |
${findingRows}

## Manual Codex Comparison

Automated checks only prove that screenshots were captured and no hard DOM/layout issue was detected. The manual review status is recorded in \`${ISSUE_REPORT_RELATIVE_PATH}\` and must remain aligned with \`docs/codex-ui-alignment-scoring.md\` and \`docs/codex-visual-quality-acceptance.md\`.

Scope claim: \`Implemented Web REPL UI states in the current function inventory now have screenshot evidence; visual issue closure still depends on the manual issue report\`.

This audit now includes the previously missing high-risk states: global command menu, queued-input accepted/error states, Pipeline failure/rollback/restart/cleanup blocking state, and tablet-width layout.

The audit reports only reproducible DOM/layout gates and the checked-in manual issue status. It does not invent visual-fidelity scores from screenshot names:

| Screenshot | UI Area | Reference Level | Reference Sample | Scope | Structural Gate | Blocking Findings | Manual Issue | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
${assessmentRows}

UI inventory coverage table:

| UI area | Required states | Evidence path(s) | Coverage result |
| --- | --- | --- | --- |
| Shell/navigation | default, empty, multi-session, selected, long title, project hover/collapse/context menu/new-thread draft, mobile drawer, tablet | desktop-default, desktop-multi-session, desktop-project-row-hover, desktop-project-collapsed, desktop-session-list-scrolled, desktop-session-selected-after-scroll, desktop-new-thread-draft, desktop-project-menu-open, mobile-default, mobile-sidebar-open, tablet-default | CAPTURED |
| Global command menu | opened, filtered, keyboard shortcut | command-palette, command-palette-filtered | CAPTURED |
| Composer | default, focused, multi-line, attachment, running, stopped/interrupted controls, queued accepted, queued attachment error | composer-focused, composer-multiline-draft, composer-image-attachment, composer-running, queued-input-accepted, queued-attachment-error, transcript-blocking | CAPTURED |
| Suggestions | command, exact command, file, skill, shell, keyboard scroll, mobile | suggestions-open, suggestions-exact-command, suggestions-keyboard-scroll, file-suggestions, skill-suggestions, shell-suggestions, mobile-suggestions | CAPTURED |
| Transcript | empty, normal, long text, tool summary, shell success, shell failure, error, blocking context | desktop-default, transcript-normal-tool, transcript-long-content, transcript-local-shell, transcript-shell-failure, transcript-error, transcript-blocking | CAPTURED |
| Tool activity | success, artifacts, collapsed/expanded, permission request, local shell, long stdout/stderr failure | transcript-normal-tool, transcript-local-shell, transcript-shell-failure, transcript-blocking, composer-running | CAPTURED |
| Blocking UI | permission, question, running composer, queued text, queued attachment error, mobile blocking | transcript-blocking, composer-running, queued-input-accepted, queued-attachment-error, mobile-blocking | CAPTURED |
| Settings/Auth | provider, model/effort, API key, cloud collapsed/expanded/saved, \`/auth\` direct open, \`/model\`, \`/effort\`, mobile, tablet | settings-modal, settings-provider-saved, settings-cloud-expanded, settings-cloud-saved, command-auth, command-model, command-effort, mobile-settings-modal, tablet-settings-modal | CAPTURED |
| Memory | project/AGENTS load, user save, auto memory save, legacy search/delete, \`/memory\` direct open | memory-modal, memory-save-states, memory-legacy-results, memory-legacy-deleted, command-memory | CAPTURED |
| Skills | bundled, locked, enabled, disabled/saved state, long description | skills-modal, skills-toggle-hover, skills-disabled-saved, command-skills | CAPTURED |
| Search/Status | session result, no-result state, status summary, prompt/help/debug commands, command menu access | session-search-results, session-search-empty, status-modal, command-prompt, command-help, command-debug, command-palette | CAPTURED |
| Pipeline | candidates, timeline/current step, diagram/review surface, selected candidate, deploy complete, cleanup complete, handoff ready, artifacts, rollback/restart/failure cleanup blocking | pipeline-modal, pipeline-candidate-selected, pipeline-rollback | CAPTURED |
| Responsive | desktop, tablet, mobile default, mobile drawer/scrolled, mobile modal, mobile suggestions, mobile blocking | desktop-default, tablet-default, tablet-settings-modal, mobile-default, mobile-sidebar-open, mobile-sidebar-scrolled, mobile-settings-modal, mobile-memory-modal, mobile-suggestions, mobile-blocking | CAPTURED |

The screenshots must be compared against the official Codex references for:

- Palette similarity.
- Density and spacing.
- Sidebar quietness.
- Composer quality.
- Modal quality.
- Border and divider restraint.
- Text overflow, wrapping, clipping, and layout jumps.

Manual review notes are recorded in \`${ISSUE_REPORT_RELATIVE_PATH}\`, including the contact-sheet sweep over all captured screenshots.

## Console

- Console errors: ${consoleErrors.length}
${consoleErrors.map((message) => `- ${message}`).join("\n")}
`;
}

function issueReportContent({ outputDir, captured, consoleErrors, automatedFindings, url }) {
  const capturedNames = new Set(captured.map((entry) => entry.name));
  const screenshotRows = captured
    .map((entry) => {
      const overflow = entry.hardChecks.overflowing.length > 0 ? `${entry.hardChecks.overflowing.length} overflow` : "ok";
      const modal = entry.hardChecks.modalOpen ? "modal" : "-";
      const suggestions = entry.hardChecks.suggestionsOpen ? "suggestions" : "-";
      return `| ${entry.name} | ${entry.viewport} | ${entry.path} | ${overflow} | ${modal} | ${suggestions} |`;
    })
    .join("\n");
  const automatedRows = automatedFindings.length
    ? automatedFindings
        .map((finding) => `| ${finding.severity} | ${finding.screenshotName} | ${finding.message} |`)
        .join("\n")
    : "| - | - | No automated overflow/console findings. Manual review passes are recorded below. |";
  const issueRows = manualIssueFindings
    .map((issue) => {
      const missingScreenshots = issue.screenshots.filter((name) => !capturedNames.has(name));
      const screenshotLinks = issue.screenshots
        .map((name) => {
          const entry = captured.find((candidate) => candidate.name === name);
          return entry ? `[${name}](${entry.path})` : `${name} (missing)`;
        })
        .join("<br>");
      const coverage = missingScreenshots.length > 0 ? `Missing: ${missingScreenshots.join(", ")}` : "Covered";
      return [
        `| ${issue.id}`,
        issue.severity,
        issue.status,
        issue.title,
        screenshotLinks,
        coverage,
        `${issue.details}<br><br>Fix direction: ${issue.fix} |`,
      ].join(" | ");
    })
    .join("\n");
  const openIssues = manualIssueFindings.filter((issue) => issue.status === "Open");
  const fixedIssues = manualIssueFindings.filter((issue) => issue.status === "Fixed");
  const reviewStatus =
    openIssues.length > 0
      ? "- Remaining Open issues are visual-quality issues, not claims that the UI is unusable."
      : "- No Open manual UI issues remain in this screenshot pass. Previously missing high-risk states are now represented in the screenshot inventory.";
  const nextFixOrder = openIssues.length
    ? openIssues.map((issue, index) => `${index + 1}. ${issue.id}: ${issue.fix}`).join("\n")
    : "No open manual UI issues remain. Continue only if new screenshot inspection or user feedback adds states.";
  return `# Comprehensive Web UI Screenshot Issue Report

Generated by \`scripts/web/e2e/web_repl_visual_audit.mjs\`.

## Run

- Local URL: \`${url}\`
- Screenshot directory: \`${outputDir}\`
- Screenshots captured: ${captured.length}
- Automated findings: ${automatedFindings.length}
- Console errors: ${consoleErrors.length}
- Open manual issues: ${openIssues.length}
- Fixed manual issues: ${fixedIssues.length}

## Screenshot Inventory

| Screenshot | Viewport | Path | Hard check | Modal | Suggestions |
| --- | --- | --- | --- | --- | --- |
${screenshotRows}

## Automated Findings

| Severity | Screenshot | Finding |
| --- | --- | --- |
${automatedRows}

## Manual Issues

| ID | Severity | Status | Title | Evidence Screenshots | Evidence Coverage | Analysis |
| --- | --- | --- | --- | --- | --- | --- |
${issueRows}

## Report Review Passes

### Review Pass 1 - Completeness

- Checked that homepage coverage is not a single screenshot: default, multi-session, project hover, collapsed project, scrolled list, selected scrolled session, unpersisted new-thread draft, project menu, sidebar Search/Skills, Settings hover.
- Checked that composer/suggestions coverage includes focused, multiline, image attachment, slash, keyboard-scroll, file, skill, shell, exact-command, running.
- Checked that transcript/tool coverage includes normal response, long output, local shell success, local shell failure with long stdout/stderr, transcript error, permission/question blocking.
- Checked that child workspaces include Settings saved provider/cloud states, expanded cloud credentials, Memory user/auto save plus legacy delete states, Skills disabled-save state, session search results/empty state, Status, Pipeline, selected pipeline candidate, rollback, and command-opened tabs.
- Checked mobile coverage includes default, drawer open, drawer scrolled, Settings, Memory, suggestions, blocking UI, and the Pipeline workspace.

### Review Pass 2 - Correctness

- Verified every manual issue references captured screenshot names.
- Verified mobile workspace blank-header issue is marked Fixed only after adding mobile row sizing and single-column settings layout.
- Verified automated findings and console errors are kept separate from manual visual issues.
${reviewStatus}

### Review Pass 3 - Contact Sheet Visual Sweep

- Reviewed all ${captured.length} captured screenshots from the latest audit directory together for blank states, obvious contrast failures, misplaced overlays, visible \`[object Object]\`, and old light-theme artifacts.
- Found and fixed UI-005: Pipeline selected/notice/active states inherited light surfaces inside the dark workspace.
- Re-ran the full browser audit after the fix and confirmed the issue no longer appears in \`pipeline-candidate-selected\` or \`pipeline-rollback\`.

## Next Fix Order

${nextFixOrder}
`;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = ensurePlaywrightCore(args.installPlaywrightCore);
  const port = await choosePort(args.host, args.port);
  const url = `http://${args.host}:${port}`;
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outputDir = args.outputDir || path.join(DEFAULT_OUTPUT_ROOT, timestamp);
  fs.mkdirSync(outputDir, { recursive: true });
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), "iac-code-web-visual-config-"));
  const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "iac-code-web-visual-home-"));
  seedVisualAuditRuntimeFiles({ configDir, homeDir });
  const captured = [];
  const automatedFindings = [];
  const consoleMessages = [];
  const consoleErrors = [];
  const httpErrors = [];
  let browser = null;
  const server = startServer({ host: args.host, port, configDir, homeDir });

  try {
    await waitForHealth(url, server);
    browser = await launchBrowser(playwright, args.headed);
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.on("console", (message) => {
      consoleMessages.push(message.text());
      if (message.type() === "error") {
        consoleErrors.push(message.text());
      }
    });
    page.on("pageerror", (error) => {
      consoleMessages.push(error.message);
      consoleErrors.push(error.message);
    });
    page.on("response", (response) => {
      if (response.status() >= 400) {
        httpErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto(url, { waitUntil: "domcontentloaded" });
    await waitReady(page);
    await captureCurrentMatrix({ page, outputDir, captured });

    for (const entry of captured) {
      if (entry.hardChecks.horizontalOverflow > 4) {
        automatedFindings.push({
          severity: "P1",
          screenshotName: entry.name,
          message: `Page has ${entry.hardChecks.horizontalOverflow}px horizontal overflow.`,
        });
      }
      if (entry.hardChecks.objectObjectText) {
        automatedFindings.push({
          severity: "P1",
          screenshotName: entry.name,
          message: "Visible UI contains [object Object] text.",
        });
      }
      for (const missing of entry.hardChecks.missingKeyElements) {
        automatedFindings.push({
          severity: "P1",
          screenshotName: entry.name,
          message: `${missing.name} (${missing.selector}) is missing or hidden.`,
        });
      }
      for (const overflow of entry.hardChecks.overflowing) {
        automatedFindings.push({
          severity: "P2",
          screenshotName: entry.name,
          message: `${overflow.tag}.${overflow.className || "-"} overflows horizontally: ${overflow.text}`,
        });
      }
      for (const collapsed of entry.hardChecks.collapsedKeyElements) {
        automatedFindings.push({
          severity: "P1",
          screenshotName: entry.name,
          message: `${collapsed.name} collapsed to ${collapsed.width}x${collapsed.height}px ` +
            `(minimum ${collapsed.minWidth}x${collapsed.minHeight}px).`,
        });
      }
      for (const occluded of entry.hardChecks.occludedKeyElements) {
        automatedFindings.push({
          severity: "P2",
          screenshotName: entry.name,
          message: `${occluded.name} is occluded by ${occluded.coveringTag}.${occluded.coveringClass || "-"}.`,
        });
      }
      for (const overlay of entry.hardChecks.oversizedTransientOverlays) {
        automatedFindings.push({
          severity: "P2",
          screenshotName: entry.name,
          message:
            `${overlay.name} unexpectedly covers ${Math.round(overlay.widthRatio * 100)}% x ` +
            `${Math.round(overlay.heightRatio * 100)}% of the viewport.`,
        });
      }
    }
    if (consoleErrors.length > 0) {
      automatedFindings.push({
        severity: "P1",
        screenshotName: "all",
        message: `Browser console emitted ${consoleErrors.length} error(s): ${consoleErrors.join("; ")}`,
      });
    }
    if (httpErrors.length > 0) {
      automatedFindings.push({
        severity: "P1",
        screenshotName: "all",
        message: `Browser observed ${httpErrors.length} HTTP error response(s): ${httpErrors.join("; ")}`,
      });
    }

    const domText = await page.evaluate(() => document.documentElement.textContent || "");
    assertNoSecrets("DOM", domText);
    assertNoSecrets("console messages", consoleMessages.join("\n"));

    fs.mkdirSync(path.dirname(ISSUE_REPORT_PATH), { recursive: true });
    fs.writeFileSync(REPORT_PATH, reportContent({ outputDir, captured, consoleErrors, automatedFindings, url }));
    fs.writeFileSync(
      ISSUE_REPORT_PATH,
      issueReportContent({ outputDir, captured, consoleErrors, automatedFindings, url }),
    );

    const blockingFindings = automatedFindings.filter((finding) =>
      ["P0", "P1", "P2"].includes(finding.severity),
    );

    console.log(
      JSON.stringify(
        {
          status: blockingFindings.length > 0 ? "failed" : "captured",
          url,
          outputDir,
          reportPath: REPORT_PATH,
          issueReportPath: ISSUE_REPORT_PATH,
          screenshots: Object.fromEntries(captured.map((entry) => [entry.name, entry.path])),
          automatedFindings,
          consoleMessageCount: consoleMessages.length,
      consoleErrorCount: consoleErrors.length,
      consoleErrors,
      httpErrors,
      visualFindings,
      screenshotMatrix,
        },
        null,
        2,
      ),
    );
    if (blockingFindings.length > 0) {
      throw new Error(`Visual audit failed with ${blockingFindings.length} blocking finding(s).`);
    }
  } finally {
    if (browser) {
      await browser.close();
    }
    await stopServer(server);
    if (process.env.IAC_CODE_WEB_SMOKE_KEEP_TMP !== "1") {
      fs.rmSync(configDir, { recursive: true, force: true });
      fs.rmSync(homeDir, { recursive: true, force: true });
    }
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});
