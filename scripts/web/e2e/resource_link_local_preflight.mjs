#!/usr/bin/env node
/** Read-only local dispatch checks. This never creates a Web session or clicks a resource. */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const origin = "http://127.0.0.1:8767";
const cdpEndpoint = "http://127.0.0.1:9225";
const require = createRequire(import.meta.url);

export function checkPrivateFile(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || (stat.mode & 0o777) !== 0o600 || stat.size === 0) {
    throw new Error("private token file must be a nonempty regular file with mode 0600");
  }
}

export function matchingChromeProcess(lines, profile) {
  const escaped = profile.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return lines.some((line) => line.includes("Google Chrome.app/Contents/MacOS/Google Chrome")
    && /(?:^|\s)--remote-debugging-port=9225(?:\s|$)/.test(line)
    && new RegExp(`(?:^|\\s)--user-data-dir=${escaped}(?:\\s|$)`).test(line));
}

function chromium() {
  const candidates = [
    process.env.IAC_RESOURCE_TEST_NODE_MODULES
      ? path.join(process.env.IAC_RESOURCE_TEST_NODE_MODULES, "playwright-core") : null,
    "playwright-core",
    "/Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/playwright-core",
    path.join(os.homedir(), "Applications/ChatGPT Bailian.app/Contents/Resources/cua_node/lib/node_modules/playwright-core"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate).chromium; } catch { /* try installed runtime */ }
  }
  throw new Error("playwright-core unavailable; supply an installed IAC_RESOURCE_TEST_NODE_MODULES");
}

export async function main(argv) {
  const args = { inputs: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--input") args.inputs.push(argv[++i]);
    else if (argv[i] === "--packet") args.packet = argv[++i];
    else if (argv[i] === "--token-file") args.tokenFile = argv[++i];
    else if (argv[i] === "--help") {
      console.log("resource_link_local_preflight.mjs [--packet <directory>] [--input <official-input>] [--token-file <0600-file>]");
      return;
    } else throw new Error(`unknown argument: ${argv[i]}`);
  }
  const result = {
    scope: "environment_only", workerBackend: "bailian-local", appOrigin: origin, cdpEndpoint,
    ready: false, checks: {}, errors: [], inputs: [],
  };
  const check = async (name, action) => {
    try { await action(); result.checks[name] = true; }
    catch (error) { result.checks[name] = false; result.errors.push({ check: name, reason: error.message }); }
  };
  await check("controller", () => {
    const state = JSON.parse(execFileSync("codex-bailian-control", ["--json", "status"], {
      encoding: "utf8", timeout: 15000,
    }));
    if (state.mode !== "shared" || !state.ready || !state.connected) {
      throw new Error("Bailian App shared server is not ready/connected; inspect codex-bailian-control status");
    }
  });
  await check("privateToken", () => checkPrivateFile(
    args.tokenFile || path.join(os.homedir(), ".iac-code/web-e2e-access.token"),
  ));
  await check("webHttp", async () => {
    const response = await fetch(`${origin}/`, { signal: AbortSignal.timeout(8000), redirect: "error" });
    if (response.status !== 200) throw new Error(`formal Web returned HTTP ${response.status}`);
    await response.body?.cancel();
  });
  await check("runnerExplicitOrigin", () => {
    try {
      execFileSync(process.execPath, [path.join(root, "scripts/web/e2e/resource_link_chrome_batch.mjs"),
        "--plan", "unused.json", "--output", "unused.json", "--artifacts-dir", "unused"], {
        encoding: "utf8", timeout: 10000, stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      if (String(error.stderr).includes("--app-origin is required")) return;
      throw new Error("runner did not reject a missing explicit --app-origin");
    }
    throw new Error("runner accepted missing --app-origin");
  });
  await check("chromeProfile", () => {
    const lines = execFileSync("ps", ["-axo", "args="], { encoding: "utf8", timeout: 10000 }).split("\n");
    if (!matchingChromeProcess(lines, path.join(root, "evidence/formal/chrome-profile"))) {
      throw new Error("Chrome9225 is not running with the dedicated local formal profile");
    }
  });
  await check("formalWebUnlocked", async () => {
    const browser = await chromium().connectOverCDP(cdpEndpoint, { timeout: 8000 });
    const pages = browser.contexts().flatMap((context) => context.pages()).filter((page) => {
      try { return new URL(page.url()).origin === origin; } catch { return false; }
    });
    if (pages.length !== 1) throw new Error(`expected one formal Web tab, found ${pages.length}`);
    const page = pages[0];
    if (await page.locator(".token-access-gate").isVisible()) throw new Error("formal Web token gate is visible");
    if (!await page.locator('[data-app-shell="new-session"]').isVisible()) {
      throw new Error("formal Web new-session UI is not visible");
    }
  });
  if (args.packet || args.inputs.length) await check("packetInputs", () => {
    const inputs = [...args.inputs];
    if (args.packet) inputs.unshift(path.join(args.packet, "TASK.md"),
      path.join(args.packet, "RESOURCE_LINK_BATCH_REPORT_TEMPLATE.md"));
    for (const file of inputs) {
      if (!file || !fs.statSync(file).isFile()) throw new Error(`missing packet input: ${file}`);
      const bytes = fs.readFileSync(file);
      if (!bytes.length) throw new Error(`empty packet input: ${file}`);
      result.inputs.push({ path: path.resolve(file), sha256: createHash("sha256").update(bytes).digest("hex") });
    }
  });
  result.ready = result.errors.length === 0;
  console.log(JSON.stringify(result, null, 2));
  if (!result.ready) process.exitCode = 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // Process exit closes this client's CDP socket; Browser.close() would terminate Chrome.
  main(process.argv.slice(2)).then(() => process.exit(process.exitCode || 0)).catch((error) => {
    console.error(error.message); process.exit(2);
  });
}
