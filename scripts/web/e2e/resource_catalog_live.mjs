#!/usr/bin/env node
// Opt-in browser E2E over the real ROS samples collected by resource_catalog_live.py.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  CONSOLE_ACCESS_ERRORS,
  CONSOLE_NOT_FOUND_ERRORS,
  locationContainsIdentifier,
  textContainsIdentifier,
} from "./resource_catalog_live_helpers.mjs";

const require = createRequire(import.meta.url);
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const staticRoot = path.join(repo, "src/iac_code/web/static");

function argument(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const inputArgument = argument("--input");
const outputArgument = argument("--output");
assert.ok(inputArgument, "--input is required");
assert.ok(outputArgument, "--output is required");
const inputPath = path.resolve(inputArgument);
const outputPath = path.resolve(outputArgument);
assert.ok(fs.existsSync(inputPath), "--input must name a live catalog report");
const browserExecutable = argument(
  "--browser",
  process.env.IAC_RESOURCE_TEST_BROWSER || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
);
const modules = process.env.IAC_RESOURCE_TEST_NODE_MODULES;
const { chromium } = require(modules ? path.join(modules, "playwright-core") : "playwright-core");
const report = JSON.parse(fs.readFileSync(inputPath, "utf8"));
assert.equal(report.schemaVersion, 2);

// Load the same validated identity payload injected by the real Web app.  The
// generated direct-API catalog deliberately does not own hand-reviewed route
// attributes, so rebuilding this payload from its JSON would silently discard
// fields such as RAM GroupName and NAS AccessGroupType.
const catalogProcess = spawnSync(
  "uv",
  [
    "run", "python", "-c",
    "import json; from iac_code.web.resource_link_catalog import resource_link_catalog_payload; " +
      "print(json.dumps(resource_link_catalog_payload(), separators=(',', ':')))",
  ],
  { cwd: repo, encoding: "utf8", timeout: 30000 },
);
assert.equal(catalogProcess.status, 0, `unable to load production resource catalog: ${catalogProcess.stderr}`);
const catalogPayload = JSON.parse(catalogProcess.stdout);

const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://localhost");
  if (url.pathname === "/") {
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.end(
      "<!doctype html><html><head><script>globalThis.__IAC_RESOURCE_LINKS__=" +
        JSON.stringify(catalogPayload).replaceAll("<", "\\u003c") +
        '</script></head><body><main id="fixture"></main><script src="/static/js/vendor/markdown-it.min.js"></script></body></html>',
    );
    return;
  }
  const file = path.resolve(staticRoot, `.${url.pathname.replace(/^\/static/, "")}`);
  if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) {
    response.writeHead(404);
    response.end();
    return;
  }
  response.setHeader("Content-Type", file.endsWith(".css") ? "text/css" : "text/javascript");
  response.end(fs.readFileSync(file));
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;

function safeTypeFailure(value) {
  return {
    source: value.source,
    resourceType: value.resourceType,
    reason: value.reason,
    ...(Array.isArray(value.missing) && value.missing.length ? { missing: value.missing } : {}),
    ...(Array.isArray(value.matchingWrappers) ? { matchingWrappers: value.matchingWrappers } : {}),
    ...(Number.isInteger(value.anchorCount) ? { anchorCount: value.anchorCount } : {}),
    ...(Array.isArray(value.occurrenceTypes) ? { occurrenceTypes: value.occurrenceTypes } : {}),
  };
}

async function consolePageText(page) {
  const values = [];
  for (const frame of page.frames()) {
    try {
      values.push(await frame.locator("body").innerText({ timeout: 2000 }));
    } catch (_error) {
      // A console can detach a frame while its SPA changes routes.
    }
  }
  return values.join("\n");
}

async function assertConsoleResourceLoaded(page, item) {
  const deadline = Date.now() + 20000;
  const exactRouteReadyAt = Date.now() + 5000;
  let text = "";
  while (Date.now() < deadline) {
    text = await consolePageText(page);
    if (CONSOLE_NOT_FOUND_ERRORS.some((pattern) => pattern.test(text)))
      throw Error("console_resource_not_found");
    if (CONSOLE_ACCESS_ERRORS.some((pattern) => pattern.test(text))) throw Error("console_access_denied");
    if (textContainsIdentifier(text, item.identifier) ||
        (locationContainsIdentifier(page.url(), item.identifier) && Date.now() >= exactRouteReadyAt)) return;
    await page.waitForTimeout(750);
  }
  throw Error("resource_identifier_not_rendered");
}

