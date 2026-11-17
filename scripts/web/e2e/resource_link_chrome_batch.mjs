#!/usr/bin/env node
/**
 * Drive one real iac-code Web resource-link batch through an already running
 * Chrome CDP endpoint.  The active Web session must contain the resources in
 * the plan.  This script discovers their server-projected resource facts,
 * performs ordinary and Shift clicks through the real DOM, and writes the
 * machine-readable browser batch consumed by run_resource_link_acceptance.py.
 *
 * It never constructs a console URL and never calls a cloud API directly.
 */

import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { isNasMountTarget, nasMountTargetLanding } from "./nas_mount_target_landing.mjs";
import { isPolardbAccount, polardbAccountContentReady, polardbAccountLanding } from "./polardb_account_landing.mjs";
import { isPolardbDatabase, polardbDatabaseLanding, polardbClusterContentReady } from "./polardb_database_landing.mjs";
import { isPolardbEndpoint, polardbEndpointLanding } from "./polardb_endpoint_landing.mjs";
import { isOtsInstance, otsInstanceSurfaceState } from "./ots_instance_landing.mjs";
import { isOtsTable, otsTableSurfaceState } from "./ots_table_landing.mjs";
import { isAccountResourceLanding, accountResourceLanding } from "./account_resource_landing.mjs";
import { isResourceGroup, resourceGroupSurfaceState } from "./resource_group_landing.mjs";
import { isVbrRouterInterface, routerInterfaceLanding } from "./router_interface_landing.mjs";
import { isSlbListener, slbListenerLanding } from "./slb_listener_landing.mjs";
import { childListKind, childListLanding } from "./sae_sag_child_landing.mjs";

const require = createRequire(import.meta.url);
const modules = process.env.IAC_RESOURCE_TEST_NODE_MODULES;
function loadChromium() {
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
  throw new Error(
    "playwright-core is unavailable; set IAC_RESOURCE_TEST_NODE_MODULES to an existing node_modules directory. " +
    failures.join("; "),
  );
}

function parseArgs(argv) {
  const result = {
    cdpEndpoint: "http://127.0.0.1:9222",
    appOrigin: "",
    timeoutMs: 30000,
    preflightInput: "",
    preflightOutput: "",
    preflightResourceType: "",
    sessionId: "",
    normalOnly: false,
    normalOnlyPreflight: false,
    preservedShiftBatch: "",
    userDataDir: "",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (name === "--help") result.help = true;
    else if (name === "--self-test") result.selfTest = true;
    else if (name === "--plan") result.plan = argv[++index];
    else if (name === "--output") result.output = argv[++index];
    else if (name === "--artifacts-dir") result.artifactsDir = argv[++index];
    else if (name === "--cdp-endpoint") result.cdpEndpoint = argv[++index];
    else if (name === "--app-origin") result.appOrigin = argv[++index];
    else if (name === "--timeout-ms") result.timeoutMs = Number(argv[++index]);
    else if (name === "--preflight-input") result.preflightInput = argv[++index];
    else if (name === "--preflight-output") result.preflightOutput = argv[++index];
    else if (name === "--preflight-resource-type") result.preflightResourceType = argv[++index];
    else if (name === "--session-id") result.sessionId = argv[++index];
    else if (name === "--normal-only") result.normalOnly = true;
    else if (name === "--normal-only-preflight") result.normalOnlyPreflight = true;
    else if (name === "--preserved-shift-batch") result.preservedShiftBatch = argv[++index];
    else if (name === "--user-data-dir") result.userDataDir = argv[++index];
    else throw new Error(`unknown argument: ${name}`);
  }
  if (result.help || result.selfTest) return result;
  for (const field of ["plan", "output", "artifactsDir"]) {
    if (!result[field]) throw new Error(`--${field.replace(/[A-Z]/g, (value) => `-${value.toLowerCase()}`)} is required`);
  }
  if (!result.appOrigin) throw new Error("--app-origin is required; select the active iac-code Web origin explicitly");
  const origin = new URL(result.appOrigin);
  if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password || origin.search || origin.hash ||
      origin.pathname !== "/") {
    throw new Error("--app-origin must be a bare HTTP(S) origin without credentials, path, query, or fragment");
  }
  result.appOrigin = origin.origin;
  if (!Number.isInteger(result.timeoutMs) || result.timeoutMs < 1000 || result.timeoutMs > 120000) {
    throw new Error("--timeout-ms must be an integer between 1000 and 120000");
  }
  if (Boolean(result.preflightInput) === Boolean(result.preflightOutput)) {
    throw new Error("provide exactly one of --preflight-input or --preflight-output");
  }
  if (result.normalOnlyPreflight && (!result.preflightOutput || !result.preservedShiftBatch)) {
    throw new Error("--normal-only-preflight requires --preflight-output and --preserved-shift-batch");
  }
  return result;
}

function usage() {
  return `Usage:
  node scripts/web/e2e/resource_link_chrome_batch.mjs \\
    --plan /tmp/resource-link-run/next-batch.json \\
    --output /tmp/resource-link-run/browser-batch.json \\
    --artifacts-dir /tmp/resource-link-run/browser-artifacts \\
    --app-origin http://<active-web-host>:<active-web-port> \\
    --session-id <active-iac-code-session-id> \\
    --preflight-output /tmp/resource-link-run/browser-preflight-input.json

The Chrome instance must expose a CDP endpoint (default 127.0.0.1:9222),
already be logged in to aliyun.com, and show the intended iac-code Web session.
Use --preflight-input for a still-fresh preflight from the same Web session.`;
}

function readJson(file) {
  const value = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`expected JSON object: ${file}`);
  return value;
}

const SENSITIVE_PARAMETER = "(?:web_token|access_token|auth_token|token|authorization|signature|sig|secret|secret_key|access_key_secret|code)";

