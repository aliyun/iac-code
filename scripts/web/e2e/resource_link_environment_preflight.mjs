#!/usr/bin/env node
/** Verify the dedicated Chrome, domestic console identity, and iac-code Web draft readiness. */

import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

function loadChromium() {
  const modules = process.env.IAC_RESOURCE_TEST_NODE_MODULES;
  const candidates = [
    modules ? path.join(modules, "playwright-core") : null,
    "playwright-core",
    "/Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/playwright-core",
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      return require(candidate).chromium;
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(`playwright-core is unavailable: ${failures.join("; ")}`);
}

function parseArgs(argv) {
  const result = { cdpEndpoint: "http://127.0.0.1:9225", timeoutMs: 30000 };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (name === "--help") result.help = true;
    else if (name === "--self-test") result.selfTest = true;
    else if (name === "--console-only") result.consoleOnly = true;
    else if (name === "--cdp-endpoint") result.cdpEndpoint = argv[++index];
    else if (name === "--app-origin") result.appOrigin = argv[++index];
    else if (name === "--output") result.output = argv[++index];
    else if (name === "--timeout-ms") result.timeoutMs = Number(argv[++index]);
    else throw new Error(`unknown argument: ${name}`);
  }
  if (result.help || result.selfTest) return result;
  if ((!result.consoleOnly && !result.appOrigin) || !result.output) {
    throw new Error("--app-origin (unless --console-only) and --output are required");
  }
  if (!Number.isInteger(result.timeoutMs) || result.timeoutMs < 1000 || result.timeoutMs > 120000) {
    throw new Error("--timeout-ms must be an integer between 1000 and 120000");
  }
  if (result.appOrigin) {
    const origin = new URL(result.appOrigin);
    if (origin.protocol !== "http:" && origin.protocol !== "https:") throw new Error("app origin must use HTTP(S)");
    result.appOrigin = origin.origin;
  }
  return result;
}

function usage() {
  return `Usage:
  cat private-input.json | node resource_link_environment_preflight.mjs \\
    --cdp-endpoint http://127.0.0.1:9225 \\
    --app-origin http://host:8767 \\
    --output /tmp/environment-preflight.json

--console-only only observes the existing domestic console and saves a screenshot;
it neither creates a tab nor touches a Web session and requires no accessToken.
stdin JSON fields: accessToken and expectedIdentityCandidates. Sensitive stdin values are never written to output.`;
}

