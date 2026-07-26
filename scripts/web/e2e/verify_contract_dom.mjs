#!/usr/bin/env node
import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const require = createRequire(import.meta.url);
const TMP_NODE_ROOTS = [
  path.join(os.tmpdir(), "iac-code-web-smoke-node"),
  path.join("/tmp", "iac-code-web-smoke-node"),
];

function playwrightCore() {
  try {
    return require("playwright-core");
  } catch (_error) {
    const installed = TMP_NODE_ROOTS.find((root) =>
      fs.existsSync(path.join(root, "node_modules", "playwright-core")),
    );
    if (!installed) throw _error;
    return require(path.join(installed, "node_modules", "playwright-core"));
  }
}

function parseArgs(argv) {
  const values = { url: "", sessionId: "", expectedText: "", screenshot: "" };
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]?.replace(/^--/, "");
    if (key in values) values[key] = argv[index + 1] || "";
  }
  if (!values.url || !values.sessionId || !values.expectedText || !values.screenshot) {
    throw new Error("--url, --sessionId, --expectedText and --screenshot are required");
  }
  return values;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const playwright = playwrightCore();
  const executablePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await playwright.chromium.launch({
    headless: true,
    executablePath: fs.existsSync(executablePath) ? executablePath : undefined,
    channel: fs.existsSync(executablePath) ? undefined : "chrome",
    args: ["--no-first-run", "--no-default-browser-check"],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.goto(args.url, { waitUntil: "networkidle" });
    await page.waitForSelector('#iac-code-web-root[data-ready="true"]', { timeout: 20000 });
    const row = page.locator(`[data-session-id="${args.sessionId}"]`).first();
    await row.waitFor({ state: "visible", timeout: 20000 });
    await row.click();
    await page.waitForFunction(
      (expected) => document.body.innerText.includes(expected),
      args.expectedText,
      { timeout: 20000 },
    );
    const bodyText = await page.locator("body").innerText();
    if (bodyText.includes("aliyun_http") || bodyText.includes("e2e-internal-header-value")) {
      throw new Error("browser DOM leaked internal Aliyun metadata");
    }
    await page.screenshot({ path: args.screenshot, fullPage: true });
    process.stdout.write(JSON.stringify({ passed: true, screenshot: args.screenshot }) + "\n");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
