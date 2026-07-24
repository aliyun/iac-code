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
const DESKTOP_SCREENSHOT = path.join(os.tmpdir(), "iac-code-web-e2e-desktop.png");
const MOBILE_SCREENSHOT = path.join(os.tmpdir(), "iac-code-web-e2e-mobile.png");
const FAKE_SECRET_STRINGS = ["sk-test-secret", "ALIYUN_SECRET", "SECRET_ACCESS_KEY"];

function parseArgs(argv) {
  const args = {
    host: "127.0.0.1",
    port: 8767,
    headed: false,
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

function startServer({ host, port, configDir }) {
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
      env: scrubbedChildEnv({ configDir, repoRoot: REPO_ROOT }),
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
  return page.locator('[data-field="session-id"]').textContent();
}

async function fullDomText(page) {
  return page.evaluate(() => document.documentElement.textContent || "");
}

function assertNoSecrets(name, value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  const leaked = FAKE_SECRET_STRINGS.filter((secret) => text.includes(secret));
  if (leaked.length > 0) {
    throw new Error(`${name} leaked fake secret strings: ${leaked.join(", ")}`);
  }
}

async function expectText(page, text, timeout = 15000) {
  await page.waitForFunction(
    (expected) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (!String(node.nodeValue || "").includes(expected)) {
          continue;
        }
        const element = node.parentElement;
        const style = element ? window.getComputedStyle(element) : null;
        if (
          element &&
          element.getClientRects().length > 0 &&
          style?.display !== "none" &&
          style?.visibility !== "hidden"
        ) {
          return true;
        }
      }
      return false;
    },
    text,
    { timeout },
  );
}

