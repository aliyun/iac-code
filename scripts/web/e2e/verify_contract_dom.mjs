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
  const values = {
    url: "",
    sessionId: "",
    expectedText: "",
    screenshot: "",
    domSnapshot: "",
    audit: "",
    requireQuote: "false",
    expandPipelineHistory: "false",
  };
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
    const defaultBodyText = await page.locator("body").innerText();
    let historyExpanded = false;
    if (args.expandPipelineHistory === "true") {
      const pipelineGroups = page.locator("details.pipeline-transcript-group");
      await pipelineGroups.first().waitFor({ state: "attached", timeout: 20000 });
      await pipelineGroups.evaluateAll((groups) => {
        for (const group of groups) group.open = true;
      });
      historyExpanded = (await pipelineGroups.count()) > 0;
    }
    if (args.requireQuote === "true") {
      await page.waitForFunction(
        () => {
          const text = document.body.innerText;
          const quoteVisible =
            text.includes("询价") ||
            text.includes("费用") ||
            text.includes("价格") ||
            text.includes("¥") ||
            text.toLowerCase().includes("price");
          return !text.includes("正在载入会话") && quoteVisible;
        },
        undefined,
        { timeout: 60000 },
      );
    }
    const bodyText = await page.locator("body").innerText();
    if (bodyText.includes("aliyun_http") || bodyText.includes("e2e-internal-header-value")) {
      throw new Error("browser DOM leaked internal Aliyun metadata");
    }
    if (args.domSnapshot) {
      fs.mkdirSync(path.dirname(args.domSnapshot), { recursive: true });
      fs.writeFileSync(args.domSnapshot, bodyText, "utf8");
    }
    await page.screenshot({ path: args.screenshot, fullPage: true });
    const audit = {
      passed: true,
      screenshot: args.screenshot,
      expectedTextVisible: bodyText.includes(args.expectedText),
      solutionVisible: bodyText.includes("方案") || bodyText.toLowerCase().includes("solution"),
      quoteVisible:
        bodyText.includes("询价") ||
        bodyText.includes("费用") ||
        bodyText.includes("价格") ||
        bodyText.includes("¥") ||
        bodyText.toLowerCase().includes("price"),
      historyExpanded,
      // W02 deliberately expands completed steps to prove that refresh preserved
      // their solution/quote history. Privacy checks remain scoped to the default
      // collapsed view that users see immediately after opening the session.
      previewSuccessHidden: !defaultBodyText.includes("PreviewStack 成功"),
      internalTemplatePathHidden: !/templates[/\\][^\s]+\.(?:ya?ml|json|tf)/i.test(defaultBodyText),
      internalParameterJsonHidden:
        !defaultBodyText.includes('"parameter_overrides"') && !defaultBodyText.includes('"parameterOverrides"'),
    };
    if (args.audit) {
      fs.mkdirSync(path.dirname(args.audit), { recursive: true });
      fs.writeFileSync(args.audit, JSON.stringify(audit, null, 2) + "\n", "utf8");
    }
    process.stdout.write(JSON.stringify(audit) + "\n");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