function sha256(value) {
  return createHash("sha256").update(String(value), "utf8").digest("hex");
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true, mode: 0o700 });
  const temporary = `${file}.tmp.${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  fs.renameSync(temporary, file);
}

async function readPrivateInput(consoleOnly = false) {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const value = JSON.parse(input);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("stdin must be a JSON object");
  if (!consoleOnly && (typeof value.accessToken !== "string" || value.accessToken.length < 32)) {
    throw new Error("stdin accessToken is missing or malformed");
  }
  if (!Array.isArray(value.expectedIdentityCandidates) || !value.expectedIdentityCandidates.length ||
      value.expectedIdentityCandidates.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error("stdin expectedIdentityCandidates must be a non-empty string array");
  }
  return {
    accessToken: value.accessToken,
    expectedIdentityCandidates: [...new Set(value.expectedIdentityCandidates.map((item) => item.trim()))],
  };
}

async function consoleObservation(context, expectedIdentityCandidates, timeoutMs, screenshot) {
  const pages = context.pages().filter((page) => {
    try {
      const host = new URL(page.url()).hostname;
      return host === "home.console.aliyun.com" || host.endsWith(".console.aliyun.com");
    } catch (_error) {
      return false;
    }
  });
  if (!pages.length) throw new Error("dedicated Chrome has no domestic Alibaba Cloud console page");
  const page = pages.find((candidate) => new URL(candidate.url()).hostname === "home.console.aliyun.com") || pages[0];
  await page.waitForLoadState("domcontentloaded", { timeout: timeoutMs }).catch(() => {});
  const finalUrl = page.url();
  const title = await page.title();
  const visibleText = await page.locator("body").innerText({ timeout: timeoutMs }).catch(() => "");
  const attributes = await page.evaluate(() => Array.from(document.querySelectorAll("[title],[aria-label]"))
    .flatMap((element) => [element.getAttribute("title"), element.getAttribute("aria-label")])
    .filter(Boolean)
    .join("\n"));
  const searchable = `${visibleText}\n${attributes}`.toLowerCase();
  const matched = expectedIdentityCandidates.some((candidate) => searchable.includes(candidate.toLowerCase()));
  const loginPage = /(^|\.)account\.aliyun\.com$/i.test(new URL(finalUrl).hostname) ||
    /login\/login\.htm/i.test(finalUrl);
  const loginSignals = /(扫码登录|账号登录|手机号登录|ram用户登录|sign in to alibaba cloud)/i.test(visibleText);
  if (loginPage || loginSignals) throw new Error("Alibaba Cloud console is not logged in");
  if (!matched) throw new Error("logged-in console does not expose the expected resource-link-test identity");
  if (screenshot) {
    fs.mkdirSync(path.dirname(path.resolve(screenshot)), { recursive: true, mode: 0o700 });
    await page.bringToFront();
    await page.screenshot({ path: screenshot, fullPage: false });
    fs.chmodSync(screenshot, 0o600);
  }
  return {
    site: "aliyun",
    loggedIn: true,
    expectedIdentityMatched: true,
    expectedIdentityDigest: sha256(JSON.stringify([...expectedIdentityCandidates].sort())),
    finalUrl: new URL(finalUrl).origin + new URL(finalUrl).pathname,
    title,
    ...(screenshot ? { screenshot: path.resolve(screenshot) } : {}),
  };
}

async function webObservation(context, appOrigin, accessToken, timeoutMs) {
  const page = await context.newPage();
  await page.goto(appOrigin, { waitUntil: "domcontentloaded", timeout: timeoutMs });
  await page.locator("#iac-code-web-root").waitFor({ state: "attached", timeout: timeoutMs });
  const gate = page.locator(".token-access-gate");
  let tokenGatePassed = false;
  if (await gate.count()) {
    await page.locator(".token-access-input").fill(accessToken);
    await page.locator(".token-access-submit").click();
    await gate.waitFor({ state: "detached", timeout: timeoutMs });
    tokenGatePassed = true;
  }
  const newChat = page.locator('[data-app-shell="new-session"]');
  await newChat.waitFor({ state: "visible", timeout: timeoutMs });
  await newChat.click();
  const prompt = page.locator("#prompt");
  await prompt.waitFor({ state: "visible", timeout: timeoutMs });
  if (!await prompt.isEditable()) throw new Error("iac-code prompt is not editable after opening a new draft");
  const materializedSessions = await page.locator(".session-item.is-active[data-session-id]").count();
  if (materializedSessions) throw new Error("environment preflight unexpectedly selected an existing materialized session");
  return {
    origin: appOrigin,
    finalUrl: page.url(),
    tokenGatePassed,
    appReady: true,
    newChatReady: true,
    sessionState: "draft-ready",
    sessionMaterialized: false,
    sessionId: null,
    note: "The first real task prompt will materialize the session; no synthetic prompt was sent by preflight.",
  };
}

function selfTest() {
  const parsed = parseArgs([
    "--cdp-endpoint", "http://127.0.0.1:9225",
    "--app-origin", "http://30.0.0.1:8767/",
    "--output", "/tmp/result.json",
  ]);
  if (parsed.appOrigin !== "http://30.0.0.1:8767" || sha256("x").length !== 64) {
    throw new Error("environment preflight self-test failed");
  }
  const consoleOnly = parseArgs(["--console-only", "--output", "/tmp/console-only.json"]);
  if (!consoleOnly.consoleOnly || consoleOnly.appOrigin) throw new Error("console-only must not require a Web page");
  process.stdout.write(`${JSON.stringify({ status: "passed", checks: 3 })}\n`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) { process.stdout.write(`${usage()}\n`); return; }
  if (args.selfTest) { selfTest(); return; }
  const privateInput = await readPrivateInput(args.consoleOnly);
  const chromium = loadChromium();
  const browser = await chromium.connectOverCDP(args.cdpEndpoint);
  const contexts = browser.contexts();
  if (contexts.length !== 1) throw new Error(`expected one dedicated Chrome context, found ${contexts.length}`);
  const version = await browser.version();
  const cloudConsole = await consoleObservation(
    contexts[0], privateInput.expectedIdentityCandidates, args.timeoutMs,
    args.consoleOnly ? `${args.output}.png` : null,
  );
  const web = args.consoleOnly ? null : await webObservation(
    contexts[0], args.appOrigin, privateInput.accessToken, args.timeoutMs,
  );
  writeJson(args.output, {
    schemaVersion: 1,
    capturedAt: new Date().toISOString(),
    status: "passed",
    purpose: "environment-readiness-only",
    browser: {
      engine: "Chrome",
      version,
      cdpEndpoint: args.cdpEndpoint,
      accessMode: "dedicated-chrome-profile",
      contextCount: contexts.length,
    },
    cloudConsole,
    web,
    formalResourcePreflightRequired: true,
  });
  process.stdout.write(`${JSON.stringify({ status: "passed", output: path.resolve(args.output) })}\n`, () => process.exit(0));
  // This is an attachment to an already running Chrome. Let process exit close
  // only the CDP socket; Browser.close() would terminate the dedicated browser.
}

main().catch((error) => {
  process.stderr.write(`resource-link environment preflight failed: ${error.message}\n`);
  process.exitCode = 1;
});