function redactSensitiveText(value) {
  if (typeof value !== "string" || !value) return value;
  let redacted = value.replace(/(#web_token=)[^&#\s"'<>]+/gi, "$1[REDACTED]");
  redacted = redacted.replace(
    new RegExp(`([?&]${SENSITIVE_PARAMETER}=)[^&#\\s\\"'<>]+`, "gi"),
    "$1[REDACTED]",
  );
  redacted = redacted.replace(
    new RegExp(`((?:^|[\\s\\"'(])${SENSITIVE_PARAMETER}=)[^&\\s\\"'<>]+`, "gi"),
    "$1[REDACTED]",
  );
  redacted = redacted.replace(
    new RegExp(`(\\"${SENSITIVE_PARAMETER}\\"\\s*:\\s*\\")[^\\"]+`, "gi"),
    "$1[REDACTED]",
  );
  redacted = redacted.replace(
    new RegExp(`(%3F|%26)(${SENSITIVE_PARAMETER})(%3D)[^%\\s\\"'<>]+`, "gi"),
    "$1$2$3[REDACTED]",
  );
  return redacted;
}

function sanitizePersistedValue(value, key = "") {
  if (typeof value === "string") {
    if (new RegExp(`^${SENSITIVE_PARAMETER}$`, "i").test(key)) return "[REDACTED]";
    return redactSensitiveText(value);
  }
  if (Array.isArray(value)) return value.map((item) => sanitizePersistedValue(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([name, item]) =>
      [name, sanitizePersistedValue(item, name)]));
  }
  return value;
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(sanitizePersistedValue(value), null, 2)}\n`,
    { encoding: "utf8", mode: 0o600 });
  fs.renameSync(temporary, file);
}

function acquireLock(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const token = `${process.pid}:${Date.now()}`;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      fs.writeFileSync(file, `${token}\n`, { encoding: "ascii", mode: 0o600, flag: "wx" });
      return token;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const previous = fs.readFileSync(file, "ascii").trim();
      const owner = Number(previous.split(":", 1)[0]);
      let alive = Number.isInteger(owner) && owner > 0;
      if (alive) {
        try { process.kill(owner, 0); } catch (_ownerError) { alive = false; }
      }
      if (alive) throw new Error(`another browser capture is active (pid ${owner})`);
      if (fs.readFileSync(file, "ascii").trim() === previous) fs.unlinkSync(file);
    }
  }
  throw new Error("unable to acquire browser capture lock");
}

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const fileDigest = (file) => sha256(fs.readFileSync(file));
const isoNow = () => new Date().toISOString();
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function normalizeConsoleHost(hostname) {
  return String(hostname || "")
    .replace("4service.console.aliyun.com", ".console.aliyun.com")
    .replace("4service-", "-");
}

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password ? parsed : null;
  } catch (_error) {
    return null;
  }
}

function pageSignals(url, text) {
  const lowerUrl = String(url || "").toLowerCase();
  const lowerText = String(text || "").toLowerCase();
  return {
    login: /account\.aliyun\.com\/(?:login|miniLogin)|signin\.aliyun\.com|\/login(?:[/?#]|$)|miniNeedLogin/i.test(lowerUrl) ||
      /(请登录|尚未登录|未登录|账号登录|手机号登录|扫码登录|重新登录|sign in|log in)/i.test(text),
    wrongAccount: /(账号不匹配|不属于当前账号|切换账号|wrong account|account mismatch)/i.test(text),
    wrongVariant: /(版本不匹配|规格版本不匹配|企业版.*社区版|社区版.*企业版|wrong (?:edition|variant))/i.test(text),
    permission: /(没有权限|无权访问|权限不足|forbidden|accessdenied|not authorized|OTSNoPermissionAccess|you have no permission to access)/i.test(text),
    missing: /(资源不存在|实例不存在|不存在或已删除|resource[^\n]{0,40}not found|does not exist)/i.test(text),
    gateway: /\b(502|503|504)\b|gateway time-?out|tengine/i.test(`${lowerUrl}\n${lowerText}`),
    routeError: /\b404\b|page not found|页面不存在/i.test(`${lowerUrl}\n${lowerText}`),
  };
}

function containsExactLiteral(value, literal) {
  if (!value || !literal) return false;
  const text = String(value);
  const decoded = (() => {
    try { return decodeURIComponent(text); } catch (_error) { return text; }
  })();
  const escaped = String(literal).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`(^|[^A-Za-z0-9_-])${escaped}($|[^A-Za-z0-9_-])`);
  return pattern.test(text) || pattern.test(decoded);
}

function resourceIdentityLiterals(reference) {
  const identifier = String(reference?.identifier || "");
  const literals = identifier ? [identifier] : [];
  if (reference?.resourceType === "ALIYUN::CEN::RouteEntry") {
    const parts = identifier.split("/");
    if (parts.length === 3 && /^cen-[a-z0-9]+$/i.test(parts[0]) &&
        /^vpc-[a-z0-9]+$/i.test(parts[1]) && /^vtb-[a-z0-9]+$/i.test(parts[2])) {
      literals.unshift(parts[2]);
    }
  }
  if (reference?.resourceType === "ALIYUN::CEN::TransitRouterVpcAttachment" &&
      /^vpc-[a-z0-9]+$/i.test(reference?.attributes?.VpcId || "")) {
    literals.unshift(reference.attributes.VpcId);
  }
  return [...new Set(literals)];
}

function containsResourceIdentity(value, reference) {
  return resourceIdentityLiterals(reference).some((literal) => containsExactLiteral(value, literal));
}

function nasRuleRowMatches(row, reference) {
  const cidr = reference.attributes?.SourceCidrIp;
  const priority = reference.attributes?.Priority;
  const cells = (row.cells || []).map((cell) => cell.trim());
  const rw = { RDWR: "读写", RDONLY: "只读" }[reference.attributes?.RWAccess];
  const user = reference.attributes?.UserAccess;
  const fullTuple = Boolean(rw && ["no_squash", "root_squash", "all_squash"].includes(user) &&
    cells[0] === cidr && cells[1] === rw && cells[2]?.endsWith(`（${user}）`) && cells[3] === priority);
  // A short numeric ID in the navigation/body is not proof of a rule.
  // The console hides AccessRuleId. Its four visible API fields are usable
  // only together, on the exact parent route, with exactly one matching row.
  return Boolean(cidr && priority && (fullTuple || ((row.key === reference.identifier ||
    cells.some((cell) => cell === reference.identifier)) &&
    containsExactLiteral(row.text, cidr) && cells.some((cell) => cell === priority))));
}

async function nasRuleLanding(surface, finalUrl, expectedUrl, reference) {
  const actual = safeUrl(finalUrl);
  const expected = safeUrl(expectedUrl);
  const parentRouteMatched = Boolean(actual && expected && actual.pathname === expected.pathname &&
    actual.hash === expected.hash && reference.attributes?.AccessGroupName && reference.attributes?.FileSystemType);
  const rows = await surface.locator('tbody tr, [role="row"], [data-row-key]').evaluateAll((elements) =>
    elements.filter((element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.width > 0 && rectangle.height > 0 && getComputedStyle(element).visibility !== "hidden";
    }).map((element) => ({
      key: element.getAttribute("data-row-key"),
      text: element.innerText || "",
      cells: [...element.querySelectorAll('td, [role="cell"]')].map((cell) => cell.innerText || ""),
    }))).catch(() => []);
  return { parentRouteMatched,
    matched: parentRouteMatched && rows.filter((row) => nasRuleRowMatches(row, reference)).length === 1, rows };
}

function classifyLanding({ navigation, expectedNavigation, finalUrl, bodyText, reference, routeMatched,
  searchPerformed = false, cmsLocated = false, crLocated = false, crLandingKind = null }) {
  if (!routeMatched || !finalUrl) return "unknown";
  if (navigation === "normal" && expectedNavigation === "new_tab") return "external_only";
  // A passed CR product-flow match is authoritative for the two CR types and
  // must be decided before the generic identity/parent heuristics: the CR
  // repository detail page renders the crr-* identifier in its visible Repo ID
  // field while the URL carries the parent segments, which would otherwise
  // mislabel an exact detail page as parent_list.  Namespace keeps the
  // product_list classification the CR flow assigns.  Non-CR references never
  // set crLocated, so every other rule below is untouched.
  if (crLocated) return crLandingKind || "product_list";
  const target = new URL(finalUrl);
  const pathAndQuery = `${target.pathname}${target.search}${target.hash}`;
  const identifierInUrl = containsResourceIdentity(pathAndQuery, reference);
  const identifierInBody = containsResourceIdentity(bodyText, reference);
  const parents = Object.values(reference.attributes || {}).filter((value) => typeof value === "string" && value);
  const parentInUrl = parents.some((value) => containsExactLiteral(pathAndQuery, value));
  const detailRoute = /\/(detail|overview|basic|info)(\/|$)|(^|[?&#])(id|name|instanceId|resourceId)=/i.test(pathAndQuery);
  if (identifierInUrl || (identifierInBody && detailRoute)) return "exact_detail";
  if (parentInUrl && identifierInBody && detailRoute) return "parent_detail";
  if (parentInUrl && identifierInBody) return "parent_list";
  if (identifierInBody && searchPerformed) return "product_list";
  if (cmsLocated) return "product_list";
  // The CR branch now lives directly after the two guards above; reaching this
  // point means the CR flow did not match, so the generic heuristics apply.
  if (identifierInBody && /\/list(\/|$)|table/i.test(`${pathAndQuery}\n${bodyText.slice(0, 1000)}`)) {
    return "product_list";
  }
  if (identifierInBody) return "shared_tab";
  return "unknown";
}

const SEARCH_METADATA = /search|搜索|关键词|关键字|模糊|名称|实例|资源|策略|\bid\b/i;
const SEARCH_CONTROL_MIN_SCORE = 6;

function searchControlScore(details) {
  const type = String(details?.type || "text").toLowerCase();
  const metadata = [details?.aria, details?.className, details?.name, details?.placeholder,
    details?.title, type].filter(Boolean).join(" ");
  const semantic = SEARCH_METADATA.test(metadata);
  if (type !== "search" && !semantic) return null;
  if (details?.inNavigation) return null;
  let score = type === "search" ? 8 : 0;
  if (semantic) score += 6;
  if (details?.placeholder || details?.aria || details?.title) score += 2;
  if (String(details?.placeholder || "").length >= 8) score += 2;
  return score >= SEARCH_CONTROL_MIN_SCORE ? score : null;
}

async function visibleSearchControl(surface) {
  if (!surface || typeof surface.locator !== "function") return null;
  const inputs = surface.locator("input");
  const candidates = [];
  for (let index = 0; index < await inputs.count(); index += 1) {
    const input = inputs.nth(index);
    if (!await input.isVisible().catch(() => false)) continue;
    const details = await input.evaluate((element) => ({
      aria: element.getAttribute("aria-label") || "",
      className: element.className || "",
      name: element.getAttribute("name") || "",
      placeholder: element.getAttribute("placeholder") || "",
      readOnly: element.readOnly,
      title: element.getAttribute("title") || "",
      type: element.getAttribute("type") || "text",
      disabled: element.disabled,
      inNavigation: Boolean(element.closest("header, nav, [role=\"navigation\"]")),
    })).catch(() => null);
    if (!details || details.disabled || details.readOnly || details.type === "hidden") continue;
    const score = searchControlScore(details);
    if (score === null) continue;
    candidates.push({ input, score, details });
  }
  candidates.sort((left, right) => right.score - left.score);
  return candidates[0] || null;
}

async function visibleStructuredResourceMatch(surface, query) {
  // A console search commonly repeats the submitted value in an input, filter
  // chip or URL even when the result table is empty.  Only a visible result
  // row/list item or a real link is acceptable evidence after a search.
  const candidates = surface.locator("tbody tr, [role=\"row\"], [role=\"listitem\"], [data-row-key], a[href]");
  for (let index = 0; index < await candidates.count(); index += 1) {
    const candidate = candidates.nth(index);
    if (!await candidate.isVisible().catch(() => false)) continue;
    const details = await candidate.evaluate((element) => ({
      text: element.innerText || element.textContent || "",
      searchChrome: Boolean(element.closest(
        "form, [role=\"search\"], [role=\"searchbox\"], [class*=\"filter\" i], [class*=\"search\" i], "+
        "[class*=\"tag\" i], [class*=\"chip\" i]",
      )),
    })).catch(() => null);
    if (!details?.searchChrome && containsExactLiteral(details?.text, query)) return true;
  }
  return false;
}

async function searchVisibleResource(surface, reference, deadline) {
  const noSearch = {
    attempted: false,
    performed: false,
    control: null,
    query: null,
    resultText: null,
    resourceMatched: false,
    finalUrl: null,
  };
  const control = await visibleSearchControl(surface);
  if (!control) return noSearch;
  const query = reference.identifier;
  const metadata = control.details;
  const result = {
    attempted: true,
    performed: false,
    control: {
      aria: metadata.aria,
      name: metadata.name,
      placeholder: metadata.placeholder,
      title: metadata.title,
      type: metadata.type,
    },
    query,
    resultText: null,
    resourceMatched: false,
    finalUrl: null,
  };
  try {
    await control.input.fill(query);
    await control.input.press("Enter").catch(() => {});
    result.performed = true;
    const stable = await stablePageResult(surface, deadline);
    result.finalUrl = stable.finalUrl;
    result.resultText = String(stable.bodyText || "").slice(0, 2000);
    result.resourceMatched = await visibleStructuredResourceMatch(surface, query);
  } catch (_error) {
    result.finalUrl = safeUrl(surface.url())?.href || null;
    result.resultText = await surface.locator("body").innerText().catch(() => "");
  }
  return result;
}

function resolveSearchFinalUrl(search, fallback) {
  if (!search?.performed) return fallback;
  return safeUrl(search.finalUrl)?.href || fallback;
}

// --- CMS (CloudMonitor) product-specific landing localization ----------------
// Diagnostics (task-008-cms-diagnostic) proved the production links for
// ALIYUN::CMS::Contact and ALIYUN::CMS::ContactGroup are correct: both land on
// the signed-in /alert/contact page.  The identifier is hidden either behind
// the account-level reversible 数据脱敏 display switch (contacts) or on the
// second visible tab 报警联系组 (groups).  The generic body-text match is
// unsafe here (contact "ros-cms-test" contains group "cms-test"), so these two
// types get a dedicated real-UI flow that matches only visible structured
// rows.  Production link rules are untouched.
const CMS_CONTACT_TYPE = "ALIYUN::CMS::Contact";
const CMS_CONTACT_GROUP_TYPE = "ALIYUN::CMS::ContactGroup";
const CMS_ALERT_CONTACT_PATH = "/alert/contact";
const CMS_GROUP_TAB_TEXT = "报警联系组";
const CMS_MASKING_SWITCH_TEXT = "数据脱敏";
const CMS_CONTACT_SCREENSHOT_OMISSION =
  "CMS contacts table renders webhook/secret columns; full-surface screenshot omitted for evidence hygiene; redacted observation JSON retained instead";
// The group tab must be clicked through Playwright's visible locators with
// exact text and normal actionability; never evaluateHandle + raw DOM click.
const CMS_GROUP_TAB_STRATEGY = {
  roleExact: true,
  actionabilityClick: true,
  evaluateHandleClick: false,
};
// After the exact 报警联系组 tab click the group pane renders its rows
// asynchronously: stablePageResult can return while the dual-pane/skeleton
// rows (contacts + groups, names not yet rendered) are still on screen, so a
// single immediate row scan observes rowCount=20 with no names even though the
// screenshot taken ~1s later shows the exact group row.  Poll the visible
// exact-row match with a short dynamic interval instead of one fixed sleep:
// stop the moment the row appears, and never exceed the 5s cap or the branch
// deadline, whichever is sooner.
const CMS_GROUP_ROW_SETTLE_INTERVAL_MS = 100;
const CMS_GROUP_ROW_SETTLE_MAX_MS = 5000;

// Pure settle budget planner.  The poll cap is the smaller of the product cap
// and the remaining branch deadline; a non-finite or already-passed deadline
// collapses the budget to zero so the loop degrades to a single immediate
// probe (safe failure) instead of sleeping past the deadline.
function cmsGroupRowSettleBudget(deadline, now = Date.now()) {
  const remaining = Number.isFinite(deadline) ? deadline - now : 0;
  const capMs = Math.min(CMS_GROUP_ROW_SETTLE_MAX_MS, Math.max(0, remaining));
  return { capMs, intervalMs: Math.min(CMS_GROUP_ROW_SETTLE_INTERVAL_MS, capMs) };
}

// Dynamic short poll of the visible exact-row match.  Injected deps keep it
// testable without a browser.  Stops immediately on a hit; otherwise retries
// every ~100ms until the cap or the branch deadline.  No fixed long sleep.
async function cmsGroupRowSettlePoll(deps, { identifier, deadline, now = Date.now() }) {
  const settle = {
    attempts: 0,
    waitedMs: 0,
    matched: false,
    finalRowCount: 0,
    capMs: null,
    intervalMs: null,
    timedOut: false,
  };
  const budget = cmsGroupRowSettleBudget(deadline, now);
  settle.capMs = budget.capMs;
  settle.intervalMs = budget.intervalMs;
  const clock = deps.clock || Date.now;
  const rest = deps.sleep || sleep;
  const probe = async () => {
    const row = await deps.rowMatch(identifier);
    settle.attempts += 1;
    settle.finalRowCount = row.rowCount || 0;
    settle.matched = Boolean(row.matched);
    return row;
  };
  let row = await probe();
  while (!row.matched && settle.waitedMs < budget.capMs && clock() < deadline) {
    const wait = Math.min(budget.intervalMs, budget.capMs - settle.waitedMs, Math.max(0, deadline - clock()));
    if (wait <= 0) break;
    await rest(wait);
    settle.waitedMs += wait;
    row = await probe();
  }
  settle.timedOut = !row.matched;
  return { row, settle };
}

async function cmsGroupRowSettleMatch(surface, identifier, deadline) {
  return cmsGroupRowSettlePoll(
    { rowMatch: (value) => cmsGroupVisibleRowCards(surface, value) },
    { identifier, deadline },
  );
}

function isCmsContactType(reference) { return reference?.resourceType === CMS_CONTACT_TYPE; }
function isCmsContactGroupType(reference) { return reference?.resourceType === CMS_CONTACT_GROUP_TYPE; }
function isCmsAlertContactSurface(finalUrl) {
  const target = safeUrl(finalUrl);
  return Boolean(target) && target.pathname.startsWith(CMS_ALERT_CONTACT_PATH);
}
function cmsFlowApplies(reference, finalUrl) {
  return (isCmsContactType(reference) || isCmsContactGroupType(reference)) &&
    isCmsAlertContactSurface(finalUrl);
}
function cmsScreenshotPolicy(flow) {
  return flow === "contact_masking_switch" ? CMS_CONTACT_SCREENSHOT_OMISSION : null;
}
function cmsRowTextMatches(rowText, identifier) {
  // Boundary-safe exact match on a single visible row only; never the whole
  // page body, so "ros-cms-test" cannot satisfy identifier "cms-test".
  return containsExactLiteral(rowText, identifier);
}
function cmsMaskingRestoreVerified(initialSwitchOn, finalSwitchOn, exactRowAfterRestore) {
  return finalSwitchOn === initialSwitchOn && (initialSwitchOn ? !exactRowAfterRestore : true);
}
// A restore that did not verify must fail the branch: passing with the
// account-level display switch left flipped (or unverifiable) is not allowed.
function cmsRestoreBlocksPass(toggled, restore) {
  return Boolean(toggled) && !Boolean(restore && restore.verified);
}
// Pure association decision: the masking control is only clickable when the
// visible 数据脱敏 label resolves to exactly one switch.  An unrelated first
// switch on the page, several candidate switches, or no label at all are all
// safe failures.
function cmsMaskingControlDecision({ switchCount, labelCount, scopes }) {
  if (!labelCount) return { unique: false, switchIndex: null, reason: "no_visible_label" };
  if (labelCount !== 1) return { unique: false, switchIndex: null, reason: "ambiguous_visible_labels" };
  if (!Array.isArray(scopes) || scopes.length !== 1) {
    return { unique: false, switchIndex: null, reason: "invalid_label_scope" };
  }
  const sets = (scopes || []).map((scope) => (scope.switchIndices || []).slice().sort((a, b) => a - b).join(","));
  const distinct = [...new Set(sets)];
  if (distinct.length !== 1 || distinct[0] === "") {
    return { unique: false, switchIndex: null, reason: distinct.length > 1 ? "labels_disagree" : "no_switch_in_scope" };
  }
  const indices = distinct[0].split(",").map(Number);
  if (indices.length !== 1) return { unique: false, switchIndex: null, reason: "ambiguous_switches_in_scope" };
  if (switchCount != null && indices[0] >= switchCount) return { unique: false, switchIndex: null, reason: "stale_switch_index" };
  return { unique: true, switchIndex: indices[0], reason: "single_associated_switch" };
}

async function cmsVisibleRowExactMatch(surface, identifier) {
  const rows = surface.locator("tbody tr, [role=\"row\"], [role=\"listitem\"], [data-row-key], .next-table-row");
  const matched = [];
  const total = await rows.count().catch(() => 0);
  for (let index = 0; index < total; index += 1) {
    const row = rows.nth(index);
    if (!await row.isVisible().catch(() => false)) continue;
    const text = await row.innerText().catch(() => "");
    if (cmsRowTextMatches(text, identifier)) matched.push(String(text).replace(/\s+/g, " ").slice(0, 160));
  }
  return { matched: matched.length > 0, matchedRows: matched.slice(0, 3), rowCount: total };
}

// --- ContactGroup-only visible row/card locator ----------------------------
// The group pane renders Fusion/Next collapse cards (.next-collapse-panel),
// not table rows, while the hidden contact table keeps duplicate locked-column
// rows in the DOM.  The generic cmsVisibleRowExactMatch selector therefore
// counted 20 hidden contact rows and never saw the rendered group cards.
// This locator is ContactGroup-only: it scopes to the single visible tab pane
// when identifiable and covers the collapse/card row structures; hidden
// contact-table rows are never candidates, and ambiguous duplicate exact
// matches fail closed.  No whole-page body substring fallback exists.
const CMS_GROUP_PANE_SELECTOR = "[role=\"tabpanel\"], .next-tabs-tabpane";
const CMS_GROUP_ROW_CARD_SELECTOR = [
  ".next-collapse-panel",
  ".next-card",
  ".next-table-row",
  "[data-row-key]",
  "[role=\"listitem\"]",
  "tbody tr",
].join(", ");

// Pure pane-scope decision: scope only when exactly one tab pane is visible;
// zero or several visible panes fall back to per-element visibility filtering
// (which still excludes display:none contact rows).
function cmsGroupPaneScope({ paneCount, visiblePaneCount }) {
  if (visiblePaneCount === 1) return { scoped: true, reason: "single_visible_pane" };
  if (visiblePaneCount === 0) return { scoped: false, reason: "no_visible_pane" };
  return { scoped: false, reason: "ambiguous_visible_panes", paneCount };
}

// Boundary-safe card text match; reuses the exact-literal predicate so a
// prefixed/suffixed different identifier can never pass.
function cmsGroupCardMatches(cardText, identifier) {
  return cmsRowTextMatches(cardText, identifier);
}

// Pure candidate evaluation over visible/hidden card facts.  Hidden cards are
// excluded from the candidate count; exactly one visible exact match passes;
// zero or several visible exact matches fail closed.
function cmsGroupRowCardResult(cards, identifier) {
  const candidates = (cards || []).filter((card) => card && card.visible);
  const matchedRows = [];
  for (const card of candidates) {
    const text = String(card.text || "").replace(/\s+/g, " ").trim();
    if (cmsGroupCardMatches(text, identifier)) matchedRows.push(text.slice(0, 160));
  }
  return {
    matched: matchedRows.length === 1,
    matchedRows: matchedRows.slice(0, 3),
    rowCount: candidates.length,
    ambiguous: matchedRows.length > 1,
    hiddenExcluded: (cards || []).length - candidates.length,
  };
}

async function cmsGroupVisibleRowCards(surface, identifier) {
  const raw = await surface.evaluate(({ paneSelector, rowSelector }) => {
    const vis = (el) => Boolean(el) && el.offsetParent !== null;
    const panes = [...document.querySelectorAll(paneSelector)];
    const visiblePanes = panes.filter(vis);
    const scope = visiblePanes.length === 1 ? visiblePanes[0] : document;
    const cards = [...scope.querySelectorAll(rowSelector)];
    return {
      paneCount: panes.length,
      visiblePaneCount: visiblePanes.length,
      cards: cards.map((el) => ({ visible: vis(el), text: el.innerText || "" })),
    };
  }, { paneSelector: CMS_GROUP_PANE_SELECTOR, rowSelector: CMS_GROUP_ROW_CARD_SELECTOR }).catch(() => null);
  if (!raw) {
    return { matched: false, matchedRows: [], rowCount: 0, ambiguous: false, hiddenExcluded: 0,
      paneScoped: false, paneCount: 0, visiblePaneCount: 0, readFailed: true };
  }
  const scope = cmsGroupPaneScope({ paneCount: raw.paneCount, visiblePaneCount: raw.visiblePaneCount });
  return {
    ...cmsGroupRowCardResult(raw.cards, identifier),
    paneScoped: scope.scoped,
    paneScopeReason: scope.reason,
    paneCount: raw.paneCount,
    visiblePaneCount: raw.visiblePaneCount,
  };
}

// Reads every visible switch once, indexes it, then associates the visible
// 数据脱敏 label with the switch in its own scope.  Returns the pure decision
// plus the on-state of the single associated switch when unique.
async function cmsMaskingSwitchState(surface) {
  const raw = await surface.evaluate((text) => {
    const vis = (el) => Boolean(el) && el.offsetParent !== null;
    document.querySelectorAll("[data-cms-sw-idx]").forEach((el) => el.removeAttribute("data-cms-sw-idx"));
    const allSwitches = [...document.querySelectorAll("[role=\"switch\"], .next-switch")].filter(vis);
    allSwitches.forEach((el, index) => el.setAttribute("data-cms-sw-idx", String(index)));
    const switches = allSwitches.map((el, index) => ({
      index,
      on: el.getAttribute("aria-checked") === "true" || /next-switch-on|\bchecked\b/.test(String(el.className)),
    }));
    const matching = [...document.querySelectorAll("*")].filter((el) => vis(el) && (el.innerText || "").trim() === text);
    const labels = matching.filter((el) => !matching.some((other) => other !== el && el.contains(other)));
    const scopes = labels.map((label) => {
      let found = label.matches("[role=\"switch\"], .next-switch")
        ? [label]
        : [...label.querySelectorAll("[role=\"switch\"], .next-switch")].filter(vis);
      let node = label;
      for (let up = 0; up < 3 && found.length === 0; up += 1) {
        node = node.parentElement;
        if (!node) break;
        found = [...node.querySelectorAll("[role=\"switch\"], .next-switch")].filter(vis);
      }
      return {
        switchIndices: [...new Set(found.map((el) => Number(el.getAttribute("data-cms-sw-idx"))))]
          .filter((value) => Number.isFinite(value)),
      };
    });
    return { switchCount: allSwitches.length, labelCount: labels.length, scopes, switches };
  }, CMS_MASKING_SWITCH_TEXT).catch(() => null);
  if (!raw) return { present: false, unique: false, switchIndex: null, switchOn: null, switchCount: 0, reason: "read_failed" };
  const decision = cmsMaskingControlDecision(raw);
  return {
    present: raw.labelCount > 0,
    unique: decision.unique,
    switchIndex: decision.switchIndex,
    switchOn: decision.unique ? (raw.switches[decision.switchIndex] || {}).on ?? null : null,
    switchCount: raw.switchCount,
    reason: decision.reason,
  };
}

async function cmsClearMaskingSwitchIndices(surface) {
  await surface.evaluate(() => {
    document.querySelectorAll("[data-cms-sw-idx]").forEach((el) => el.removeAttribute("data-cms-sw-idx"));
  }).catch(() => {});
}

async function cmsClickMaskingSwitch(surface) {
  // Only the single switch associated with the visible 数据脱敏 label may be
  // clicked; anything else is a safe failure (never another switch).
  const state = await cmsMaskingSwitchState(surface);
  if (!state.unique || state.switchIndex == null) return null;
  const target = surface.locator(`[data-cms-sw-idx="${state.switchIndex}"]`).first();
  if (!await target.isVisible().catch(() => false)) return null;
  await target.click().catch(() => {});
  return `associated-switch#${state.switchIndex}`;
}

async function cmsRestoreMaskingState(surface, reference, initial, deadline) {
  const restore = { attempted: true, clicks: 0, verified: false, finalSwitchOn: null, exactRowAfterRestore: null, reason: null };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const state = await cmsMaskingSwitchState(surface);
    const row = await cmsVisibleRowExactMatch(surface, reference.identifier);
    restore.finalSwitchOn = state.switchOn;
    restore.exactRowAfterRestore = row.matched;
    restore.verified = cmsMaskingRestoreVerified(initial.switchOn, state.switchOn, row.matched);
    if (restore.verified) { restore.reason = "verified"; break; }
    if (!state.unique) { restore.reason = state.reason || "control_not_unique"; break; }
    if (!await cmsClickMaskingSwitch(surface)) { restore.reason = "click_unavailable"; break; }
    restore.clicks += 1;
    await stablePageResult(surface, deadline);
  }
  await cmsClearMaskingSwitchIndices(surface);
  return restore;
}

// Contact flow with an internal try/finally: once the display switch has been
// flipped, every exit path (including exceptions from stability waits or row
// matching) attempts restore+verification.  Injected deps keep this testable
// without a browser.
async function cmsContactToggleFlow(deps, { identifier }) {
  const masking = {
    initial: await deps.readState(),
    toggled: false,
    toggledVia: null,
    restore: null,
    mutatedResources: false,
    error: null,
  };
  let rowMatch = await deps.rowMatch(identifier);
  const needToggle = !rowMatch.matched && masking.initial.present && masking.initial.unique &&
    masking.initial.switchOn !== false;
  if (needToggle) {
    masking.toggled = true;
    try {
      masking.toggledVia = await deps.click();
      await deps.stable();
      rowMatch = await deps.rowMatch(identifier);
    } catch (error) {
      // Stability waits or row matching may explode after the switch was
      // flipped; the branch fails, but the restore below still runs.
      masking.error = String((error && error.message) || error).slice(0, 200);
    } finally {
      masking.restore = await deps.restore(masking.initial);
    }
  }
  return {
    flow: "contact_masking_switch",
    matched: !masking.error && rowMatch.matched &&
      !cmsRestoreBlocksPass(masking.toggled, masking.restore),
    tab: null,
    masking,
    rowMatch,
    bodyTextAfter: null,
  };
}

async function cmsGroupTabLocate(surface, reference, deadline) {
  const tab = {
    tabFound: false,
    clicked: false,
    selector: null,
    urlBefore: safeUrl(surface.url())?.href || null,
    urlAfter: null,
    urlChanged: null,
    hashQueryDelta: null,
    activeTabAfter: null,
    stable: false,
    rowMatch: null,
    rowSettle: null,
    strategy: CMS_GROUP_TAB_STRATEGY,
    note: "visible Playwright locator with exact tab text and normal actionability click; no URL construction; SPA may keep URL unchanged",
  };
  const candidates = [
    { selector: "getByRole(tab, exact)", locator: surface.getByRole("tab", { name: CMS_GROUP_TAB_TEXT, exact: true }) },
    {
      selector: "locator([role=tab],.next-tabs-tab, exact-text filter)",
      locator: surface.locator("[role=\"tab\"], .next-tabs-tab").filter({ hasText: new RegExp(`^${CMS_GROUP_TAB_TEXT}$`) }),
    },
  ];
  let chosen = null;
  for (const candidate of candidates) {
    if (await candidate.locator.first().isVisible().catch(() => false)) { chosen = candidate; break; }
  }
  if (!chosen) return tab;
  tab.tabFound = true;
  tab.selector = chosen.selector;
  await chosen.locator.first().click();
  tab.clicked = true;
  const stable = await stablePageResult(surface, deadline);
  tab.stable = Boolean(stable.contentReady);
  tab.urlAfter = stable.finalUrl;
  tab.urlChanged = tab.urlBefore !== stable.finalUrl;
  try {
    const before = new URL(tab.urlBefore || "about:blank");
    const after = new URL(tab.urlAfter || "about:blank");
    tab.hashQueryDelta = { before: `${before.search}${before.hash}`, after: `${after.search}${after.hash}` };
  } catch (_error) { tab.hashQueryDelta = null; }
  tab.activeTabAfter = await surface.evaluate(() => {
    const active = [...document.querySelectorAll("[role=\"tab\"], .next-tabs-tab")]
      .find((el) => el.getAttribute("aria-selected") === "true" || el.classList.contains("active"));
    return active ? (active.innerText || "").trim() : null;
  }).catch(() => null);
  const settled = await cmsGroupRowSettleMatch(surface, reference.identifier, deadline);
  tab.rowMatch = settled.row;
  tab.rowSettle = settled.settle;
  tab.bodyTextAfter = stable.bodyText;
  return tab;
}