async function clickWorkspaceTab(page, tabName) {
  await page.locator(`[data-workspace-tab="${tabName.toLowerCase()}"]`).click();
  await page.locator(`[data-workspace-panel="${tabName.toLowerCase()}"]`).waitFor({ state: "visible", timeout: 10000 });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = ensurePlaywrightCore(args.installPlaywrightCore);
  const port = await choosePort(args.host, args.port);
  const url = `http://${args.host}:${port}`;
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), "iac-code-web-e2e-config-"));
  const checks = [];
  const consoleMessages = [];
  const consoleErrors = [];
  let browser = null;
  let pipelineSessionId = "";
  const server = startServer({ host: args.host, port, configDir });

  async function check(name, fn) {
    await fn();
    checks.push(name);
  }

  try {
    await waitForHealth(url, server);
    browser = await launchBrowser(playwright, args.headed);
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
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

    await check("app loads in an unpersisted new-session draft", async () => {
      await page.goto(url, { waitUntil: "domcontentloaded" });
      await waitReady(page);
      await page.locator('[data-app-shell="draft-session-controls"]').waitFor({ state: "visible", timeout: 10000 });
      if ((await page.locator(".session-item.is-active").count()) !== 0) {
        throw new Error("new-session draft was persisted before the first message");
      }
    });

    const composer = page.locator('[data-app-shell="composer-input"]');
    await check("composer Shift+Enter inserts newline and Enter submits", async () => {
      await composer.fill("Smoke line 1");
      await composer.press("Shift+Enter");
      await composer.type("Smoke line 2");
      const draft = await composer.inputValue();
      if (!draft.includes("\n")) {
        throw new Error("Shift+Enter did not insert a newline");
      }
      await composer.press("Enter");
      await expectText(page, "Smoke line 2");
      await expectText(page, "normal assistant response from fake runtime");
      await page.locator(".session-item.is-active").waitFor({ state: "visible", timeout: 10000 });
      const sessionId = await activeSessionId(page);
      if (!sessionId || sessionId.trim() === "-") {
        throw new Error("first submit did not materialize an active session");
      }
    });

    await check("assistant tool card renders", async () => {
      await expectText(page, "fakeRosPlan");
      await expectText(page, "fake tool completed");
    });

    await check("local shell card renders without executing shell", async () => {
      await composer.fill("!echo fake local shell");
      await composer.press("Enter");
      const shellCard = page.locator(".tool-card", { hasText: "已运行 echo fake local shell" }).first();
      await shellCard.waitFor({ state: "visible", timeout: 15000 });
      await shellCard.locator("summary").click();
      await expectText(page, "fake local shell stdout");
    });

    await check("pending permission/question and queued draft behavior work", async () => {
      await composer.fill("e2e blocking turn");
      await composer.press("Enter");
      await expectText(page, "Allow fake action");

      await composer.fill("queued follow-up");
      await composer.press("Enter");
      await page.waitForFunction(
        () => document.querySelector('[data-app-shell="composer-input"]')?.value === "",
        null,
        { timeout: 10000 },
      );

      await composer.fill("/status");
      await composer.press("Escape");
      await composer.press("Enter");
      await page.waitForFunction(
        () => document.querySelector('[data-app-shell="composer-input"]')?.value === "/status",
        null,
        { timeout: 10000 },
      );

      await page.locator('.blocking-option-row[data-choice-id="allow_once"]').click();
      await expectText(page, "Choose deployment region");
      await page.getByRole("button", { name: "Use cn-hangzhou" }).click();
      await expectText(page, "question answered: cn-hangzhou");
    });

    await check("settings tabs render", async () => {
      await page.locator('[data-app-shell="workspace-open-config"]').click();
      await page.locator('[data-app-shell="workspace-modal"]').waitFor({ state: "visible", timeout: 10000 });
      for (const tabName of ["other", "model", "cloud", "memory", "skills", "archived"]) {
        await clickWorkspaceTab(page, tabName);
      }
      await page.locator('[data-app-shell="workspace-modal-close"]').click();
    });

    await check("pipeline mode can be selected without persisting an empty session", async () => {
      await page.locator('[data-app-shell="new-session"]').click();
      await page.locator('[data-app-shell="draft-mode-control"]').click();
      await page.locator('[data-app-shell="draft-mode-menu"] .draft-session-menu-item', { hasText: "流水线模式" }).click();
      await page
        .locator('[data-app-shell="draft-pipeline-menu"] .draft-session-menu-item', { hasText: "售卖流水线" })
        .click();
      await page.locator('[data-app-shell="draft-mode-control"]', { hasText: "售卖流水线" }).waitFor({ timeout: 10000 });
      if ((await page.locator(".session-item.is-active").count()) !== 0) {
        throw new Error("selecting a pipeline persisted an empty session before submit");
      }
    });

    await check("pipeline candidate selection state renders", async () => {
      await composer.fill("Provision a smoke-test selling VPC");
      await composer.press("Enter");
      const activePipelineSession = page.locator(".session-item.is-active");
      await activePipelineSession.waitFor({ state: "visible", timeout: 10000 });
      pipelineSessionId = (await activePipelineSession.getAttribute("data-session-id"))?.trim() || "";
      if (!pipelineSessionId) {
        throw new Error("pipeline submit did not materialize a session");
      }
      await expectText(page, "Smoke balanced VPC");
      await expectText(page, "Smoke minimal VPC");
      await expectText(page, "Choose the selling candidate to deploy");
      await expectText(page, "graph TD");
      const pipelinePanel = page.locator('[data-workspace-panel="pipeline"]');
      try {
        await pipelinePanel.waitFor({ state: "visible", timeout: 10000 });
      } catch (error) {
        const diagnostics = await page.evaluate(() => ({
          mode: document.querySelector('[data-field="mode"]')?.textContent?.trim() || "",
          modalHidden: document.querySelector('[data-app-shell="workspace-modal"]')?.hidden,
          panelHidden: document.querySelector('[data-workspace-panel="pipeline"]')?.hidden,
          activePanel: document.querySelector('[data-workspace-panel]:not([hidden])')?.getAttribute("data-workspace-panel") || "",
        }));
        throw new Error(`pipeline selection workspace did not reopen: ${JSON.stringify(diagnostics)}; ${error}`);
      }
    });

    await check("pipeline interrupt accepts the frozen model selection", async () => {
      const result = await page.evaluate(async (sessionId) => {
        const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/interrupt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: "Keep the selected pipeline candidate" }),
        });
        return { status: response.status, payload: await response.json() };
      }, pipelineSessionId);
      if (result.status !== 202 || result.payload?.accepted !== true) {
        throw new Error(`pipeline interrupt failed: ${JSON.stringify(result)}`);
      }
    });

    await check("candidate override, deploy success, cleanup, and handoff render", async () => {
      const candidate = page.locator(".pipeline-candidate", { hasText: "Smoke balanced VPC" }).first();
      await candidate.locator(".pipeline-candidate-overrides-panel > summary").click();
      await candidate.locator(".pipeline-candidate-overrides").fill('{"InstanceType":"ecs.g7.large"}');
      await candidate.getByRole("button", { name: "Select candidate" }).click();
      await expectText(page, "accepted · candidate_selected");
      await expectText(page, "CREATE_IN_PROGRESS");
      await expectText(page, "CREATE_COMPLETE");
      await expectText(page, "cleanup completed");
      await expectText(page, "handoff normal ready");
    });

    await check("reload recovers pipeline and handoff state", async () => {
      await page.reload({ waitUntil: "domcontentloaded" });
      await waitReady(page);
      const pipelineButton = page.locator(`.session-item[data-session-id="${pipelineSessionId}"]`);
      await pipelineButton.waitFor({ state: "visible", timeout: 10000 });
      await pipelineButton.click();
      await expectText(page, "CREATE_COMPLETE");
      await expectText(page, "cleanup completed");
      await expectText(page, "handoff normal ready");
    });

    await check("DOM and console do not leak fake secrets", async () => {
      assertNoSecrets("DOM", await fullDomText(page));
      assertNoSecrets("console messages", consoleMessages.join("\n"));
      if (consoleErrors.length > 0) {
        throw new Error(`browser console errors were emitted:\n${consoleErrors.join("\n")}`);
      }
      const sessionId = await activeSessionId(page);
      const statusPayload = await page.evaluate(async (id) => {
        const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/status`);
        return response.json();
      }, sessionId.trim());
      assertNoSecrets("session status", statusPayload);
    });

    await check("desktop and mobile screenshots saved", async () => {
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.screenshot({ path: DESKTOP_SCREENSHOT, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: MOBILE_SCREENSHOT, fullPage: true });
      for (const screenshot of [DESKTOP_SCREENSHOT, MOBILE_SCREENSHOT]) {
        const stat = fs.statSync(screenshot);
        if (stat.size < 1024) {
          throw new Error(`${screenshot} looks too small to be useful`);
        }
      }
    });

    console.log(
      JSON.stringify(
        {
          status: "passed",
          url,
          checks,
          screenshots: {
            desktop: DESKTOP_SCREENSHOT,
            mobile: MOBILE_SCREENSHOT,
          },
          configDir,
          configDirRemoved: process.env.IAC_CODE_WEB_SMOKE_KEEP_TMP !== "1",
          consoleMessageCount: consoleMessages.length,
          consoleErrorCount: consoleErrors.length,
          consoleMessages,
          consoleErrors,
        },
        null,
        2,
      ),
    );
  } finally {
    if (browser) {
      await browser.close();
    }
    await stopServer(server);
    if (process.env.IAC_CODE_WEB_SMOKE_KEEP_TMP !== "1") {
      fs.rmSync(configDir, { recursive: true, force: true });
    }
  }
}

main().catch(async (error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});