async function signinDestination(appOrigin, sessionId, partition, destination) {
  const response = await fetch(
    `${appOrigin}/api/sessions/${encodeURIComponent(sessionId)}/resource-references/signin`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        accountScope: "live-catalog-account",
        preferredPartition: partition,
        destinations: { [partition]: destination },
      }),
      signal: AbortSignal.timeout(30000),
    },
  );
  if (!response.ok) {
    let code = `http_${response.status}`;
    try {
      const payload = await response.json();
      code = payload.code || payload.error || code;
    } catch (_error) {
      // The public status is enough; never include an HTML error body.
    }
    throw Error(String(code));
  }
  const payload = await response.json();
  if (payload.status !== "ready" || typeof payload.url !== "string") throw Error("signin_response_invalid");
  return payload.url;
}

try {
  browser = await chromium.launch({ executablePath: browserExecutable, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await page.goto(origin, { waitUntil: "domcontentloaded" });
  const rawBrowserAudit = await page.evaluate(async (liveReport) => {
    const rules = await import("/static/js/resource_link_rules.js?live-catalog-e2e");
  const links = await import("/static/js/resource_linkify.js?live-catalog-e2e");
  const occurrences = await import("/static/js/resource_occurrences.js?live-catalog-e2e");
  const { ResourceIndex } = await import("/static/js/resource_index.js?live-catalog-e2e");
  const identities = new Map();
  for (const identity of globalThis.__IAC_RESOURCE_LINKS__.identities) {
    identities.set(identity.resourceType, identity);
    for (const alias of identity.aliases) identities.set(alias, identity);
  }
  // The real service sanitizes every stored fact against the current identity
  // spec before returning a snapshot.  Apply the same scope migration here so
  // an older live report cannot preserve a superseded account/regional flag.
  const samples = liveReport.samples.map((sample) => {
    const identity = identities.get(sample.reference.resourceType);
    if (!identity) return sample;
    const reference = {...sample.reference, scopeKind: identity.scopeKind};
    if (identity.scopeKind === "regional") reference.regionId ||= sample.regionId;
    else delete reference.regionId;
    return {...sample, reference};
  });
  const index = new ResourceIndex({ canonicalType: rules.canonicalResourceType });
    index.reset("live-catalog-e2e");
    const loaded = await index.loadSnapshot(
      {
        schemaVersion: 1,
        revision: 1,
        items: samples.map((sample) => sample.reference),
        bindings: [],
      },
      () => {
        throw Error("single-page live snapshot unexpectedly requested another page");
      },
    );
    if (!loaded) throw Error("live resource snapshot was rejected");
    const verified = [];
    const failures = [];
    for (const sample of samples) {
      const source = [
        "```json",
        JSON.stringify({
          ResourceType: sample.resourceType,
          PhysicalResourceId: sample.identifier,
          ...sample.reference.attributes,
        }),
        "```",
      ].join("\n");
      const owner = {
        toolUseId: sample.toolUseId,
        messageId: sample.toolUseId,
        turnId: "live-catalog-turn",
      };
      const root = document.createElement("section");
      root.dataset.resourceType = sample.resourceType;
      root.textContent = source;
      document.getElementById("fixture").append(root);
      links.decorateResourceLinks(root, { index, rules: rules.RESOURCE_LINK_RULES, context: owner, complete: true });
      const candidates = occurrences
        .scanResourceOccurrences(source, { index, context: owner, complete: true })
        .filter((item) => item.identifier === sample.identifier);
      const resolutions = candidates.map((item) => rules.resolveResourceOccurrence(item, index));
      const alternativeResolutions = candidates.map((item) =>
        links.resolveResourceOccurrenceAlternatives(item, index, rules.RESOURCE_LINK_RULES));
      const resolution = alternativeResolutions.find((item) => item.status !== "resolved") ||
        alternativeResolutions[0] || resolutions[0];
      // Long identifiers can span syntax-highlighted/line-wrapped text nodes.
      // The first generated piece carries the full accessible label even when
      // no single anchor node owns the complete visible string.
      const anchor = [...root.querySelectorAll("a.resource-link")].find(
        (node) => node.getAttribute("aria-label") === sample.identifier || node.textContent === sample.identifier);
      if (anchor) {
        verified.push({
          source: sample.source,
          resourceType: sample.resourceType,
          identifier: sample.identifier,
          routeIdentifiers: [
            sample.identifier,
            ...(sample.reference.aliases || []).map((alias) => alias.identifier).filter(Boolean),
          ],
          href: anchor.href,
          ruleId: resolution?.ruleId || "",
        });
        continue;
      }
      failures.push({
        source: sample.source,
        resourceType: sample.resourceType,
        reason: resolution?.status || (root.querySelector("button.resource-link-pending") ? "pending" : "not_recognized"),
        missing: resolution?.missing || [],
        matchingWrappers: [...root.querySelectorAll("[data-resource-link]")]
          .filter((node) => node.getAttribute("aria-label") === sample.identifier).map((node) => node.tagName),
        anchorCount: root.querySelectorAll("a.resource-link").length,
        occurrenceTypes: candidates.map((item) => [
          item.resourceType, ...(item.alternatives || []).map((alternative) => alternative.resourceType),
        ]),
      });
    }
    return { verified, failures };
  }, report);
  const browserAudit = {
    ...rawBrowserAudit,
    verified: rawBrowserAudit.verified.map((item) => ({
      ...item,
      exactRoute: item.routeIdentifiers.some((identifier) => locationContainsIdentifier(item.href, identifier)),
    })),
  };

  const applicationOrigin = argument("--app-origin").replace(/\/$/, "");
  const sessionId = argument("--session-id");
  const destinationAudit = {
    enabled: Boolean(applicationOrigin && sessionId), attempted: 0, loaded: 0, failures: [],
    sampleAgeSeconds: null,
  };
  if (destinationAudit.enabled) {
    const completedAt = Date.parse(report.completedAt || "");
    destinationAudit.sampleAgeSeconds = Number.isFinite(completedAt)
      ? Math.max(0, Math.round((Date.now() - completedAt) / 1000))
      : null;
    const maxSampleAgeSeconds = Number(argument("--max-sample-age-seconds", "900"));
    const fresh = Number.isFinite(destinationAudit.sampleAgeSeconds) && Number.isFinite(maxSampleAgeSeconds) &&
      maxSampleAgeSeconds > 0 && destinationAudit.sampleAgeSeconds <= maxSampleAgeSeconds;
    if (!fresh) destinationAudit.failures.push({
      source: "catalog", resourceType: "", ruleId: "", reason: "live_report_stale",
    });
    for (const item of fresh ? browserAudit.verified : []) {
      destinationAudit.attempted += 1;
      let targetPage;
      try {
        const signinUrl = await signinDestination(applicationOrigin, sessionId, report.partition, item.href);
        targetPage = await context.newPage();
        await targetPage.goto(signinUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
        await targetPage.waitForTimeout(2500);
        const finalHost = new URL(targetPage.url()).hostname;
        if (/^(?:account|signin)\./.test(finalHost) || /\/login(?:\/|\.|$)/i.test(targetPage.url())) {
          throw Error("login_required");
        }
        await assertConsoleResourceLoaded(targetPage, item);
        destinationAudit.loaded += 1;
      } catch (error) {
        destinationAudit.failures.push({
          source: item.source,
          resourceType: item.resourceType,
          identifier: item.identifier,
          href: item.href,
          ruleId: item.ruleId,
          reason: String(error.message || error),
        });
      } finally {
        await targetPage?.close();
      }
    }
  }

  const output = {
    schemaVersion: 1,
    catalog: report.catalog,
    scan: report.scan,
    directScan: report.directScan,
    coverage: {
      discoveredTypeCount: report.coverage.discoveredTypeCount,
      sampledTypeCount: report.coverage.sampledTypeCount,
      noSampleTypeCount: report.coverage.noSampleTypeCount,
      noSampleTypes: report.coverage.noSampleTypes,
      discoveredWithoutUsableSample: report.coverage.discoveredWithoutUsableSample,
      directSampledTypeCount: report.coverage.directSampledTypeCount,
      directSampledTypes: report.coverage.directSampledTypes,
      directNoSampleTypes: report.coverage.directNoSampleTypes,
      rosUnavailableSamples: report.coverage.rosUnavailableSamples || [],
    },
    collectionFailures: report.failures,
    browser: {
      attempted: report.samples.length,
      linked: browserAudit.verified.length,
      listRoutes: browserAudit.verified.filter((item) => !item.exactRoute).map((item) => item.resourceType),
      routes: browserAudit.verified,
      failures: browserAudit.failures.map(safeTypeFailure),
    },
    destinations: destinationAudit,
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(output, null, 2) + "\n", "utf8");
  console.log(
    JSON.stringify({
      catalog: output.catalog.resourceCount,
      scannedRegions: `${output.scan.scannedRegionCount}/${output.scan.regionCount}`,
      discoveredTypes: output.coverage.discoveredTypeCount,
      directContracts: `${output.directScan.sampledContractCount}/${output.directScan.contractCount}`,
      linked: `${output.browser.linked}/${output.browser.attempted}`,
      collectionFailures: output.collectionFailures.length,
      browserFailures: output.browser.failures.length,
      destinations: output.destinations.enabled
        ? `${output.destinations.loaded}/${output.destinations.attempted}`
        : "disabled",
      output: outputPath,
    }),
  );
  if (!output.scan.complete || !output.directScan.complete || output.collectionFailures.length || output.browser.failures.length ||
      output.destinations.failures.length) process.exitCode = 1;
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