async function cmsLocateResource(surface, reference, deadline) {
  if (isCmsContactGroupType(reference)) {
    const tab = await cmsGroupTabLocate(surface, reference, deadline);
    const bodyTextAfter = tab.bodyTextAfter || null;
    delete tab.bodyTextAfter;
    return {
      flow: "contact_group_tab",
      matched: Boolean(tab.rowMatch && tab.rowMatch.matched),
      tab,
      masking: null,
      rowMatch: tab.rowMatch,
      rowSettle: tab.rowSettle || null,
      bodyTextAfter,
    };
  }
  try {
    return await cmsContactToggleFlow({
      readState: () => cmsMaskingSwitchState(surface),
      click: () => cmsClickMaskingSwitch(surface),
      stable: () => stablePageResult(surface, deadline),
      rowMatch: (identifier) => cmsVisibleRowExactMatch(surface, identifier),
      restore: (initial) => cmsRestoreMaskingState(surface, reference, initial, deadline),
    }, { identifier: reference.identifier });
  } finally {
    await cmsClearMaskingSwitchIndices(surface);
  }
}

// --- CR (Container Registry) product-specific landing localization -----------
// Task-010 machine evidence proved the production links for ALIYUN::CR::Namespace
// and ALIYUN::CR::Repository are correct, but neither landing renders the
// crn-*/crr-* identifier:
//   * Namespace lands on the parent-instance namespace list
//     /<region>/instance/<InstanceId>/namespaces, which renders NamespaceName
//     only (there is no namespace detail page);
//   * Repository lands on the exact detail route
//     /repository/<region>/<InstanceId>/<RepoNamespaceName>/<RepoName>/details,
//     whose URL carries the names and parent context instead of RepoId.
// The generic identity matcher therefore never matched, and the generic search
// fallback bound to the console-global 产品与服务 search box (which answered
// "未找到与 crn-* 相关的产品" instead of filtering the list).  Both types now get
// a dedicated fail-closed product flow: exact parent-context verification plus,
// for namespaces, a unique visible structured row match on NamespaceName.  The
// generic search fallback is never reached for them.  Names are deliberately NOT
// added to resourceIdentityLiterals(), so no other resource type, CMS locator,
// CEN alias, navigation decision or pass semantic changes.
const CR_NAMESPACE_TYPE = "ALIYUN::CR::Namespace";
const CR_REPOSITORY_TYPE = "ALIYUN::CR::Repository";
const CR_CONSOLE_HOST = "cr.console.aliyun.com";
const CR_NAMESPACE_FLOW = "namespace_parent_list";
const CR_REPOSITORY_FLOW = "repository_detail";
// Structured row candidates of a CR console list.  A wrapper that contains
// another candidate (a card/section around the table) is dropped by the
// innermost-only rule below, so it can never double-count a row.
const CR_ROW_SELECTOR = [
  "tbody tr",
  "[role=\"row\"]",
  ".next-table-row",
  "[data-row-key]",
  "[role=\"listitem\"]",
  ".next-collapse-panel",
  ".next-card",
].join(", ");
// Page chrome is never a resource row: navigation, breadcrumbs, menus, search
// widgets and chips/tags can echo a name without being a list row.  A bare
// <form> ancestor is deliberately NOT disqualifying: some consoles wrap the
// whole page body in a form, and excluding it would hide real rows (fail
// closed on a false negative); filter chips are still excluded above.
const CR_ROW_CHROME_SELECTOR =
  "nav, [role=\"navigation\"], [role=\"search\"], [role=\"searchbox\"], " +
  "[class*=\"breadcrumb\" i], [class*=\"menu\" i], [class*=\"chip\" i], [class*=\"tag\" i]";

function isCrNamespaceType(reference) { return reference?.resourceType === CR_NAMESPACE_TYPE; }
function isCrRepositoryType(reference) { return reference?.resourceType === CR_REPOSITORY_TYPE; }
function isCrType(reference) { return isCrNamespaceType(reference) || isCrRepositoryType(reference); }
// normalizeConsoleHost() maps the embedded cr4service.console.aliyun.com host to
// cr.console.aliyun.com, so the iframe and the new tab are judged identically.
function isCrConsoleHost(hostname) { return normalizeConsoleHost(hostname) === CR_CONSOLE_HOST; }
function crFlowApplies(reference, finalUrl) {
  const target = safeUrl(finalUrl);
  return Boolean(target) && isCrType(reference) && isCrConsoleHost(target.hostname);
}
// The only qualifying search control on a CR console surface is the console-global
// product search box, which is not a list filter.  These two types must never
// reach the generic search fallback, whatever the landing URL is.
function crSuppressesSearchFallback(reference) { return isCrType(reference); }
function crLandingKind(reference) {
  if (isCrRepositoryType(reference)) return "exact_detail";
  if (isCrNamespaceType(reference)) return "product_list";
  return null;
}
function crPathSegments(finalUrl) {
  const target = safeUrl(finalUrl);
  if (!target) return null;
  return target.pathname.split("/").filter(Boolean).map((segment) => {
    try { return decodeURIComponent(segment); } catch (_error) { return segment; }
  });
}
function crExpectedText(value) {
  return typeof value === "string" && value.trim() ? value : null;
}
// A context field passes only on a full segment-exact match against a known
// source attribute; a missing expectation or a missing observation fails closed.
function crContextField(name, expected, observed) {
  return {
    name,
    ok: Boolean(expected) && typeof observed === "string" && observed === expected,
    expected: expected || null,
    observed: typeof observed === "string" && observed ? observed : null,
  };
}
function crContextResult({ hostOk, shapeRecognized, route, fields, reason }) {
  const matched = Boolean(hostOk && shapeRecognized && fields.every((field) => field.ok));
  const firstFailure = fields.find((field) => !field.ok);
  return {
    hostOk: Boolean(hostOk),
    shapeRecognized: Boolean(shapeRecognized),
    matched,
    route: route || null,
    fields,
    reason: matched ? null : (reason || (firstFailure ? firstFailure.name : null) || "context_unmatched"),
  };
}
// Namespace: /<region>/instance/<InstanceId>/namespaces.  The region and the
// parent instance must both come from the reference itself.
function crNamespaceContextCheck(finalUrl, reference) {
  const target = safeUrl(finalUrl);
  const segments = crPathSegments(finalUrl);
  const hostOk = Boolean(target) && isCrConsoleHost(target.hostname);
  const attributes = (reference && reference.attributes) || {};
  let observedRegion = null;
  let observedInstance = null;
  let shapeRecognized = false;
  if (segments) {
    const index = segments.findIndex((segment, position) =>
      segment === "instance" && position >= 1 && segments[position + 2] === "namespaces");
    if (index >= 1) {
      shapeRecognized = true;
      observedRegion = segments[index - 1];
      observedInstance = segments[index + 1];
    }
  }
  return crContextResult({
    hostOk,
    shapeRecognized,
    route: target ? target.pathname : null,
    fields: [
      crContextField("region", crExpectedText(reference && reference.regionId), observedRegion),
      crContextField("instanceId", crExpectedText(attributes.InstanceId), observedInstance),
    ],
    reason: !hostOk ? "host_not_cr_console"
      : (shapeRecognized ? null : "namespace_list_route_not_recognized"),
  });
}
// Repository: /repository/<region>/<InstanceId>/<RepoNamespaceName>/<RepoName>/details.
// All four URL segments must match the source context one by one.
function crRepositoryContextCheck(finalUrl, reference) {
  const target = safeUrl(finalUrl);
  const segments = crPathSegments(finalUrl);
  const hostOk = Boolean(target) && isCrConsoleHost(target.hostname);
  const attributes = (reference && reference.attributes) || {};
  let observed = { region: null, instanceId: null, repoNamespaceName: null, repoName: null };
  let shapeRecognized = false;
  if (segments) {
    const index = segments.findIndex((segment, position) =>
      segment === "repository" && segments[position + 5] === "details");
    if (index >= 0) {
      shapeRecognized = true;
      observed = {
        region: segments[index + 1],
        instanceId: segments[index + 2],
        repoNamespaceName: segments[index + 3],
        repoName: segments[index + 4],
      };
    }
  }
  return crContextResult({
    hostOk,
    shapeRecognized,
    route: target ? target.pathname : null,
    fields: [
      crContextField("region", crExpectedText(reference && reference.regionId), observed.region),
      crContextField("instanceId", crExpectedText(attributes.InstanceId), observed.instanceId),
      crContextField("repoNamespaceName", crExpectedText(attributes.RepoNamespaceName), observed.repoNamespaceName),
      crContextField("repoName", crExpectedText(attributes.RepoName), observed.repoName),
    ],
    reason: !hostOk ? "host_not_cr_console"
      : (shapeRecognized ? null : "repository_detail_route_not_recognized"),
  });
}
// Boundary-safe exact name match on one visible row; never whole-page text and
// never the crn-*/crr-* identifier, which the CR console does not render.
function crNameMatches(rowText, name) {
  if (!name) return false;
  return containsExactLiteral(rowText, name);
}
// Fusion/Next tables render locked columns and row wrappers as extra DOM rows
// for one logical namespace, so raw match counts over-count.  One logical row is
// identified by its rendered top edge within a 2px tolerance (sub-pixel layout,
// locked-column clones and split cells of the same row); rows without a usable
// rect fall back to their normalized text.  Clustering keeps the decision
// fail-closed: two genuinely different visible rows never share a top edge.
const CR_ROW_SAME_EDGE_TOLERANCE_PX = 2;
function crDistinctMatchKeys(matches) {
  const keys = [];
  const withEdge = (matches || []).filter((row) => Number.isFinite(row && row.top));
  const sorted = withEdge.slice().sort((left, right) => left.top - right.top);
  const groups = [];
  for (const row of sorted) {
    const current = groups[groups.length - 1];
    if (current && Math.abs(row.top - current.top) <= CR_ROW_SAME_EDGE_TOLERANCE_PX) continue;
    groups.push(row);
  }
  for (const row of groups) keys.push(`top:${Math.round(row.top)}`);
  for (const row of (matches || [])) {
    if (Number.isFinite(row.top)) continue;
    keys.push(`text:${row.text}`);
  }
  return [...new Set(keys)];
}

// Pure candidate evaluation over row facts read from the landing surface.
// Hidden DOM and page chrome are never candidates.  A row wrapper that contains
// another candidate stays a candidate but is reported as a wrapper: wrappers,
// locked-column clones and split cells of one logical row share its rendered
// top edge, so matched rows are de-duplicated by top (text when no rect is
// available).  Exactly one distinct visible exact row passes; zero or several
// distinct ones fail closed.
function crNamespaceRowResult(rows, name) {
  const all = Array.isArray(rows) ? rows : [];
  const visible = all.filter((row) => row && row.visible);
  const candidates = visible.filter((row) => !row.chrome);
  const matched = [];
  for (const row of candidates) {
    const text = String(row.text || "").replace(/\s+/g, " ").trim();
    if (!crNameMatches(text, name)) continue;
    matched.push({ text: text.slice(0, 160), top: Number.isFinite(row.top) ? row.top : null });
  }
  const distinct = crDistinctMatchKeys(matched);
  return {
    matched: distinct.length === 1,
    matchedRows: matched.slice(0, 3).map((row) => row.text),
    candidateMatches: matched.length,
    distinctMatches: distinct.length,
    rowCount: candidates.length,
    ambiguous: distinct.length > 1,
    hiddenExcluded: all.length - visible.length,
    chromeExcluded: visible.filter((row) => row.chrome).length,
    wrapperCandidates: candidates.filter((row) => row.nested).length,
  };
}
const CR_EMPTY_ROW_MATCH = {
  matched: false,
  matchedRows: [],
  candidateMatches: 0,
  distinctMatches: 0,
  rowCount: 0,
  ambiguous: false,
  hiddenExcluded: 0,
  chromeExcluded: 0,
  wrapperCandidates: 0,
};
async function crVisibleNamespaceRows(surface) {
  if (!surface || typeof surface.evaluate !== "function") return { readFailed: true, rows: [] };
  const raw = await surface.evaluate(({ rowSelector, chromeSelector }) => {
    const isVisible = (element) => Boolean(element) && (element.offsetParent !== null ||
      (typeof element.getClientRects === "function" && element.getClientRects().length > 0));
    const all = [...document.querySelectorAll(rowSelector)];
    return {
      rows: all.map((element) => {
        const rect = typeof element.getBoundingClientRect === "function"
          ? element.getBoundingClientRect() : null;
        return {
          visible: isVisible(element),
          chrome: Boolean(element.closest(chromeSelector)),
          nested: all.some((other) => other !== element && element.contains(other)),
          formWrapped: Boolean(element.closest("form")),
          text: String(element.innerText || element.textContent || ""),
          top: rect ? rect.top : null,
        };
      }),
    };
  }, { rowSelector: CR_ROW_SELECTOR, chromeSelector: CR_ROW_CHROME_SELECTOR }).catch(() => null);
  if (!raw || !Array.isArray(raw.rows)) return { readFailed: true, rows: [] };
  return { readFailed: false, rows: raw.rows };
}
async function crNamespaceRowMatch(surface, name) {
  const scan = await crVisibleNamespaceRows(surface);
  if (scan.readFailed) return { ...CR_EMPTY_ROW_MATCH, readFailed: true };
  return { ...crNamespaceRowResult(scan.rows, name), readFailed: false };
}
// Namespace rows can render just after the route settles, so reuse the generic
// bounded short poll (100ms interval, 5s product cap, branch deadline, stop on
// hit, single probe on an expired deadline) already used by the CMS group flow.
async function crNamespaceRowSettleMatch(surface, name, deadline) {
  return cmsGroupRowSettlePoll(
    { rowMatch: (value) => crNamespaceRowMatch(surface, value) },
    { identifier: name, deadline },
  );
}
function crExactName(reference) {
  const attributes = (reference && reference.attributes) || {};
  if (isCrNamespaceType(reference)) return crExpectedText(attributes.NamespaceName);
  if (isCrRepositoryType(reference)) {
    const namespace = crExpectedText(attributes.RepoNamespaceName) || "";
    const repo = crExpectedText(attributes.RepoName) || "";
    return namespace || repo ? `${namespace}/${repo}` : null;
  }
  return null;
}
// Read-only product flow: it never clicks, types, navigates or constructs a URL.
async function crLocateResource(surface, reference, finalUrl, signals, deadline) {
  const signalsClear = !Object.values(signals || {}).some(Boolean);
  const base = {
    matched: false,
    signalsClear,
    searchFallbackSuppressed: true,
    context: null,
    rowMatch: null,
    rowSettle: null,
    error: null,
  };
  if (isCrNamespaceType(reference)) {
    const context = crNamespaceContextCheck(finalUrl, reference);
    const exactName = crExactName(reference);
    if (!signalsClear) {
      return { ...base, flow: CR_NAMESPACE_FLOW, context, exactName, note: null, reason: "page_signals_present" };
    }
    if (!context.matched) {
      return { ...base, flow: CR_NAMESPACE_FLOW, context, exactName, note: null, reason: context.reason };
    }
    if (!exactName) {
      return {
        ...base, flow: CR_NAMESPACE_FLOW, context, exactName: null, note: null,
        reason: "reference_namespace_name_missing",
      };
    }
    const settled = await crNamespaceRowSettleMatch(surface, exactName, deadline);
    const rowMatch = settled.row;
    return {
      ...base,
      flow: CR_NAMESPACE_FLOW,
      context,
      exactName,
      matched: Boolean(rowMatch.matched),
      rowMatch,
      rowSettle: settled.settle,
      note: "unique visible structured row exact-matching NamespaceName; hidden DOM, page chrome and wrapper candidates excluded",
      reason: rowMatch.matched ? null
        : (rowMatch.readFailed ? "row_read_failed"
          : (rowMatch.ambiguous ? "ambiguous_visible_rows" : "no_unique_visible_row")),
    };
  }
  if (isCrRepositoryType(reference)) {
    const context = crRepositoryContextCheck(finalUrl, reference);
    const matched = Boolean(signalsClear && context.matched);
    return {
      ...base,
      flow: CR_REPOSITORY_FLOW,
      context,
      exactName: crExactName(reference),
      matched,
      note: "exact detail route: all four URL parent-context segments must match the source; same-text on the page is never sufficient",
      reason: !signalsClear ? "page_signals_present" : (matched ? null : context.reason),
    };
  }
  return null;
}

function classifyFailure({ actualUiClick, expectedNavigation, actualNavigation, newTabObserved, signals,
  routeMatched, loginPassed, resourceMatched, popoverVisible, timedOut }) {
  if (!actualUiClick) return "ui_link_missing";
  if (popoverVisible) return "parent_context_missing";
  if (timedOut) return "navigation_timeout";
  if (expectedNavigation === "new_tab" && (actualNavigation !== "new_tab" || !newTabObserved)) {
    return "new_tab_not_observed";
  }
  if (expectedNavigation === "iframe" && actualNavigation !== "iframe") return "inconclusive";
  if (signals.wrongAccount) return "wrong_account";
  if (signals.wrongVariant) return "wrong_edition_or_variant";
  if (signals.login) return "login_error";
  if (signals.permission) return "permission_denied";
  if (!loginPassed) return "login_error";
  if (signals.gateway) return "network_or_gateway";
  if (signals.missing) return "resource_absent";
  if (signals.routeError || !routeMatched) return "route_error";
  if (!resourceMatched) return "inconclusive";
  return null;
}

async function activeIacPage(browser, appOrigin, requestedSessionId) {
  const candidates = browser.contexts().flatMap((context) => context.pages())
    .filter((page) => page.url().startsWith(appOrigin));
  const matches = [];
  for (const page of candidates) {
    if (!await page.locator("#iac-code-web-root").count()) continue;
    if (requestedSessionId) {
      const sessionItem = page.locator(`.session-item[data-session-id="${requestedSessionId}"]`).first();
      if (!await sessionItem.count()) continue;
      // Some Web builds keep the selected session in the DOM without adding
      // the historical .is-active class.  The explicit session item is still
      // a positive identity check; the caller has already selected this
      // session through the visible Web UI.
    }
    matches.push(page);
  }
  if (matches.length === 1) return matches[0];
  if (matches.length === 0) {
    throw new Error(`no iac-code Web tab found at ${appOrigin}${requestedSessionId ? ` for session ${requestedSessionId}` : ""}`);
  }
  throw new Error("multiple iac-code Web tabs match; pass --session-id to select the intended active session");
}

async function activeSessionId(page, requestedSessionId) {
  if (requestedSessionId) {
    const sessionItem = page.locator(`.session-item[data-session-id="${requestedSessionId}"]`).first();
    await sessionItem.waitFor({ state: "attached", timeout: 10000 });
    await sessionItem.scrollIntoViewIfNeeded();
    await sessionItem.click();
    await page.waitForFunction(({ sessionId }) => {
      const active = document.querySelector(`.session-item.is-active[data-session-id="${sessionId}"]`);
      return Boolean(active);
    }, { sessionId: requestedSessionId }, { timeout: 10000 }).catch(() => {});
    return requestedSessionId;
  }
  const active = page.locator(".session-item.is-active[data-session-id]").first();
  await active.waitFor({ state: "attached", timeout: 10000 });
  const value = await active.getAttribute("data-session-id");
  if (!value) throw new Error("active iac-code session has no session ID");
  return value;
}

async function resourceReferences(page, sessionId) {
  return page.evaluate(async ({ sessionId }) => {
    const api = await import("/static/js/api.js?v=resource-references-v12");
    const result = [];
    let cursor = null;
    do {
      const payload = await api.getResourceReferences(sessionId, cursor);
      result.push(...(payload.items || []));
      cursor = payload.nextCursor || null;
    } while (cursor);
    return result;
  }, { sessionId });
}

function linkMatchesReference(href, reference) {
  if (!["ALIYUN::NAS::AccessRule", "alicloud_nas_access_rule"].includes(reference?.resourceType)) return true;
  const { AccessGroupName: group, FileSystemType: fileType, AccessGroupType: groupType } = reference.attributes || {};
  if (!group || !fileType || !groupType || !reference.regionId) return false;
  try {
    const url = new URL(href);
    const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    return url.protocol === "https:" && url.hostname === "nasnext.console.aliyun.com" &&
      parts.length === 8 && parts[0] === reference.regionId && parts[1] === "access-group" &&
      parts[2] === fileType && parts[3] === "detail" && parts[4] === fileType &&
      parts[5] === group && parts[6] === groupType && parts[7] === "rule";
  } catch { return false; }
}

async function revealReferenceLink(page, reference, deadline) {
  // Closed <details> can retain nonempty rectangles while being excluded from
  // the accessibility tree. Expand only the exact product anchor's ancestors
  // through real summary clicks; never mutate open/hidden state or credit them
  // as resource clicks. A short child ID must keep its parent-scoped URL.
  const anchors = page.locator(`a.resource-link[aria-label=${JSON.stringify(reference.identifier)}]`);
  for (let index = 0; index < await anchors.count(); index += 1) {
    const anchor = anchors.nth(index);
    if (!linkMatchesReference(await anchor.getAttribute("href"), reference)) continue;
    for (let depth = 0; depth < 8 && Date.now() < deadline; depth += 1) {
      const closed = anchor.locator("xpath=ancestor::details[not(@open)]");
      if (!await closed.count()) break;
      const summary = closed.first().locator(":scope > summary");
      if (!await summary.isVisible()) break;
      await summary.click({ timeout: Math.max(1, deadline - Date.now()) });
    }
    if (await visibleLink(page, reference.identifier, reference)) return true;
  }
  return false;
}

function isStructuredResourceAnchor(element, identifier) {
  // Model-written Markdown links with the same name are not product resource
  // references. Closed details may retain rectangles: exclude them explicitly.
  return element.matches("a.resource-link[data-resource-link]") &&
    element.getAttribute("aria-label") === identifier && !element.closest("details:not([open])");
}

function webReplyCompleted(button) {
  return Boolean(button) && !button.classList.contains("is-stopping");
}

async function waitForWebReplyCompleted(page, sessionId, timeoutMs) {
  // UI readiness can precede a final streaming update. Wait within the existing
  // budget, after selecting the exact session, and never mask evaluation errors
  // as "still streaming". This gate cannot produce a resource-click receipt.
  const completed = ({ sessionId }) => {
    const active = document.querySelector(".session-item.is-active[data-session-id]");
    const button = document.querySelector("[data-app-shell=composer-send]");
    return active?.getAttribute("data-session-id") === sessionId &&
      Boolean(button) && !button.classList.contains("is-stopping");
  };
  const deadline = Date.now() + Math.min(timeoutMs, 15000);
  try {
    while (Date.now() < deadline) {
      // Playwright polls the immediate return value. An async predicate returns
      // a truthy Promise even when it resolves to false, ending polling early.
      const handle = await page.waitForFunction(completed, { sessionId }, {
        timeout: Math.max(1, deadline - Date.now()), polling: 100,
      });
      let ready;
      try { ready = await handle.jsonValue(); } finally { await handle.dispose(); }
      if (ready !== true) return false;
      if (deadline - Date.now() < 1200) return false;
      await page.waitForTimeout(1200);
      if (await page.evaluate(completed, { sessionId }) === true) return true;
    }
    return false;
  } catch (error) {
    if (error?.name === "TimeoutError") return false;
    throw new Error("formal Web reply readiness could not be observed", { cause: error });
  }
}

async function webReplyReadinessSnapshot(page, sessionId) {
  return page.evaluate(({ sessionId }) => {
    const active = document.querySelector(".session-item.is-active[data-session-id]");
    const buttons = [...document.querySelectorAll("[data-app-shell=composer-send]")];
    return {
      capturedAt: new Date().toISOString(),
      origin: location.origin,
      exactSessionActive: active?.getAttribute("data-session-id") === sessionId,
      activeSessionCount: document.querySelectorAll(".session-item.is-active[data-session-id]").length,
      composerButtonCount: buttons.length,
      stopping: buttons.length === 1 ? buttons[0].classList.contains("is-stopping") : null,
      scope: "readiness_only_not_click_evidence",
    };
  }, { sessionId });
}

async function visibleLink(page, identifier, reference = null) {
  const links = page.getByRole("link", { name: identifier, exact: true });
  const visible = [];
  for (let index = 0; index < await links.count(); index += 1) {
    const link = links.nth(index);
    if (await link.evaluate(isStructuredResourceAnchor, identifier) && await link.isVisible() &&
        linkMatchesReference(await link.getAttribute("href"), reference)) visible.push(link);
  }
  if (!visible.length) return null;
  const hrefs = new Set(await Promise.all(visible.map((link) => link.getAttribute("href"))));
  if (hrefs.size !== 1) throw new Error(`ambiguous visible links for resource identifier ${identifier}`);
  return visible[0];
}

async function visiblePendingButton(page, identifier) {
  const buttons = page.getByRole("button", { name: identifier, exact: true });
  for (let index = 0; index < await buttons.count(); index += 1) {
    const button = buttons.nth(index);
    if (!await button.isVisible().catch(() => false)) continue;
    const pending = await button.evaluate((element) => element.classList.contains("resource-link-pending"))
      .catch(() => false);
    if (pending) return button;
  }
  return null;
}

async function resolvePendingLink(page, identifier, deadline, reference = null) {
  const pending = await visiblePendingButton(page, identifier);
  if (!pending) return null;
  await pending.click();
  const dialog = page.locator(".resource-link-popover").first();
  await dialog.waitFor({ state: "visible", timeout: Math.max(1, deadline - Date.now()) }).catch(() => {});
  const lookup = dialog.locator('[data-resource-query="true"]').first();
  if (!await lookup.isVisible().catch(() => false)) {
    await dialog.locator(".resource-link-popover-close").click().catch(() => {});
    return null;
  }
  await lookup.click();
  let resolved = null;
  while (Date.now() < deadline) {
    resolved = await visibleLink(page, identifier, reference);
    if (resolved) break;
    const consoleTarget = dialog.locator('a:not([hidden])').first();
    if (await consoleTarget.isVisible().catch(() => false)) break;
    await sleep(100);
  }
  await dialog.locator(".resource-link-popover-close").click().catch(() => {});
  await dialog.waitFor({ state: "detached", timeout: 1000 }).catch(() => {});
  while (!resolved && Date.now() < deadline) {
    resolved = await visibleLink(page, identifier, reference);
    if (!resolved) await sleep(100);
  }
  return resolved;
}

function sourceMatches(source, product, action) {
  return source && source.product === product && source.action === action;
}

function routeReferenceRegionId(reference, item) {
  if (reference?.regionId) return reference.regionId;
  // Account-scoped resources have no physical region. The route receipt
  // records the exact API request context, not a fabricated resource region.
  // Product/action and request region must belong to the SAME source event.
  if (reference?.scopeKind !== "account" || reference.regionId != null ||
      !item.routeSourceBinding?.regionId || !item.product || !item.action) return null;
  return (reference.sources || []).find((source) =>
    sourceMatches(source, item.product, item.action) &&
    source.requestRegionId === item.routeSourceBinding.regionId)?.requestRegionId || null;
}

function queryReferenceRegionId(reference, item, source) {
  return reference.regionId || (item.querySourceBinding?.regionId == null ? null : source?.requestRegionId || null);
}

function queryReferenceSource(reference, item) {
  return (reference.sources || []).find((source) => sourceMatches(source, item.product, item.action) &&
    queryReferenceRegionId(reference, item, source) === item.querySourceBinding?.regionId &&
    Object.entries(item.querySourceBinding?.source || {}).every(([key, value]) => source[key] === value));
}

function referenceMatchesPlan(reference, item, sessionId) {
  const directApi = ["query_api", "create_api"].includes(item.receiptSource);
  const cloudRoute = item.receiptSource === "route_catalog" && item.actualApiCalled === true &&
    item.sampleLevel === "L2" && item.contractId === `route_catalog:${item.sourceType}:aliyun` &&
    item.routeSourceBinding;
  // Direct API facts may retain the Terraform source alias even when the
  // matrix names its ROS canonical identity. Both identities are reviewed in
  // the plan; provenance must still match the exact product/action below.
  const typeMatches = directApi || cloudRoute
    ? [item.sourceType, item.canonicalResourceType].includes(reference?.resourceType)
    : reference?.resourceType === item.sourceType;
  if (!reference || reference.consolePartition !== "aliyun" || !reference.accountScope ||
      reference.status === "DELETE_COMPLETE" || !typeMatches) return false;
  if ((item.excludedSampleContexts || []).some((sample) =>
    sample.sessionIdDigest === sha256(sessionId) &&
    sample.accountScopeDigest === sha256(reference.accountScope) &&
    sample.resourceIdentifierDigest === sha256(reference.identifier) &&
    sample.regionId === (reference.regionId || null) &&
    sample.sourceEvidenceDigest === item.sourceEvidenceDigest)) return false;
  const sources = Array.isArray(reference.sources) ? reference.sources : [];
  if (cloudRoute) {
    return routeReferenceRegionId(reference, item) === item.routeSourceBinding.regionId &&
      item.routeSourceBinding.resourceIdentifierDigests.includes(sha256(reference.identifier)) &&
      Object.entries(item.routeSourceBinding.parentAttributes || {}).every(
        ([field, value]) => reference.attributes?.[field] === value) &&
      Boolean(item.product && item.action && sources.some((source) => sourceMatches(source, item.product, item.action)));
  }
  if (directApi) {
    if (item.receiptSource === "query_api") {
      const binding = item.querySourceBinding;
      if (!binding || binding.resourceIdentifierDigest !== sha256(reference.identifier) ||
          (binding.sessionIdDigest && binding.sessionIdDigest !== sha256(sessionId)) ||
          (binding.accountScopeDigest && binding.accountScopeDigest !== sha256(reference.accountScope))) return false;
      return Boolean(queryReferenceSource(reference, item));
    }
    return Boolean(item.product && item.action && sources.some((source) => sourceMatches(source, item.product, item.action)));
  }
  if (item.receiptSource === "ros_stack") {
    return sources.some((source) => sourceMatches(source, "ros", "GetStackResource") ||
      sourceMatches(source, "ros", "ListStackResources"));
  }
  // An L1 route contract has no independently replayable Web provenance. Do
  // not borrow a same-type query or ROS fact and accidentally credit it.
  return false;
}

async function selectReference(page, references, item, sessionId, deadline) {
  const candidates = references.filter((reference) => referenceMatchesPlan(reference, item, sessionId));
  for (const reference of candidates) {
    let link = await visibleLink(page, reference.identifier, reference);
    if (!link && await revealReferenceLink(page, reference, deadline)) {
      link = await visibleLink(page, reference.identifier, reference);
    }
    if (link) return { reference, link };
    const resolved = await resolvePendingLink(page, reference.identifier, deadline, reference);
    if (resolved) {
      const refreshed = await resourceReferences(page, sessionId);
      return {
        reference: refreshed.find((candidate) => candidate.referenceId === reference.referenceId) || reference,
        link: resolved,
      };
    }
  }
  return { reference: candidates[0] || null, link: null };
}

async function expectedOrdinaryNavigation(page, sessionId, item, reference, link) {
  if (!link) throw new Error(`active session has no resolved UI link for ${item.canonicalResourceType}`);
  const [capability, settings] = await Promise.all([
    page.evaluate(async ({ sessionId }) => {
      const api = await import("/static/js/api.js?v=resource-references-v12");
      return api.getResourceConsoleSigninCapability(sessionId);
    }, { sessionId }),
    page.evaluate(async () => {
      const api = await import("/static/js/api.js?v=resource-references-v12");
      return api.getConsoleSigninRole();
    }),
  ]);
  const href = await link.getAttribute("href");
  const embeddedNavigationReason = settings.roleArn ? "configured_signin_role" : "credential_role";
  const supportsEmbedded = await page.evaluate(async ({ sourceType, reference, href }) => {
    const preview = await import("/static/js/components/resource_link_preview.js?v=resource-links-v70");
    const rules = await import("/static/js/resource_link_rules.js?v=resource-links-v58");
    const rule = rules.RESOURCE_LINK_RULES.find((candidate) =>
      candidate.releaseStatus === "verified" && candidate.partition === "aliyun" &&
      candidate.sourceType === sourceType);
    if (!rule || !href) return false;
    return preview.supportsEmbeddedSignin({ ruleId: rule.ruleId, url: href, reference }, rules.RESOURCE_LINK_RULES);
  }, { sourceType: item.sourceType, reference, href });
  if (capability.openMode === "embedded" && supportsEmbedded) {
    return {
      expectedNavigation: "iframe",
      expectedNavigationReason: embeddedNavigationReason,
      embeddedNavigationReason,
      expectedUrl: href,
    };
  }
  return {
    expectedNavigation: "new_tab",
    expectedNavigationReason: capability.openMode === "embedded" && !supportsEmbedded
      ? "product_external_only" : "external_default",
    embeddedNavigationReason,
    expectedUrl: href,
  };
}

function effectiveNavigationExpectation({ navigation, expected, actualNavigation, routeMatched,
  loginPassed, resourceMatched }) {
  // The visible link resolution may contain row-level parent attributes that
  // are intentionally absent from the server-projected base reference.  In
  // that case the driver's conservative pre-click reconstruction can call a
  // product external-only even though the real frontend proves that the same
  // destination is safely embeddable.  Accept that frontend decision only
  // after the iframe itself proves the exact route, login and resource.
  if (navigation === "normal" && expected.expectedNavigationReason === "product_external_only" &&
      actualNavigation === "iframe" && routeMatched && loginPassed && resourceMatched) {
    return {
      expectedNavigation: "iframe",
      expectedNavigationReason: expected.embeddedNavigationReason || "credential_role",
    };
  }
  return {
    expectedNavigation: navigation === "shift" ? "new_tab" : expected.expectedNavigation,
    expectedNavigationReason: navigation === "shift" ? "shift_override" : expected.expectedNavigationReason,
  };
}

async function waitForActivation(page, context, beforePages, deadline) {
  const panel = page.locator(".resource-link-preview").first();
  const popover = page.locator(".resource-link-popover").first();
  while (Date.now() < deadline) {
    if (await panel.isVisible().catch(() => false)) return { actualNavigation: "iframe", panel };
    let opened = null;
    for (const candidate of context.pages()) {
      if (beforePages.has(candidate) || candidate.isClosed()) continue;
      if (await candidate.opener().catch(() => null) === page) {
        opened = candidate;
        break;
      }
    }
    if (opened) {
      // Embedded sign-in can briefly open a first-party bootstrap window
      // before the preview panel is attached. Prefer the panel when it
      // appears; otherwise the stable page is the real external navigation.
      const grace = Math.min(deadline, Date.now() + 1500);
      while (Date.now() < grace) {
        if (await panel.isVisible().catch(() => false)) return { actualNavigation: "iframe", panel };
        await sleep(50);
      }
      return { actualNavigation: "new_tab", opened };
    }
    if (await popover.isVisible().catch(() => false)) return { actualNavigation: "none", popoverVisible: true };
    await sleep(50);
  }
  return {
    actualNavigation: "none",
    popoverVisible: await popover.isVisible().catch(() => false),
    timedOut: true,
  };
}

function visibleLoadingNode(node, style) {
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
}

function landingContentReady(bodyText, visibleLoading, reference = null) {
  const text = String(bodyText || "").trim();
  if (visibleLoading || text.length < 32 || /(?:^|\n)\s*(?:loading|加载中)[.。…\s]*$/im.test(text)) return false;
  if (isPolardbAccount(reference)) return polardbAccountContentReady(text);
  if (isPolardbEndpoint(reference)) {
    // Parent readiness only permits read-only inspection of connection cards;
    // it never establishes the identity of the child endpoint/address.
    return polardbClusterContentReady(text, { resourceType: "ALIYUN::POLARDB::DBCluster",
      identifier: reference.attributes?.DBClusterId, regionId: reference.regionId });
  }
  if (["ALIYUN::POLARDB::DBCluster", "alicloud_polardb_cluster"].includes(reference?.resourceType)) {
    return polardbClusterContentReady(text, reference);
  }
  // MongoDB renders its instance heading and the whole navigation sidebar
  // before the details API returns.  Neither the URL nor that shell proves a
  // usable landing: require fields from the loaded basic-information panel.
  if (reference && reference.resourceType === "ALIYUN::MONGODB::Instance") {
    return containsResourceIdentity(text, reference) &&
      /网络类型|Network\s+Type/i.test(text) && /实例状态|Instance\s+Status/i.test(text);
  }
  return true;
}

async function stablePageResult(surface, deadline, reference = null) {
  let previous = null;
  let stableReads = 0;
  let latest = { finalUrl: safeUrl(surface.url()) ? surface.url() : null, bodyText: "", contentReady: false, surface };
  while (Date.now() < deadline) {
    const finalUrl = safeUrl(surface.url()) ? surface.url() : null;
    const bodyText = await surface.locator("body").innerText({ timeout: 1000 }).catch(() => "");
    // A console iframe may expose its final route before the SPA has rendered
    // anything useful.  Treating that URL alone as a successful landing can
    // accept a visibly blank panel, so wait for a minimally substantive body.
    const visibleLoading = await surface.locator(
      '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
    ).evaluateAll(nodes => nodes.some(node => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    })).catch(() => true);
    // Tablestore's shell can retain Loading... while the exact instance card
    // is fully rendered. Still reject an intersecting spinner/overlay, a
    // sidebar-only shell, or any incomplete/wrong instance-information card.
    const otsState = isOtsInstance(reference) ? await otsInstanceSurfaceState(surface, reference)
      : isOtsTable(reference) ? await otsTableSurfaceState(surface, reference) : null;
    const databaseState = isPolardbDatabase(reference)
      ? await polardbDatabaseLanding(surface, finalUrl, reference) : null;
    const groupState = isResourceGroup(reference) ? await resourceGroupSurfaceState(surface, reference) : null;
    const contentReady = databaseState ? !visibleLoading && databaseState.matched
      : groupState ? groupState.contentReady
      : otsState ? otsState.contentReady : landingContentReady(bodyText, visibleLoading, reference);
    const fingerprint = sha256(`${finalUrl || ""}\n${bodyText}`);
    latest = { finalUrl, bodyText, contentReady, surface };
    stableReads = fingerprint === previous ? stableReads + 1 : 0;
    previous = fingerprint;
    if (finalUrl && contentReady && stableReads >= 2) break;
    await sleep(100);
  }
  return latest;
}

async function iframeResult(panel, deadline, reference) {
  const iframe = panel.locator("iframe").first();
  let frame = null;
  while (Date.now() < deadline) {
    const handle = await iframe.elementHandle().catch(() => null);
    frame = handle ? await handle.contentFrame().catch(() => null) : null;
    if (frame && safeUrl(frame.url())) break;
    const status = await panel.locator(".resource-link-preview-status").textContent().catch(() => "");
    if (status && !/loading|加载/i.test(status)) break;
    await sleep(75);
  }
  if (!frame || !safeUrl(frame.url())) {
    return {
      finalUrl: null,
      bodyText: await panel.textContent().catch(() => ""),
      contentReady: false,
      surface: panel.page(),
      interactionSurface: null,
    };
  }
  await frame.waitForLoadState("domcontentloaded", { timeout: Math.max(1, deadline - Date.now()) }).catch(() => {});
  const result = await stablePageResult(frame, deadline, reference);
  return { ...result, surface: panel.page(), interactionSurface: frame };
}

async function newTabResult(opened, deadline, reference) {
  while (Date.now() < deadline && !safeUrl(opened.url())) await sleep(75);
  await opened.waitForLoadState("domcontentloaded", { timeout: Math.max(1, deadline - Date.now()) }).catch(() => {});
  return { ...(await stablePageResult(opened, deadline, reference)), interactionSurface: opened };
}

async function captureObservation({ page, context, item, reference, link, navigation, expected, attemptCount,
  artifactsDir, timeoutMs, sequence, planId }) {
  const beforePages = new Set(context.pages());
  const deadline = Date.now() + timeoutMs;
  let actualUiClick = false;
  let activation = { actualNavigation: "none", popoverVisible: false };
  if (link) {
    try {
      await link.scrollIntoViewIfNeeded();
      await link.click(navigation === "shift" ? { modifiers: ["Shift"] } : {});
      actualUiClick = true;
      activation = await waitForActivation(page, context, beforePages, deadline);
    } catch (_error) {
      activation = { actualNavigation: "none", popoverVisible: false };
    }
  }
  const result = activation.actualNavigation === "iframe"
    ? await iframeResult(activation.panel, deadline, reference)
    : activation.actualNavigation === "new_tab"
      ? await newTabResult(activation.opened, deadline, reference)
      : {
          finalUrl: null,
          bodyText: await page.locator("body").innerText().catch(() => ""),
          contentReady: false,
          surface: page,
          interactionSurface: page,
        };
  const expectedUrl = safeUrl(expected.expectedUrl);
  let finalTarget = safeUrl(result.finalUrl);
  let bodyText = result.bodyText;
  let signals = pageSignals(result.finalUrl, bodyText);
  const cmsSurface = cmsFlowApplies(reference, result.finalUrl);
  // CR namespace/repository landings never render the identifier, and matching
  // them by generic body text is unsafe (a namespace name also appears in
  // repository routes and breadcrumbs).  Their verdict comes only from the
  // dedicated CR flow below.
  const crType = isCrType(reference);
  const crSurface = crFlowApplies(reference, result.finalUrl);
  const exactIdentifier = Boolean(finalTarget) &&
    (containsResourceIdentity(finalTarget.href, reference) ||
      ((cmsSurface || crType) ? false : containsResourceIdentity(bodyText, reference)));
  const baseRouteMatched = Boolean(
    finalTarget && expectedUrl && !Object.values(signals).some(Boolean) &&
    normalizeConsoleHost(finalTarget.hostname) === normalizeConsoleHost(expectedUrl.hostname),
  );
  const loginPassed = Boolean(finalTarget) && Boolean(result.contentReady) && !signals.login;
  let search = {
    attempted: false,
    performed: false,
    control: null,
    query: null,
    resultText: null,
    resourceMatched: false,
    finalUrl: null,
  };
  if (baseRouteMatched && loginPassed && !exactIdentifier && !cmsSurface && !isNasMountTarget(reference) &&
      !isVbrRouterInterface(reference) &&
      !isSlbListener(reference) &&
      !childListKind(reference) &&
      !isAccountResourceLanding(reference) &&
      !isPolardbAccount(reference) &&
      !isPolardbDatabase(reference) &&
      !isPolardbEndpoint(reference) &&
      !crSuppressesSearchFallback(reference)) {
    search = await searchVisibleResource(result.interactionSurface, reference, deadline);
    if (search.performed) {
      const searched = await stablePageResult(result.interactionSurface, deadline);
      search.finalUrl = searched.finalUrl;
      finalTarget = safeUrl(resolveSearchFinalUrl(search, searched.finalUrl));
      bodyText = searched.bodyText;
      signals = pageSignals(searched.finalUrl, bodyText);
    }
  }
  let cms = null;
  if (baseRouteMatched && loginPassed && cmsSurface) {
    // Restore of the account-level display switch is guaranteed inside
    // cmsContactToggleFlow's own try/finally, so an exception here can never
    // skip it; the outer layer only records the failure.
    try {
      cms = await cmsLocateResource(result.interactionSurface, reference, deadline);
      if (cms.bodyTextAfter) bodyText = cms.bodyTextAfter;
    } catch (error) {
      cms = {
        flow: isCmsContactGroupType(reference) ? "contact_group_tab" : "contact_masking_switch",
        matched: false,
        tab: null,
        masking: null,
        rowMatch: null,
        rowSettle: null,
        bodyTextAfter: null,
        error: String((error && error.message) || error).slice(0, 200),
      };
    }
  }
  let cr = null;
  if (baseRouteMatched && loginPassed && crSurface) {
    try {
      cr = await crLocateResource(result.interactionSurface, reference,
        finalTarget ? finalTarget.href : null, signals, deadline);
    } catch (error) {
      cr = {
        flow: isCrNamespaceType(reference) ? CR_NAMESPACE_FLOW : CR_REPOSITORY_FLOW,
        matched: false,
        signalsClear: false,
        searchFallbackSuppressed: true,
        context: null,
        exactName: crExactName(reference),
        rowMatch: null,
        rowSettle: null,
        note: null,
        reason: "locator_error",
        error: String((error && error.message) || error).slice(0, 200),
      };
    }
  }
  const cmsRestoreFailed = Boolean(cms && cms.masking &&
    cmsRestoreBlocksPass(cms.masking.toggled, cms.masking.restore));
  // A CR type is matched only by its product-specific fail-closed flow; the
  // generic identifier-in-URL / identifier-in-body paths stay authoritative for
  // every other resource type.
  const nasRuleType = ["ALIYUN::NAS::AccessRule", "alicloud_nas_access_rule"].includes(reference.resourceType);
  const nasRule = nasRuleType
    ? await nasRuleLanding(result.interactionSurface, finalTarget?.href, expected.expectedUrl, reference) : null;
  const nasMount = isNasMountTarget(reference)
    ? await nasMountTargetLanding(result.interactionSurface, finalTarget?.href, expected.expectedUrl, reference) : null;
  const polardbAccount = isPolardbAccount(reference)
    ? await polardbAccountLanding(result.interactionSurface, finalTarget?.href, reference) : null;
  const polardbDatabase = isPolardbDatabase(reference)
    ? await polardbDatabaseLanding(result.interactionSurface, finalTarget?.href, reference) : null;
  const polardbEndpoint = isPolardbEndpoint(reference)
    ? await polardbEndpointLanding(baseRouteMatched && loginPassed ? result.interactionSurface : null,
      finalTarget?.href, reference, deadline) : null;
  const accountResource = isAccountResourceLanding(reference)
    ? await accountResourceLanding(result.interactionSurface, finalTarget?.href, expected.expectedUrl, reference) : null;
  const routerInterface = isVbrRouterInterface(reference)
    ? await routerInterfaceLanding(baseRouteMatched && loginPassed ? result.interactionSurface : null,
      finalTarget?.href, reference, deadline) : null;
  if (routerInterface?.matched) {
    finalTarget = safeUrl(routerInterface.finalUrl);
    bodyText = routerInterface.bodyTextAfter;
  }
  const childList = childListKind(reference)
    ? await childListLanding(baseRouteMatched && loginPassed ? result.interactionSurface : null,
      finalTarget?.href, reference) : null;
  const slbListener = isSlbListener(reference)
    ? await slbListenerLanding(baseRouteMatched && loginPassed ? result.interactionSurface : null,
      finalTarget?.href, reference, deadline) : null;
  if (slbListener?.matched) bodyText = slbListener.bodyTextAfter;
  const resourceMatched = Boolean(finalTarget) && !cmsRestoreFailed &&
    (slbListener ? Boolean(slbListener.matched) : childList ? Boolean(childList.matched) : routerInterface ? Boolean(routerInterface.matched) : accountResource ? Boolean(accountResource.matched) : polardbEndpoint ? Boolean(polardbEndpoint.matched) : polardbDatabase ? Boolean(polardbDatabase.matched) : polardbAccount ? Boolean(polardbAccount.matched) : nasMount ? Boolean(nasMount.matched) : nasRuleType ? Boolean(nasRule?.matched) : crType
      ? (Boolean(crSurface) && Boolean(cr && cr.matched))
      : (containsResourceIdentity(finalTarget.href, reference) ||
        (cmsSurface
          ? Boolean(cms && cms.matched)
          : (search.performed ? search.resourceMatched : containsResourceIdentity(bodyText, reference)))));
  const routeMatched = Boolean(
    baseRouteMatched && !Object.values(signals).some(Boolean) && resourceMatched,
  );
  const effectiveExpectation = effectiveNavigationExpectation({
    navigation,
    expected,
    actualNavigation: activation.actualNavigation,
    routeMatched,
    loginPassed,
    resourceMatched,
  });
  const expectedNavigation = effectiveExpectation.expectedNavigation;
  const newTabObserved = activation.actualNavigation === "new_tab";
  let failureReason = classifyFailure({
    actualUiClick,
    expectedNavigation,
    actualNavigation: activation.actualNavigation,
    newTabObserved,
    signals,
    routeMatched,
    loginPassed,
    resourceMatched,
    popoverVisible: Boolean(activation.popoverVisible),
    timedOut: Boolean(activation.timedOut),
  });
  if (cmsRestoreFailed) failureReason = "cms_masking_restore_failed";
  // A spinning SAE app is inconclusive rendering, not proof of logged-out UI.
  if (["ALIYUN::SAE::Application", "alicloud_sae_application"].includes(reference.resourceType) &&
      !result.contentReady && !Object.values(signals).some(Boolean)) failureReason = "inconclusive";
  const landingKind = accountResource?.matched && routeMatched
    ? (reference.resourceType.includes("Group") || reference.resourceType === "alicloud_ram_group"
      ? (navigation === "normal" ? "external_only" : "exact_detail") : "parent_list")
    : (slbListener?.matched || childList?.matched || routerInterface?.matched || nasMount?.matched || polardbAccount?.matched || polardbDatabase?.matched || polardbEndpoint?.matched) && routeMatched ? "parent_list" : classifyLanding({
    navigation,
    expectedNavigation,
    finalUrl: finalTarget ? finalTarget.href : null,
    bodyText,
    reference,
    routeMatched,
    searchPerformed: search.performed,
    cmsLocated: Boolean(cms && cms.matched),
    crLocated: Boolean(cr && cr.matched),
    crLandingKind: cr ? crLandingKind(reference) : null,
  });
  const slug = `${planId.slice(0, 12)}-${String(sequence).padStart(2, "0")}-${item.sourceType.replace(/[^a-z0-9]+/gi, "-")}-${navigation}`;
  const screenshotPath = path.resolve(artifactsDir, `${slug}.png`);
  const screenshotOmitted = cmsScreenshotPolicy(cms ? cms.flow : null);
  if (!screenshotOmitted) {
    await result.surface.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
  }
  const raw = {
    capturedAt: isoNow(),
    sourceType: item.sourceType,
    canonicalResourceType: item.canonicalResourceType,
    source: item.receiptSource,
    contractId: item.contractId,
    navigation,
    resourceIdentifier: reference.identifier,
    ...(item.querySourceBinding?.source ? {querySource: Object.fromEntries(
      Object.keys(item.querySourceBinding.source).map((key) => [key,
        queryReferenceSource(reference, item)?.[key]]),
    )} : {}),
    ...(item.routeSourceBinding?.parentAttributes
      ? { resourceParentAttributes: { ...item.routeSourceBinding.parentAttributes } } : {}),
    regionId: item.receiptSource === "route_catalog"
      ? routeReferenceRegionId(reference, item) : reference.regionId ||
        (item.receiptSource === "query_api" ? queryReferenceRegionId(reference, item,
          queryReferenceSource(reference, item)) : null),
    expectedNavigation,
    expectedNavigationReason: effectiveExpectation.expectedNavigationReason,
    actualUiClick,
    actualNavigation: activation.actualNavigation,
    newTabObserved,
    landingKind,
    routeMatched,
    loginPassed,
    resourceMatched,
    failureReason: failureReason || (landingKind === "unknown" ? "inconclusive" : null),
    attemptCount,
    finalUrl: finalTarget ? finalTarget.href : null,
    diagnostics: {
      expectedUrl: expected.expectedUrl,
      pageTitle: await result.surface.title().catch(() => ""),
      bodyExcerpt: String(bodyText || "").slice(0, 2000),
      signals,
      search,
      ...(nasRule ? { nasRule } : {}),
      ...(accountResource ? { accountResource } : {}),
      ...(routerInterface ? { routerInterface } : {}),
      ...(slbListener ? { slbListener } : {}),
      ...(childList ? { childList } : {}),
      ...(nasMount ? { nasMount } : {}),
      ...(polardbAccount ? { polardbAccount } : {}),
      ...(polardbEndpoint ? { polardbEndpoint } : {}),
      contentReady: Boolean(result.contentReady),
      screenshotOmitted: screenshotOmitted || null,
      cms: cms ? {
        flow: cms.flow,
        tabClick: cms.tab || null,
        masking: cms.masking ? {
          controlPresent: cms.masking.initial.present,
          initialSwitchOn: cms.masking.initial.switchOn,
          toggled: cms.masking.toggled,
          toggledVia: cms.masking.toggledVia,
          restore: cms.masking.restore || null,
          restoreFailed: cmsRestoreFailed,
          mutatedResources: cms.masking.mutatedResources,
        } : null,
        rowMatch: cms.rowMatch || null,
        rowSettle: cms.rowSettle || null,
      } : null,
      cr: cr ? {
        flow: cr.flow,
        matched: Boolean(cr.matched),
        signalsClear: Boolean(cr.signalsClear),
        searchFallbackSuppressed: Boolean(cr.searchFallbackSuppressed),
        exactName: cr.exactName || null,
        context: cr.context ? {
          hostOk: Boolean(cr.context.hostOk),
          shapeRecognized: Boolean(cr.context.shapeRecognized),
          matched: Boolean(cr.context.matched),
          route: cr.context.route || null,
          fields: (cr.context.fields || []).map((field) => ({
            name: field.name,
            ok: Boolean(field.ok),
            expected: field.expected || null,
            observed: field.observed || null,
          })),
          reason: cr.context.reason || null,
        } : null,
        rowMatch: cr.rowMatch || null,
        rowSettle: cr.rowSettle || null,
        note: cr.note || null,
        reason: cr.reason || null,
        error: cr.error || null,
      } : null,
    },
  };
  const observationPath = path.resolve(artifactsDir, `${slug}.json`);
  writeJson(observationPath, raw);
  const artifacts = [
    { kind: "observation", path: observationPath, digest: fileDigest(observationPath) },
  ];
  if (fs.existsSync(screenshotPath)) {
    artifacts.push({ kind: "screenshot", path: screenshotPath, digest: fileDigest(screenshotPath) });
  }
  // A failed embedded destination can leave the preview panel open with the
  // iframe still owning focus.  Escape is best-effort in that state and can
  // leave the next branch unable to find its original resource anchor.  Use
  // the panel's real close control as the durable cleanup path, then retain
  // Escape for popovers and older UI variants.
  const previewClose = page.locator(".resource-link-preview button").first();
  if (await previewClose.isVisible().catch(() => false)) {
    await previewClose.click().catch(() => {});
    await page.locator(".resource-link-preview").waitFor({state: "detached", timeout: 1000}).catch(() => {});
  }
  const popoverClose = page.locator(".resource-link-popover-close").first();
  if (await popoverClose.isVisible().catch(() => false)) {
    await popoverClose.click().catch(() => {});
    await page.locator(".resource-link-popover").waitFor({state: "detached", timeout: 1000}).catch(() => {});
  }
  await page.keyboard.press("Escape").catch(() => {});
  for (const candidate of context.pages()) {
    if (candidate === page || beforePages.has(candidate) || candidate.isClosed()) continue;
    if (await candidate.opener().catch(() => null) === page) await candidate.close().catch(() => {});
  }
  return { ...raw, artifacts };
}

function sameResumeSample(item, { sessionId, accountScope, reference }) {
  const resumeContext = item.resumeContext;
  if (!resumeContext) return false;
  return resumeContext.sessionIdDigest === sha256(sessionId) &&
    resumeContext.accountScopeDigest === sha256(accountScope) &&
    resumeContext.resourceIdentifierDigest === sha256(reference.identifier) &&
    resumeContext.regionId === (reference.regionId || null) &&
    resumeContext.sourceEvidenceDigest === item.sourceEvidenceDigest;
}

async function cmsSelfTestExceptionPathRestores() {
  const calls = [];
  const failing = await cmsContactToggleFlow({
    readState: async () => { calls.push("read"); return { present: true, unique: true, switchOn: true }; },
    click: async () => { calls.push("click"); return "associated-switch#0"; },
    stable: async () => { calls.push("stable"); throw new Error("stability wait exploded"); },
    rowMatch: async () => { calls.push("row"); return { matched: false, matchedRows: [], rowCount: 0 }; },
    restore: async () => { calls.push("restore"); return { attempted: true, verified: false, clicks: 1 }; },
  }, { identifier: "sin-x" });
  return calls.includes("restore") && failing.matched === false && failing.masking.restore !== null;
}

async function cmsSelfTestVerifiedRestorePasses() {
  let reads = 0;
  const ok = await cmsContactToggleFlow({
    readState: async () => ({ present: true, unique: true, switchOn: true }),
    click: async () => "associated-switch#0",
    stable: async () => ({}),
    rowMatch: async () => {
      reads += 1;
      return reads === 1
        ? { matched: false, matchedRows: [], rowCount: 1 }
        : { matched: true, matchedRows: ["sin-x row"], rowCount: 1 };
    },
    restore: async () => ({ attempted: true, verified: true, clicks: 1 }),
  }, { identifier: "sin-x" });
  return ok.matched === true && ok.masking.toggled === true &&
    ok.masking.restore !== null && ok.masking.restore.verified === true;
}

async function cmsSelfTestNoToggleNoRestore() {
  const untouched = await cmsContactToggleFlow({
    readState: async () => ({ present: true, unique: true, switchOn: true }),
    click: async () => { throw new Error("must not be called"); },
    stable: async () => ({}),
    rowMatch: async () => ({ matched: true, matchedRows: ["sin-x row"], rowCount: 1 }),
    restore: async () => { throw new Error("must not be called"); },
  }, { identifier: "sin-x" });
  return untouched.matched === true && untouched.masking.toggled === false && untouched.masking.restore === null;
}

// Settle poll: a hit on the first probe stops immediately with zero waits.
async function cmsSelfTestSettleImmediateHit() {
  let probes = 0;
  const sleeps = [];
  const { row, settle } = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => { probes += 1; return { matched: true, matchedRows: ["cms-test row"], rowCount: 10 }; },
      sleep: async (ms) => { sleeps.push(ms); },
      clock: () => 1000,
    },
    { identifier: "cms-test", deadline: 1000 + 60000, now: 1000 },
  );
  return probes === 1 && sleeps.length === 0 && settle.matched === true &&
    settle.attempts === 1 && settle.waitedMs === 0 && settle.timedOut === false &&
    settle.finalRowCount === 10 && row.matched === true;
}

// Settle poll: a late hit retries on the short interval and stops at the hit.
async function cmsSelfTestSettleLateHit() {
  let probes = 0;
  const sleeps = [];
  let clock = 5000;
  const { row, settle } = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => {
        probes += 1;
        return probes >= 4
          ? { matched: true, matchedRows: ["云账号报警联系人 row"], rowCount: 10 }
          : { matched: false, matchedRows: [], rowCount: 20 };
      },
      sleep: async (ms) => { sleeps.push(ms); clock += ms; },
      clock: () => clock,
    },
    { identifier: "云账号报警联系人", deadline: 5000 + 60000, now: 5000 },
  );
  return probes === 4 && sleeps.length === 3 && sleeps.every((ms) => ms === 100) &&
    settle.matched === true && settle.attempts === 4 && settle.waitedMs === 300 &&
    settle.timedOut === false && settle.finalRowCount === 10 && row.matched === true;
}

// Settle poll: the 5s product cap bounds the wait even with a huge deadline.
async function cmsSelfTestSettleCapBound() {
  let clock = 9000;
  const sleeps = [];
  const { row, settle } = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => ({ matched: false, matchedRows: [], rowCount: 20 }),
      sleep: async (ms) => { sleeps.push(ms); clock += ms; },
      clock: () => clock,
    },
    { identifier: "cms-test", deadline: 9000 + 120000, now: 9000 },
  );
  return row.matched === false && settle.timedOut === true && settle.capMs === 5000 &&
    settle.waitedMs <= 5000 && settle.waitedMs >= 4900 &&
    sleeps.every((ms) => ms > 0 && ms <= 100);
}

// Settle poll: a tight branch deadline shrinks the cap below the product cap.
async function cmsSelfTestSettleDeadlineBound() {
  let clock = 20000;
  const sleeps = [];
  const { row, settle } = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => ({ matched: false, matchedRows: [], rowCount: 20 }),
      sleep: async (ms) => { sleeps.push(ms); clock += ms; },
      clock: () => clock,
    },
    { identifier: "cms-test", deadline: 20000 + 700, now: 20000 },
  );
  return row.matched === false && settle.timedOut === true && settle.capMs === 700 &&
    settle.waitedMs <= 700 && sleeps.every((ms) => ms > 0 && ms <= 100);
}

// Settle poll: an already-passed or non-finite deadline collapses to a single
// immediate probe (safe failure) and never sleeps past the deadline.
async function cmsSelfTestSettleExpiredDeadlineSafeFails() {
  const sleeps = [];
  const expired = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => ({ matched: false, matchedRows: [], rowCount: 20 }),
      sleep: async (ms) => { sleeps.push(ms); },
      clock: () => 40000,
    },
    { identifier: "cms-test", deadline: 39000, now: 40000 },
  );
  const nonFinite = await cmsGroupRowSettlePoll(
    {
      rowMatch: async () => ({ matched: false, matchedRows: [], rowCount: 20 }),
      sleep: async (ms) => { sleeps.push(ms); },
      clock: () => 40000,
    },
    { identifier: "cms-test", deadline: Number.POSITIVE_INFINITY, now: 40000 },
  );
  return sleeps.length === 0 &&
    expired.row.matched === false && expired.settle.capMs === 0 &&
    expired.settle.attempts === 1 && expired.settle.timedOut === true &&
    nonFinite.row.matched === false && nonFinite.settle.capMs === 0 &&
    nonFinite.settle.attempts === 1 && nonFinite.settle.timedOut === true;
}

// --- CR self-test fixtures and flows (pure, no browser) -----------------------
const CR_CLEAN_SIGNALS = {
  login: false, wrongAccount: false, wrongVariant: false, permission: false,
  missing: false, gateway: false, routeError: false,
};
const CR_SELF_TEST_NAMESPACE_REFERENCE = {
  resourceType: CR_NAMESPACE_TYPE,
  identifier: "crn-x",
  regionId: "cn-beijing",
  attributes: { NamespaceId: "crn-x", InstanceId: "cri-a", NamespaceName: "test192" },
};
const CR_SELF_TEST_NAMESPACE_URL =
  "https://cr4service.console.aliyun.com/cn-beijing/instance/cri-a/namespaces";
const CR_SELF_TEST_REPOSITORY_REFERENCE = {
  resourceType: CR_REPOSITORY_TYPE,
  identifier: "crr-x",
  regionId: "cn-beijing",
  attributes: { RepoId: "crr-x", InstanceId: "cri-a", RepoNamespaceName: "test192", RepoName: "demo-repo" },
};

// A stand-in landing surface that records every interaction.  The CR flow must
// only read the DOM: any locator use, click, typing or navigation is a bug.
function crSelfTestSurface(rows) {
  const calls = [];
  return {
    calls,
    evaluate: async () => { calls.push("evaluate"); return { rows }; },
    locator: () => { calls.push("locator"); throw new Error("CR flow must not use locators"); },
    click: async () => { calls.push("click"); throw new Error("CR flow must not click"); },
    fill: async () => { calls.push("fill"); throw new Error("CR flow must not type"); },
    url: () => CR_SELF_TEST_NAMESPACE_URL,
  };
}

function crSelfTestRow(text, extra = {}) {
  return { visible: true, chrome: false, nested: false, text, top: 100, ...extra };
}

async function crSelfTestNamespaceFlows() {
  const deadline = Date.now() + 400;
  const passed = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常 关闭 仓库类型私有"), crSelfTestRow("test6182 正常 开启", { top: 140 })]),
    CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL, CR_CLEAN_SIGNALS, deadline);
  const zero = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test6182 正常 开启")]),
    CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL, CR_CLEAN_SIGNALS, deadline);
  const duplicated = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常"), crSelfTestRow("test192 正常", { top: 140 })]),
    CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL, CR_CLEAN_SIGNALS, deadline);
  const wrongRegion = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常")]),
    CR_SELF_TEST_NAMESPACE_REFERENCE,
    "https://cr.console.aliyun.com/cn-hangzhou/instance/cri-a/namespaces", CR_CLEAN_SIGNALS, deadline);
  const wrongInstance = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常")]),
    CR_SELF_TEST_NAMESPACE_REFERENCE,
    "https://cr.console.aliyun.com/cn-beijing/instance/cri-other/namespaces", CR_CLEAN_SIGNALS, deadline);
  const loginSignal = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常")]),
    CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL,
    { ...CR_CLEAN_SIGNALS, login: true }, deadline);
  const readFailed = await crLocateResource(
    { evaluate: async () => { throw new Error("frame detached"); } },
    CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL, CR_CLEAN_SIGNALS, deadline);
  const missingName = await crLocateResource(
    crSelfTestSurface([crSelfTestRow("test192 正常")]),
    { ...CR_SELF_TEST_NAMESPACE_REFERENCE, attributes: { InstanceId: "cri-a" } },
    CR_SELF_TEST_NAMESPACE_URL, CR_CLEAN_SIGNALS, deadline);
  return passed.flow === CR_NAMESPACE_FLOW && passed.matched === true &&
    passed.searchFallbackSuppressed === true && passed.reason === null &&
    passed.rowMatch.distinctMatches === 1 && passed.rowMatch.hiddenExcluded === 0 &&
    passed.rowSettle.attempts >= 1 && passed.context.matched === true &&
    zero.matched === false && zero.reason === "no_unique_visible_row" && zero.rowMatch.rowCount === 1 &&
    duplicated.matched === false && duplicated.reason === "ambiguous_visible_rows" &&
    duplicated.rowMatch.ambiguous === true &&
    wrongRegion.matched === false && wrongRegion.reason === "region" && wrongRegion.rowMatch === null &&
    wrongInstance.matched === false && wrongInstance.reason === "instanceId" &&
    loginSignal.matched === false && loginSignal.reason === "page_signals_present" &&
    loginSignal.signalsClear === false &&
    readFailed.matched === false && readFailed.reason === "row_read_failed" &&
    readFailed.rowMatch.readFailed === true &&
    missingName.matched === false && missingName.reason === "reference_namespace_name_missing" &&
    missingName.exactName === null;
}

async function crSelfTestRepositoryFlows() {
  const deadline = Date.now() + 400;
  const surface = crSelfTestSurface([]);
  const url = (region, instance, namespace, repo) =>
    `https://cr.console.aliyun.com/repository/${region}/${instance}/${namespace}/${repo}/details`;
  const passed = await crLocateResource(surface, CR_SELF_TEST_REPOSITORY_REFERENCE,
    url("cn-beijing", "cri-a", "test192", "demo-repo"), CR_CLEAN_SIGNALS, deadline);
  const wrongRepoName = await crLocateResource(surface, CR_SELF_TEST_REPOSITORY_REFERENCE,
    url("cn-beijing", "cri-a", "test192", "other-repo"), CR_CLEAN_SIGNALS, deadline);
  const missingSignal = await crLocateResource(surface, CR_SELF_TEST_REPOSITORY_REFERENCE,
    url("cn-beijing", "cri-a", "test192", "demo-repo"), { ...CR_CLEAN_SIGNALS, missing: true }, deadline);
  const listRoute = await crLocateResource(surface, CR_SELF_TEST_REPOSITORY_REFERENCE,
    "https://cr.console.aliyun.com/cn-beijing/instance/cri-a/repositories", CR_CLEAN_SIGNALS, deadline);
  return passed.flow === CR_REPOSITORY_FLOW && passed.matched === true &&
    passed.exactName === "test192/demo-repo" && passed.reason === null &&
    passed.context.fields.length === 4 && passed.context.fields.every((field) => field.ok) &&
    passed.rowMatch === null && passed.rowSettle === null &&
    wrongRepoName.matched === false && wrongRepoName.reason === "repoName" &&
    missingSignal.matched === false && missingSignal.reason === "page_signals_present" &&
    missingSignal.signalsClear === false &&
    listRoute.matched === false &&
    listRoute.reason === "repository_detail_route_not_recognized" &&
    surface.calls.length === 0;
}

async function runSelfTest() {
  const nasReference = { resourceType: "ALIYUN::NAS::AccessRule", identifier: "1", regionId: "cn-hangzhou",
    attributes: { AccessGroupName: "group-one", FileSystemType: "standard", AccessGroupType: "Classic" } };
  const nasHref = "https://nasnext.console.aliyun.com/cn-hangzhou/access-group/standard/detail/standard/group-one/Classic/rule";
  async function hiddenLinkCheck(summaryVisible) {
    let closedCount = 3;
    let summaryClicks = 0;
    const link = { isVisible: async () => closedCount === 0, getAttribute: async () => nasHref,
      evaluate: async () => closedCount === 0 };
    const anchor = { getAttribute: async () => nasHref, locator: () => ({
      count: async () => closedCount,
      first: () => ({ locator: () => ({ isVisible: async () => summaryVisible,
        click: async () => { closedCount -= 1; summaryClicks += 1; } }) }),
    }) };
    const page = { locator: () => ({ count: async () => 1, nth: () => anchor }),
      getByRole: () => ({ count: async () => closedCount ? 0 : 1, nth: () => link }) };
    const revealed = await revealReferenceLink(page, nasReference, Date.now() + 1000);
    return summaryVisible ? revealed && summaryClicks === 3 : !revealed && summaryClicks === 0;
  }
  const reference = {
    resourceType: "ALIYUN::ECS::Instance",
    consolePartition: "aliyun",
    accountScope: "account-a",
    identifier: "i-abc",
    regionId: "cn-hangzhou",
    sources: [{ product: "ecs", action: "DescribeInstances" }],
  };
  const item = {
    sourceType: reference.resourceType,
    canonicalResourceType: reference.resourceType,
    receiptSource: "query_api",
    product: "ecs",
    action: "DescribeInstances",
    sourceEvidenceDigest: "a".repeat(64),
    querySourceBinding: {resourceIdentifierDigest: sha256(reference.identifier), regionId: reference.regionId},
    excludedSampleContexts: [],
  };
  const anchorFacts = (structured, label, closed) => ({
    matches: () => structured, getAttribute: () => label, closest: () => closed ? {} : null,
  });
  const checks = [
    webReplyCompleted({ classList: { contains: () => false } }),
    !webReplyCompleted({ classList: { contains: () => true } }),
    !webReplyCompleted(null),
    isStructuredResourceAnchor(anchorFacts(true, "same-name", false), "same-name"),
    !isStructuredResourceAnchor(anchorFacts(false, "same-name", false), "same-name"),
    !isStructuredResourceAnchor(anchorFacts(true, "other-name", false), "same-name"),
    !isStructuredResourceAnchor(anchorFacts(true, "same-name", true), "same-name"),
    linkMatchesReference(nasHref, nasReference),
    !linkMatchesReference(nasHref.replace("group-one", "group-two"), nasReference),
    !linkMatchesReference(nasHref.replace("cn-hangzhou", "cn-beijing"), nasReference),
    !linkMatchesReference(nasHref.replaceAll("standard", "extreme"), nasReference),
    !linkMatchesReference(nasHref.replace("Classic", "Vpc"), nasReference),
    !linkMatchesReference(nasHref.replace("console.aliyun.com", "console.alibabacloud.com"), nasReference),
    await hiddenLinkCheck(true),
    await hiddenLinkCheck(false),
    nasRuleRowMatches({ key: "0", text: "0.0.0.0/0 100", cells: ["0.0.0.0/0", "100"] },
      { identifier: "0", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100" } }),
    !nasRuleRowMatches({ key: "1", text: "0.0.0.0/0 100", cells: ["0.0.0.0/0", "100"] },
      { identifier: "0", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100" } }),
    nasRuleRowMatches({ key: null, cells: ["0.0.0.0/0", "读写", "所有用户不匿名（no_squash）", "100"] },
      { identifier: "1", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100",
        RWAccess: "RDWR", UserAccess: "no_squash" } }),
    !nasRuleRowMatches({ key: null, cells: ["0.0.0.0/0", "只读", "所有用户不匿名（no_squash）", "100"] },
      { identifier: "1", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100",
        RWAccess: "RDWR", UserAccess: "no_squash" } }),
    !nasRuleRowMatches({ key: null, cells: ["0.0.0.0/0", "读写", "所有用户匿名（all_squash）", "100"] },
      { identifier: "1", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100",
        RWAccess: "RDWR", UserAccess: "no_squash" } }),
    !nasRuleRowMatches({ key: null, cells: ["0.0.0.0/0", "读写", "所有用户不匿名（no_squash）", "100"] },
      { identifier: "1", attributes: { SourceCidrIp: "0.0.0.0/0", Priority: "100" } }),
    referenceMatchesPlan({ ...reference, attributes: { AccessGroupName: "group-one", FileSystemType: "standard" } },
      { ...item, receiptSource: "route_catalog", actualApiCalled: true, sampleLevel: "L2",
        contractId: `route_catalog:${item.sourceType}:aliyun`,
        routeSourceBinding: { regionId: reference.regionId, resourceIdentifierDigests: [sha256(reference.identifier)],
          parentAttributes: { AccessGroupName: "group-one", FileSystemType: "standard" } } }, "session-a"),
    !referenceMatchesPlan({ ...reference, attributes: { AccessGroupName: "group-two", FileSystemType: "standard" } },
      { ...item, receiptSource: "route_catalog", actualApiCalled: true, sampleLevel: "L2",
        contractId: `route_catalog:${item.sourceType}:aliyun`,
        routeSourceBinding: { regionId: reference.regionId, resourceIdentifierDigests: [sha256(reference.identifier)],
          parentAttributes: { AccessGroupName: "group-one", FileSystemType: "standard" } } }, "session-a"),
    !landingContentReady("实例 dds-test 基本信息 账号管理 数据库连接 备份与恢复 性能趋势 监控信息", false,
      { resourceType: "ALIYUN::MONGODB::Instance", identifier: "dds-test" }),
    !landingContentReady("实例 dds-test 网络类型 专有网络 实例状态 运行中 完整基本信息", true,
      { resourceType: "ALIYUN::MONGODB::Instance", identifier: "dds-test" }),
    landingContentReady("实例 dds-test 网络类型 专有网络 实例状态 运行中 完整基本信息", false,
      { resourceType: "ALIYUN::MONGODB::Instance", identifier: "dds-test" }),
    !landingContentReady("A substantive console sidebar with many entries\nloading...", false),
    !landingContentReady("系统运维管理 OOS 自定义任务模板 auto-disable-password 模板信息 流程配置 YAML JSON", true,
      { resourceType: "ALIYUN::OOS::Template", identifier: "auto-disable-password" }),
    visibleLoadingNode({ getBoundingClientRect: () => ({ width: 200, height: 20 }) },
      { visibility: "visible", display: "block" }),
    !visibleLoadingNode({ getBoundingClientRect: () => ({ width: 0, height: 20 }) },
      { visibility: "visible", display: "block" }),
    !visibleLoadingNode({ getBoundingClientRect: () => ({ width: 200, height: 20 }) },
      { visibility: "hidden", display: "block" }),
    !visibleLoadingNode({ getBoundingClientRect: () => ({ width: 200, height: 20 }) },
      { visibility: "visible", display: "none" }),
    referenceMatchesPlan(reference, { ...item, receiptSource: "route_catalog", actualApiCalled: true,
      sampleLevel: "L2", contractId: `route_catalog:${item.sourceType}:aliyun`,
      routeSourceBinding: { regionId: reference.regionId, resourceIdentifierDigests: [sha256(reference.identifier)] } },
    "session-a"),
    !referenceMatchesPlan(reference, { ...item, receiptSource: "route_catalog", actualApiCalled: true,
      sampleLevel: "L2", contractId: `route_catalog:${item.sourceType}:aliyun`,
      routeSourceBinding: { regionId: "cn-beijing", resourceIdentifierDigests: [sha256(reference.identifier)] } },
    "session-a"),
    !referenceMatchesPlan(reference, { ...item, receiptSource: "route_catalog", actualApiCalled: true,
      sampleLevel: "L2", contractId: `route_catalog:${item.sourceType}:aliyun`,
      routeSourceBinding: { regionId: reference.regionId, resourceIdentifierDigests: [sha256("different-resource")] } },
    "session-a"),
    !referenceMatchesPlan(reference, { ...item, receiptSource: "route_catalog", actualApiCalled: false,
      sampleLevel: "L1", contractId: `route_catalog:${item.sourceType}:aliyun` }, "session-a"),
    containsExactLiteral("/detail/i-abc?region=cn-hangzhou", "i-abc"),
    !containsExactLiteral("/login?instanceId=i-abcdef", "i-abc"),
    pageSignals("https://account.aliyun.com/login", "请登录").login,
    pageSignals("https://ots4service.console.aliyun.com", "OTSNoPermissionAccess").permission,
    pageSignals("https://ots4service.console.aliyun.com", "You have no permission to access the requested resource").permission,
    classifyFailure({ actualUiClick: true, expectedNavigation: "iframe", actualNavigation: "iframe",
      newTabObserved: false, signals: { ...CR_CLEAN_SIGNALS, permission: true },
      routeMatched: true, loginPassed: false, resourceMatched: true }) === "permission_denied",
    classifyFailure({ actualUiClick: true, expectedNavigation: "iframe", actualNavigation: "iframe",
      newTabObserved: false, signals: { ...CR_CLEAN_SIGNALS, permission: true, login: true },
      routeMatched: true, loginPassed: false, resourceMatched: true }) === "login_error",
    referenceMatchesPlan(reference, item, "session-a"),
    referenceMatchesPlan({...reference, regionId:null, scopeKind:"account",
      sources:[{product:"ecs",action:"DescribeInstances",requestRegionId:"cn-hangzhou"}]},
      {...item,querySourceBinding:{...item.querySourceBinding,regionId:null}}, "session-a"),
    !referenceMatchesPlan({...reference, identifier:"other-resource"}, item, "session-a"),
    !referenceMatchesPlan({...reference, regionId:"cn-beijing"}, item, "session-a"),
    !referenceMatchesPlan(reference, {...item, querySourceBinding:{...item.querySourceBinding,
      sessionIdDigest:sha256("other-session")}}, "session-a"),
    !referenceMatchesPlan(reference, {...item, querySourceBinding:{...item.querySourceBinding,
      accountScopeDigest:sha256("other-account")}}, "session-a"),
    !referenceMatchesPlan(reference, {...item, querySourceBinding:{...item.querySourceBinding,
      source:{sourcePath:"/result/Wrong/0/InstanceId"}}}, "session-a"),
    !referenceMatchesPlan(reference, { ...item, action: "DescribeDisks" }, "session-a"),
    referenceMatchesPlan({ ...reference, resourceType: "alicloud_log_project",
      sources: [{ product: "sls", action: "ListProject" }] },
    { ...item, sourceType: "alicloud_log_project", canonicalResourceType: "ALIYUN::SLS::Project",
      product: "sls", action: "ListProject" }, "session-a"),
    !referenceMatchesPlan({ ...reference, resourceType: "alicloud_log_project",
      sources: [{ product: "sls", action: "ListProject" }] },
    { ...item, sourceType: "alicloud_log_store", canonicalResourceType: "ALIYUN::SLS::Logstore",
      product: "sls", action: "ListProject" }, "session-a"),
    !referenceMatchesPlan({ ...reference, resourceType: "alicloud_log_project",
      sources: [{ product: "sls", action: "ListLogstores" }] },
    { ...item, sourceType: "alicloud_log_project", canonicalResourceType: "ALIYUN::SLS::Project",
      product: "sls", action: "ListProject" }, "session-a"),
    effectiveNavigationExpectation({
      navigation: "normal",
      expected: { expectedNavigation: "new_tab", expectedNavigationReason: "product_external_only",
        embeddedNavigationReason: "configured_signin_role" },
      actualNavigation: "iframe", routeMatched: true, loginPassed: true, resourceMatched: true,
    }).expectedNavigation === "iframe",
    effectiveNavigationExpectation({
      navigation: "normal",
      expected: { expectedNavigation: "new_tab", expectedNavigationReason: "product_external_only",
        embeddedNavigationReason: "configured_signin_role" },
      actualNavigation: "iframe", routeMatched: false, loginPassed: true, resourceMatched: true,
    }).expectedNavigation === "new_tab",
    classifyLanding({
      navigation: "normal",
      expectedNavigation: "iframe",
      finalUrl: "https://alb.console.aliyun.com/list",
      bodyText: "i-abc",
      reference,
      routeMatched: true,
      searchPerformed: true,
    }) === "product_list",
    searchControlScore({ type: "text", placeholder: "备注", inNavigation: false }) === null,
    searchControlScore({ type: "text", placeholder: "请输入资源 ID", inNavigation: false }) >= SEARCH_CONTROL_MIN_SCORE,
    searchControlScore({ type: "search", placeholder: "", inNavigation: true }) === null,
    resolveSearchFinalUrl(
      { performed: true, finalUrl: "https://alb.console.aliyun.com/tls?KeyWords=i-abc" },
      "https://alb.console.aliyun.com/tls",
    ) === "https://alb.console.aliyun.com/tls?KeyWords=i-abc",
    resourceIdentityLiterals({
      resourceType: "ALIYUN::CEN::RouteEntry",
      identifier: "cen-a/vpc-b/vtb-c",
    }).join(",") === "vtb-c,cen-a/vpc-b/vtb-c",
    containsResourceIdentity(
      "/vpc/cn-hangzhou/route-tables/vtb-c",
      { resourceType: "ALIYUN::CEN::RouteEntry", identifier: "cen-a/vpc-b/vtb-c" },
    ),
    resourceIdentityLiterals({
      resourceType: "ALIYUN::CEN::TransitRouterVpcAttachment",
      identifier: "tr-attach-a",
      attributes: { VpcId: "vpc-b" },
    }).join(",") === "vpc-b,tr-attach-a",
    redactSensitiveText(
      "webhook=https://example.invalid/hook?access_token=raw-value&room=ops token=second-value",
    ) === "webhook=https://example.invalid/hook?access_token=[REDACTED]&room=ops token=[REDACTED]",
    redactSensitiveText("http://example.invalid/#web_token=raw-value") ===
      "http://example.invalid/#web_token=[REDACTED]",
    sanitizePersistedValue({
      finalUrl: "https://example.invalid/callback?code=raw-code&state=kept",
      nested: { signature: "raw-signature" },
    }).finalUrl === "https://example.invalid/callback?code=[REDACTED]&state=kept",
    sanitizePersistedValue({ nested: { signature: "raw-signature" } }).nested.signature === "[REDACTED]",
    // CMS routing applies to exactly the two CloudMonitor types on /alert/contact.
    isCmsContactType({ resourceType: CMS_CONTACT_TYPE }) &&
      isCmsContactGroupType({ resourceType: CMS_CONTACT_GROUP_TYPE }) &&
      !isCmsContactType({ resourceType: CMS_CONTACT_GROUP_TYPE }) &&
      !isCmsContactGroupType({ resourceType: CMS_CONTACT_TYPE }) &&
      cmsFlowApplies({ resourceType: CMS_CONTACT_TYPE }, "https://cloudmonitor.console.aliyun.com/alert/contact") &&
      cmsFlowApplies({ resourceType: CMS_CONTACT_GROUP_TYPE }, "https://cloudmonitor.console.aliyun.com/alert/contact") &&
      !cmsFlowApplies({ resourceType: CMS_CONTACT_TYPE }, "https://ecs.console.aliyun.com/server/i-abc") &&
      !cmsFlowApplies({ resourceType: "ALIYUN::ECS::Instance" }, "https://cloudmonitor.console.aliyun.com/alert/contact"),
    // Group matching is exact-row, never whole-body substring (ros-cms-test vs cms-test).
    cmsRowTextMatches("cms-test(创建时间:2020年9月29日,修改时间:2026年1月7日)", "cms-test") &&
      !cmsRowTextMatches("ros-cms-test(创建时间:2021年12月8日)", "cms-test") &&
      !cmsRowTextMatches("ack_Default Contact Group(创建时间:2021年12月8日)", "cms-test"),
    // Contact evidence safety: full-surface screenshot omitted only for the contact flow.
    cmsScreenshotPolicy("contact_masking_switch") === CMS_CONTACT_SCREENSHOT_OMISSION &&
      cmsScreenshotPolicy("contact_group_tab") === null &&
      cmsScreenshotPolicy(null) === null,
    // Masking restore verification predicate.
    cmsMaskingRestoreVerified(true, true, false) &&
      !cmsMaskingRestoreVerified(true, true, true) &&
      !cmsMaskingRestoreVerified(true, false, false) &&
      cmsMaskingRestoreVerified(false, false, true),
    // Control association: an unrelated first switch, several switches, no
    // label or disagreeing labels must all safe-fail; only one associated
    // switch is clickable.
    cmsMaskingControlDecision({ switchCount: 3, labelCount: 0, scopes: [] }).unique === false &&
      cmsMaskingControlDecision({ switchCount: 3, labelCount: 1, scopes: [{ switchIndices: [] }] }).unique === false &&
      cmsMaskingControlDecision({ switchCount: 3, labelCount: 1, scopes: [{ switchIndices: [0, 2] }] }).unique === false &&
      cmsMaskingControlDecision({ switchCount: 3, labelCount: 2, scopes: [{ switchIndices: [1] }, { switchIndices: [2] }] }).unique === false &&
      cmsMaskingControlDecision({ switchCount: 3, labelCount: 2, scopes: [{ switchIndices: [1] }, { switchIndices: [1] }] }).unique === false &&
      (() => {
        const decision = cmsMaskingControlDecision({ switchCount: 3, labelCount: 1, scopes: [{ switchIndices: [1] }] });
        return decision.unique === true && decision.switchIndex === 1;
      })() &&
      cmsMaskingControlDecision({ switchCount: 1, labelCount: 1, scopes: [{ switchIndices: [5] }] }).unique === false,
    // A failed restore after a toggle must block pass and set a failure class.
    cmsRestoreBlocksPass(true, null) &&
      cmsRestoreBlocksPass(true, { verified: false }) &&
      !cmsRestoreBlocksPass(true, { verified: true }) &&
      !cmsRestoreBlocksPass(false, null),
    // Contact flow restores on the exception path and still fails the branch
    // when the restore cannot be verified (injected deps, no browser).
    await cmsSelfTestExceptionPathRestores() &&
    await cmsSelfTestVerifiedRestorePasses() &&
    await cmsSelfTestNoToggleNoRestore(),
    // ContactGroup tab strategy: visible Playwright locator + exact text +
    // normal actionability click; never evaluateHandle + raw DOM click.
    CMS_GROUP_TAB_STRATEGY.roleExact === true &&
      CMS_GROUP_TAB_STRATEGY.actionabilityClick === true &&
      CMS_GROUP_TAB_STRATEGY.evaluateHandleClick === false,
    classifyLanding({
      navigation: "shift",
      expectedNavigation: "new_tab",
      finalUrl: "https://cloudmonitor.console.aliyun.com/alert/contact",
      bodyText: "s*n-ros-py3-fvt-pre-1fb1152f",
      reference: { resourceType: CMS_CONTACT_TYPE, identifier: "sin-ros-py3-fvt-pre-1fb1152f", attributes: {} },
      routeMatched: true,
      cmsLocated: true,
    }) === "product_list",
    // CR classification priority: a passed CR repository match keeps the CR
    // exact_detail classification even though the rendered Repo ID field puts
    // the crr-* literal in the body and the parent segments in the URL; the
    // namespace list keeps product_list; a failed CR match still falls through
    // to the unchanged generic heuristics.
    classifyLanding({
      navigation: "normal",
      expectedNavigation: "iframe",
      finalUrl: "https://cr4service.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
      bodyText: "Repo ID\ncrr-x\n复制\n仓库名称\ndemo-repo",
      reference: CR_SELF_TEST_REPOSITORY_REFERENCE,
      routeMatched: true,
      crLocated: true,
      crLandingKind: crLandingKind(CR_SELF_TEST_REPOSITORY_REFERENCE),
    }) === "exact_detail",
    classifyLanding({
      navigation: "shift",
      expectedNavigation: "new_tab",
      finalUrl: "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
      bodyText: "crr-x",
      reference: CR_SELF_TEST_REPOSITORY_REFERENCE,
      routeMatched: true,
      crLocated: true,
      crLandingKind: crLandingKind(CR_SELF_TEST_REPOSITORY_REFERENCE),
    }) === "exact_detail",
    classifyLanding({
      navigation: "normal",
      expectedNavigation: "iframe",
      finalUrl: CR_SELF_TEST_NAMESPACE_URL,
      bodyText: "test192 正常 关闭 仓库类型私有",
      reference: CR_SELF_TEST_NAMESPACE_REFERENCE,
      routeMatched: true,
      crLocated: true,
      crLandingKind: crLandingKind(CR_SELF_TEST_NAMESPACE_REFERENCE),
    }) === "product_list",
    classifyLanding({
      navigation: "normal",
      expectedNavigation: "iframe",
      finalUrl: "https://cr4service.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
      bodyText: "crr-x",
      reference: CR_SELF_TEST_REPOSITORY_REFERENCE,
      routeMatched: true,
      crLocated: false,
      crLandingKind: null,
    }) === "parent_list",
    // Group row settle: cap is min(product 5s, remaining deadline); expired or
    // non-finite deadlines collapse to a zero budget (single probe, no sleep).
    cmsGroupRowSettleBudget(1000 + 60000, 1000).capMs === 5000 &&
      cmsGroupRowSettleBudget(1000 + 60000, 1000).intervalMs === 100 &&
      cmsGroupRowSettleBudget(1000 + 700, 1000).capMs === 700 &&
      cmsGroupRowSettleBudget(900, 1000).capMs === 0 &&
      cmsGroupRowSettleBudget(Number.POSITIVE_INFINITY, 1000).capMs === 0,
    // Group row settle poll: immediate hit stops, late hit retries on the
    // short interval, cap/deadline bound the wait, timeout safe-fails.
    await cmsSelfTestSettleImmediateHit() &&
      await cmsSelfTestSettleLateHit() &&
      await cmsSelfTestSettleCapBound() &&
      await cmsSelfTestSettleDeadlineBound() &&
      await cmsSelfTestSettleExpiredDeadlineSafeFails(),
    // Group pane scope: only exactly one visible tab pane scopes the search;
    // zero or several visible panes fall back to per-element visibility.
    cmsGroupPaneScope({ paneCount: 2, visiblePaneCount: 1 }).scoped === true &&
      cmsGroupPaneScope({ paneCount: 2, visiblePaneCount: 0 }).scoped === false &&
      cmsGroupPaneScope({ paneCount: 2, visiblePaneCount: 2 }).scoped === false,
    // Group card match accepts the rendered collapse-card shape
    // "identifier(创建时间:...)" and rejects prefixed/suffixed identifiers.
    cmsGroupCardMatches("云账号报警联系人(创建时间:2020年5月26日,修改时间:2020年5月26日)", "云账号报警联系人") &&
      cmsGroupCardMatches("cms-test(创建时间:2020年9月29日,修改时间:2026年1月7日)", "cms-test") &&
      !cmsGroupCardMatches("ros-cms-test(创建时间:2021年12月8日)", "cms-test") &&
      !cmsGroupCardMatches("ack_Default Contact Group(创建时间:2021年12月8日)", "cms-test") &&
      !cmsGroupCardMatches("cms-test-group(创建时间:2021年12月8日)", "cms-test"),
    // Hidden contact-table rows are never candidates; only visible cards are
    // counted, and a single visible exact card passes.
    (() => {
      const cards = [
        { visible: true, text: "云账号报警联系人(创建时间:2020年5月26日,修改时间:2020年5月26日)" },
        ...Array.from({ length: 10 }, () => ({ visible: false, text: "hidden-contact-row" })),
      ];
      const result = cmsGroupRowCardResult(cards, "云账号报警联系人");
      return result.matched === true && result.rowCount === 1 && result.hiddenExcluded === 10 &&
        result.ambiguous === false;
    })() &&
    // No visible exact card (hidden ones only) must fail closed.
    cmsGroupRowCardResult(
      Array.from({ length: 10 }, () => ({ visible: false, text: "云账号报警联系人(创建时间:x)" })),
      "云账号报警联系人",
    ).matched === false &&
    // Ambiguous duplicate visible exact matches must fail closed.
    (() => {
      const result = cmsGroupRowCardResult([
        { visible: true, text: "cms-test(创建时间:2020年9月29日,修改时间:2026年1月7日)" },
        { visible: true, text: "cms-test(创建时间:2020年9月29日,修改时间:2026年1月7日)" },
      ], "cms-test");
      return result.matched === false && result.ambiguous === true && result.rowCount === 2;
    })(),
    // CR routing applies to exactly the two Container Registry types on a CR
    // domestic console host (the embedded cr4service host normalizes to it);
    // non-CR types never enter the flow, so their behaviour is unchanged.
    isCrNamespaceType({ resourceType: CR_NAMESPACE_TYPE }) &&
      isCrRepositoryType({ resourceType: CR_REPOSITORY_TYPE }) &&
      isCrConsoleHost("cr.console.aliyun.com") &&
      isCrConsoleHost("cr4service.console.aliyun.com") &&
      !isCrConsoleHost("cr.console.aliyun.com.evil.invalid") &&
      !isCrConsoleHost("cms.console.aliyun.com") &&
      crFlowApplies(CR_SELF_TEST_NAMESPACE_REFERENCE, CR_SELF_TEST_NAMESPACE_URL) &&
      crFlowApplies(CR_SELF_TEST_REPOSITORY_REFERENCE,
        "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details") &&
      !crFlowApplies(CR_SELF_TEST_NAMESPACE_REFERENCE,
        "https://ecs.console.aliyun.com/cn-beijing/instance/cri-a/namespaces") &&
      !crFlowApplies(CR_SELF_TEST_NAMESPACE_REFERENCE, null) &&
      !crFlowApplies(CR_SELF_TEST_NAMESPACE_REFERENCE,
        "http://cr.console.aliyun.com/cn-beijing/instance/cri-a/namespaces") &&
      !crFlowApplies({ resourceType: CMS_CONTACT_TYPE, identifier: "cms-test" }, CR_SELF_TEST_NAMESPACE_URL) &&
      !crFlowApplies({ resourceType: "ALIYUN::ECS::Instance", identifier: "i-abc" }, CR_SELF_TEST_NAMESPACE_URL) &&
      !crFlowApplies({ resourceType: "ALIYUN::CEN::CenInstance", identifier: "cen-a" }, CR_SELF_TEST_NAMESPACE_URL),
    // The two CR types never trigger the generic search fallback (the only
    // qualifying control on a CR console is the console-global product search
    // box); CMS and other types keep the existing fallback behaviour.
    crSuppressesSearchFallback(CR_SELF_TEST_NAMESPACE_REFERENCE) === true &&
      crSuppressesSearchFallback(CR_SELF_TEST_REPOSITORY_REFERENCE) === true &&
      crSuppressesSearchFallback({ resourceType: CMS_CONTACT_TYPE }) === false &&
      crSuppressesSearchFallback(reference) === false &&
      crSuppressesSearchFallback({ resourceType: "ALIYUN::CEN::CenInstance" }) === false &&
      crLandingKind(CR_SELF_TEST_NAMESPACE_REFERENCE) === "product_list" &&
      crLandingKind(CR_SELF_TEST_REPOSITORY_REFERENCE) === "exact_detail" &&
      crLandingKind(reference) === null,
    // CR names are deliberately NOT added to the global identity aliases.
    resourceIdentityLiterals(CR_SELF_TEST_NAMESPACE_REFERENCE).join(",") === "crn-x" &&
      resourceIdentityLiterals(CR_SELF_TEST_REPOSITORY_REFERENCE).join(",") === "crr-x",
    // Namespace parent context: region + InstanceId must both match; a wrong
    // region, a wrong instance or an unrecognized route fails closed.
    crNamespaceContextCheck(CR_SELF_TEST_NAMESPACE_URL, CR_SELF_TEST_NAMESPACE_REFERENCE).matched === true &&
      crNamespaceContextCheck(CR_SELF_TEST_NAMESPACE_URL,
        { ...CR_SELF_TEST_NAMESPACE_REFERENCE, regionId: "cn-hangzhou" }).matched === false &&
      crNamespaceContextCheck(CR_SELF_TEST_NAMESPACE_URL,
        { ...CR_SELF_TEST_NAMESPACE_REFERENCE, attributes: { InstanceId: "cri-other" } }).matched === false &&
      crNamespaceContextCheck("https://cr.console.aliyun.com/cn-beijing/instance/cri-a/repositories",
        CR_SELF_TEST_NAMESPACE_REFERENCE).matched === false &&
      crNamespaceContextCheck("https://ecs.console.aliyun.com/cn-beijing/instance/cri-a/namespaces",
        CR_SELF_TEST_NAMESPACE_REFERENCE).hostOk === false &&
      (() => {
        const context = crNamespaceContextCheck(CR_SELF_TEST_NAMESPACE_URL, CR_SELF_TEST_NAMESPACE_REFERENCE);
        return context.hostOk === true && context.shapeRecognized === true &&
          context.reason === null && context.route === "/cn-beijing/instance/cri-a/namespaces" &&
          context.fields.length === 2 &&
          context.fields[0].name === "region" && context.fields[0].expected === "cn-beijing" &&
          context.fields[0].observed === "cn-beijing" &&
          context.fields[1].name === "instanceId" && context.fields[1].expected === "cri-a" &&
          context.fields[1].observed === "cri-a";
      })(),
    // Namespace visible-row evaluation: exactly one distinct visible exact row
    // passes; zero rows, ambiguous duplicates, hidden rows, page chrome and
    // wrapper candidates all fail closed.  Boundary-safe matching rejects a
    // prefixed/suffixed different name.
    (() => {
      const rows = [
        crSelfTestRow("test192 正常 关闭 仓库类型私有"),
        crSelfTestRow("test6182 正常 开启", { top: 140 }),
        crSelfTestRow("test192 正常", { visible: false, top: 100 }),
        crSelfTestRow("test192 正常", { chrome: true, top: 100 }),
        crSelfTestRow("test192 正常 关闭 仓库类型私有 修改 删除", { nested: true, top: 100 }),
      ];
      const result = crNamespaceRowResult(rows, "test192");
      return result.matched === true && result.distinctMatches === 1 && result.rowCount === 3 &&
        result.candidateMatches === 2 && result.ambiguous === false && result.hiddenExcluded === 1 &&
        result.chromeExcluded === 1 && result.wrapperCandidates === 1;
    })() &&
    // A bare <form> ancestor does not disqualify a real row (some consoles wrap
    // the page body in a form); search/nav/chrome ancestors still do.
    crNamespaceRowResult([crSelfTestRow("test192 正常", { formWrapped: true })], "test192").matched === true &&
      crNamespaceRowResult([crSelfTestRow("test192 正常", { chrome: true, formWrapped: true })],
        "test192").matched === false &&
      crNamespaceRowResult([crSelfTestRow("test192 正常", { visible: false, formWrapped: true })],
        "test192").matched === false &&
    crNamespaceRowResult([crSelfTestRow("test6182 正常 开启")], "test192").matched === false &&
    crNamespaceRowResult([], "test192").matched === false &&
    (() => {
      const result = crNamespaceRowResult([
        crSelfTestRow("test192 正常", { top: 100 }),
        crSelfTestRow("test192 正常", { top: 180 }),
      ], "test192");
      return result.matched === false && result.ambiguous === true && result.distinctMatches === 2;
    })() &&
    // Wrapper/locked-column clones of one logical row share the same rendered
    // top edge (within 2px) and count once; the crn-* identifier is never a
    // match value, and two rows on different edges stay ambiguous.
    crNamespaceRowResult([
      crSelfTestRow("test192 正常 关闭", { top: 100 }),
      crSelfTestRow("test192", { top: 100.4 }),
    ], "test192").matched === true &&
    crNamespaceRowResult([
      crSelfTestRow("test192 正常", { top: 100 }),
      crSelfTestRow("test192 正常", { top: 140 }),
    ], "test192").matched === false &&
    crDistinctMatchKeys([
      { text: "a", top: 100 }, { text: "a", top: 101 }, { text: "a", top: 200 },
    ]).length === 2 &&
    crDistinctMatchKeys([{ text: "a", top: null }, { text: "a", top: null }]).length === 1 &&
    crDistinctMatchKeys([{ text: "a", top: null }, { text: "b", top: null }]).length === 2 &&
    crNameMatches("test192 正常", "test192") &&
      !crNameMatches("mytest192 正常", "test192") &&
      !crNameMatches("test1922 正常", "test192") &&
      !crNameMatches("test6182 正常", "test192") &&
      !crNameMatches("crn-x 正常", "test192") &&
      crNameMatches(null, "test192") === false &&
      crNameMatches("test192", "") === false,
    // Repository detail context: all four URL segments must match the source
    // context; any single wrong field fails closed.
    crRepositoryContextCheck(
      "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
      CR_SELF_TEST_REPOSITORY_REFERENCE).matched === true &&
      crRepositoryContextCheck(
        "https://cr4service.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === true &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/repository/cn-hangzhou/cri-a/test192/demo-repo/details",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === false &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/repository/cn-beijing/cri-other/test192/demo-repo/details",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === false &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test6182/demo-repo/details",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === false &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/other-repo/details",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === false &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/cn-beijing/instance/cri-a/repositories",
        CR_SELF_TEST_REPOSITORY_REFERENCE).matched === false &&
      crRepositoryContextCheck(
        "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
        { ...CR_SELF_TEST_REPOSITORY_REFERENCE, attributes: { InstanceId: "cri-a" } }).matched === false &&
      (() => {
        const context = crRepositoryContextCheck(
          "https://cr.console.aliyun.com/repository/cn-beijing/cri-a/test192/demo-repo/details",
          CR_SELF_TEST_REPOSITORY_REFERENCE);
        return context.shapeRecognized === true && context.reason === null &&
          context.fields.map((field) => `${field.name}:${field.ok}`).join(",") ===
            "region:true,instanceId:true,repoNamespaceName:true,repoName:true";
      })(),
    // End-to-end CR flows with an injected read-only surface: the namespace flow
    // matches a unique visible row, fails on zero/duplicate rows, wrong region,
    // wrong instance, page signals, read failure and a missing source name; the
    // repository flow is URL-context only and never touches the surface.  No
    // flow may click, type or navigate, and the search fallback stays suppressed.
    await crSelfTestNamespaceFlows() &&
    await crSelfTestRepositoryFlows() &&
    (await crLocateResource(crSelfTestSurface([]), reference,
      "https://alb.console.aliyun.com/list", CR_CLEAN_SIGNALS, Date.now() + 200)) === null,
  ];
  if (!checks.every(Boolean)) throw new Error("browser classifier self-test failed");
  process.stdout.write(`${JSON.stringify({ status: "passed", checks: checks.length })}\n`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) { process.stdout.write(`${usage()}\n`); return; }
  if (args.selfTest) { await runSelfTest(); return; }
  const chromium = loadChromium();
  const plan = readJson(args.plan);
  if (plan.site !== "aliyun" || !/^[0-9a-f]{64}$/.test(plan.planId || "") ||
      plan.batchSize !== plan.items?.length || plan.items.length > 5) {
    throw new Error("batch plan is malformed or outside the domestic-site scope");
  }
  fs.mkdirSync(args.artifactsDir, { recursive: true, mode: 0o700 });
  const lockPath = path.resolve(args.artifactsDir, ".capture.lock");
  const lockToken = acquireLock(lockPath);
  let browser = null;
  try {
    browser = await chromium.connectOverCDP(args.cdpEndpoint);
    const page = await activeIacPage(browser, args.appOrigin, args.sessionId);
    const sessionId = await activeSessionId(page, args.sessionId);
    const readinessPath = path.resolve(args.output + ".reply-readiness.json");
    const readinessBefore = await webReplyReadinessSnapshot(page, sessionId);
    const replyCompleted = await waitForWebReplyCompleted(page, sessionId, args.timeoutMs);
    writeJson(readinessPath, {
      schemaVersion: 1,
      before: readinessBefore,
      after: await webReplyReadinessSnapshot(page, sessionId),
      replyCompleted,
    });
    if (!replyCompleted) throw new Error("formal Web reply has not completed; resource clicks are forbidden");
    const context = page.context();
    const references = await resourceReferences(page, sessionId);
    const selections = [];
    for (const item of plan.items) {
      const selected = await selectReference(page, references, item, sessionId, Date.now() + args.timeoutMs);
      if (!selected.reference) {
        throw new Error(`active session has no domestic resource fact for ${item.canonicalResourceType}`);
      }
      selections.push({ item, ...selected });
    }
    const accountScopes = new Set(selections.map(({ reference }) => reference.accountScope));
    if (accountScopes.size !== 1) throw new Error("one browser batch must use exactly one account scope");
    const accountScope = [...accountScopes][0];
    const sessionIdDigest = sha256(sessionId);
    const accountScopeDigest = sha256(accountScope);

    let preflight;
    let freshPreflightObservations = [];
    if (args.preflightInput) {
      preflight = readJson(args.preflightInput);
      const capturedAt = Date.parse(preflight.capturedAt);
      const expiresAt = Date.parse(preflight.expiresAt);
      if (preflight.catalogRevisionDigest !== plan.catalogRevisionDigest ||
          preflight.sessionIdDigest !== sessionIdDigest || preflight.accountScopeDigest !== accountScopeDigest) {
        throw new Error("preflight does not belong to this catalog revision, active session, and account");
      }
      if (!Number.isFinite(capturedAt) || !Number.isFinite(expiresAt) || Date.now() >= expiresAt ||
          expiresAt <= capturedAt || expiresAt - capturedAt > 30 * 60 * 1000) {
        throw new Error("preflight is expired or has an invalid validity window");
      }
    } else if (!args.normalOnlyPreflight) {
      const probe = args.preflightResourceType
        ? selections.find(({ item }) => item.sourceType === args.preflightResourceType)
        : selections.find(({ item }) => item.actualApiCalled && ["L2", "L3"].includes(item.sampleLevel));
      if (!probe) {
        throw new Error("fresh preflight resource type is not present or has no L2/L3 plan item");
      }
      const expected = await expectedOrdinaryNavigation(page, sessionId, probe.item, probe.reference, probe.link);
      const normal = await captureObservation({
        page, context, ...probe, navigation: "normal", expected, attemptCount: 1,
        artifactsDir: args.artifactsDir, timeoutMs: args.timeoutMs, sequence: "preflight-normal", planId: plan.planId,
      });
      probe.link = await visibleLink(page, probe.reference.identifier, probe.reference);
      const shift = await captureObservation({
        page, context, ...probe, navigation: "shift", expected, attemptCount: 1,
        artifactsDir: args.artifactsDir, timeoutMs: args.timeoutMs, sequence: "preflight-shift", planId: plan.planId,
      });
      const passed = [normal, shift].every((item) =>
        item.actualUiClick && item.routeMatched && item.loginPassed && item.resourceMatched && !item.failureReason) &&
        normal.actualNavigation === normal.expectedNavigation && shift.actualNavigation === "new_tab" &&
        shift.newTabObserved;
      if (!passed) throw new Error("browser preflight failed; inspect its observation artifacts before batch execution");
      const capturedAt = new Date();
      preflight = {
        schemaVersion: 1,
        capturedAt: capturedAt.toISOString(),
        expiresAt: new Date(capturedAt.getTime() + 25 * 60 * 1000).toISOString(),
        catalogRevisionDigest: plan.catalogRevisionDigest,
        status: "passed",
        surface: "iac-code-web",
        browserAccessMode: "existing-chrome",
        sessionIdDigest,
        accountScopeDigest,
        sample: {
          resourceType: probe.item.canonicalResourceType,
          resourceIdentifierDigest: sha256(probe.reference.identifier),
          site: "aliyun",
          sampleLevel: "L2",
          actualApiCalled: true,
        },
        normal: {
          actualUiClick: normal.actualUiClick,
          actualNavigation: normal.actualNavigation,
          routeMatched: normal.routeMatched,
          newTabObserved: normal.newTabObserved,
          finalUrl: normal.finalUrl,
          loginPassed: normal.loginPassed,
          resourceMatched: normal.resourceMatched,
          observedAt: normal.capturedAt,
          artifactPath: normal.artifacts[0].path,
          artifactDigest: normal.artifacts[0].digest,
        },
        shift: {
          actualUiClick: shift.actualUiClick,
          actualNavigation: shift.actualNavigation,
          routeMatched: shift.routeMatched,
          newTabObserved: shift.newTabObserved,
          finalUrl: shift.finalUrl,
          loginPassed: shift.loginPassed,
          resourceMatched: shift.resourceMatched,
          observedAt: shift.capturedAt,
          artifactPath: shift.artifacts[0].path,
          artifactDigest: shift.artifacts[0].digest,
        },
      };
      writeJson(args.preflightOutput, preflight);
      freshPreflightObservations = [normal, shift];
    }

    const batch = {
      schemaVersion: 1,
      catalogRevisionDigest: plan.catalogRevisionDigest,
      planId: plan.planId,
      browserAccessMode: preflight?.browserAccessMode || "existing-chrome",
      sessionIdDigest,
      accountScopeDigest,
      items: [],
    };
    let sequence = 0;
    for (const selection of selections) {
      const { item, reference } = selection;
      const resumesSameSample = sameResumeSample(item, { sessionId, accountScope, reference });
      const navigations = args.normalOnly
        ? ["normal"]
        : (resumesSameSample ? item.missingNavigations : item.allowedNavigations);
      const expected = selection.link
        ? await expectedOrdinaryNavigation(page, sessionId, item, reference, selection.link)
        : { expectedNavigation: "new_tab", expectedNavigationReason: "external_default", expectedUrl: null };
      for (const navigation of navigations) {
        const preflightObservation = freshPreflightObservations.find((observation) =>
          observation.sourceType === item.sourceType && observation.source === item.receiptSource &&
          observation.contractId === item.contractId && observation.navigation === navigation &&
          observation.resourceIdentifier === reference.identifier);
        if (preflightObservation) {
          const { canonicalResourceType: _canonical, ...recorded } = preflightObservation;
          batch.items.push(recorded);
          writeJson(args.output, batch);
          continue;
        }
        const link = await visibleLink(page, reference.identifier, reference);
        const attemptCount = resumesSameSample
          ? (item.resumeContext.nextAttemptCounts?.[navigation] || 1)
          : 1;
        const observation = await captureObservation({
          page,
          context,
          item,
          reference,
          link,
          navigation,
          expected,
          attemptCount,
          artifactsDir: args.artifactsDir,
          timeoutMs: args.timeoutMs,
          sequence: ++sequence,
          planId: plan.planId,
        });
        const { canonicalResourceType: _canonical, ...recorded } = observation;
        batch.items.push(recorded);
        // Persist after every branch. An interrupted run remains importable as
        // a partial batch and never repeats branches that already completed.
        writeJson(args.output, batch);
      }
    }
    if (batch.items.length) writeJson(args.output, batch);
    if (args.normalOnlyPreflight) {
      const normal = batch.items.find((item) => item.navigation === "normal");
      if (!normal) throw new Error("normal-only preflight requires a captured normal observation");
      const preserved = readJson(args.preservedShiftBatch);
      const shift = preserved.items?.find((item) =>
        item.sourceType === normal.sourceType && item.source === normal.source &&
        item.contractId === normal.contractId && item.navigation === "shift");
      if (!shift) throw new Error("preserved Shift batch has no matching CEN contract");
      const normalSelection = selections.find(({ item }) =>
        item.sourceType === normal.sourceType && item.receiptSource === normal.source &&
        item.contractId === normal.contractId);
      if (!normalSelection) throw new Error("normal-only preflight has no matching plan item");
      const missingNormalReplay = normalSelection.item.missingNavigations.length === 1 &&
        normalSelection.item.missingNavigations[0] === "normal" && Boolean(normalSelection.item.resumeContext);
      if (missingNormalReplay && (preserved.catalogRevisionDigest !== plan.catalogRevisionDigest ||
          preserved.accountScopeDigest !== accountScopeDigest ||
          shift.regionId !== normal.regionId || shift.resourceIdentifier !== normal.resourceIdentifier ||
          JSON.stringify(shift.resourceParentAttributes || {}) !== JSON.stringify(normal.resourceParentAttributes || {}) ||
          !shift.actualUiClick || !shift.loginPassed || !shift.resourceMatched || !shift.routeMatched ||
          !shift.newTabObserved || shift.failureReason !== null)) {
        throw new Error("preserved Shift must be the same verified catalog/account/region/resource branch");
      }
      const shiftObservation = shift.artifacts?.find((artifact) => artifact.kind === "observation");
      const normalObservation = normal.artifacts?.find((artifact) => artifact.kind === "observation");
      if (!shiftObservation || !normalObservation) throw new Error("preflight observations are incomplete");
      const capturedAt = new Date();
      preflight = {
        schemaVersion: 1,
        capturedAt: capturedAt.toISOString(),
        expiresAt: new Date(capturedAt.getTime() + 25 * 60 * 1000).toISOString(),
        catalogRevisionDigest: plan.catalogRevisionDigest,
        status: "passed",
        surface: "iac-code-web",
        browserAccessMode: "existing-chrome",
        sessionIdDigest,
        accountScopeDigest,
        cdpEndpoint: args.cdpEndpoint,
        ...(args.userDataDir ? { userDataDir: args.userDataDir } : {}),
        singleBranchReplayReason: missingNormalReplay
          ? "targeted_missing_normal_replay" : "production_semantic_change_targeted_normal_replay",
        branchSessionIdDigests: { normal: sessionIdDigest, shift: preserved.sessionIdDigest },
        sample: {
          resourceType: normal.sourceType,
          resourceIdentifierDigest: sha256(normal.resourceIdentifier),
          site: "aliyun",
          sampleLevel: normalSelection.item.sampleLevel,
          actualApiCalled: normalSelection.item.actualApiCalled,
        },
        normal: {
          actualUiClick: normal.actualUiClick,
          actualNavigation: normal.actualNavigation,
          routeMatched: normal.routeMatched,
          newTabObserved: normal.newTabObserved,
          finalUrl: normal.finalUrl,
          loginPassed: normal.loginPassed,
          resourceMatched: normal.resourceMatched,
          observedAt: normal.capturedAt,
          artifactPath: normalObservation.path,
          artifactDigest: normalObservation.digest,
        },
        shift: {
          actualUiClick: shift.actualUiClick,
          actualNavigation: shift.actualNavigation,
          routeMatched: shift.routeMatched,
          newTabObserved: shift.newTabObserved,
          finalUrl: shift.finalUrl,
          loginPassed: shift.loginPassed,
          resourceMatched: shift.resourceMatched,
          observedAt: shift.capturedAt,
          artifactPath: shiftObservation.path,
          artifactDigest: shiftObservation.digest,
        },
      };
      writeJson(args.preflightOutput, preflight);
    }
    process.stdout.write(`${JSON.stringify({
      status: "captured",
      sessionIdDigest,
      items: batch.items.length,
      passed: batch.items.filter((item) => item.failureReason === null).length,
      failed: batch.items.filter((item) => item.failureReason !== null).length,
      output: path.resolve(args.output),
      preflightOutput: args.preflightOutput ? path.resolve(args.preflightOutput) : null,
    })}\n`);
  } finally {
    try {
      if (fs.readFileSync(lockPath, "ascii").trim() === lockToken) fs.unlinkSync(lockPath);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    // This is an attachment to the user's already running Chrome. Let process
    // exit close only the CDP socket; Browser.close() would terminate Chrome.
  }
}

await main();
// CDP attachment sockets can keep Node alive after artifacts and lock cleanup.
// Exit this worker only; do not send Browser.close() to the user's Chrome.
process.exit(0);
