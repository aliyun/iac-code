import { t } from "../i18n.js?v=web-repl-ui-277";
import { buildResourceConsoleUrl, RESOURCE_LINK_RULES } from "../resource_link_rules.js?v=resource-links-v82";

let closeActive = null;

// Alibaba Cloud's federation endpoint writes its `.aliyun.com` session
// cookies without `SameSite=None`. Modern browsers therefore discard those
// cookies when the very first federation flow runs inside the local app's
// cross-site iframe. A short top-level bootstrap lets the released mini
// console establish the session in first-party context; later iframe loads
// can reuse it. Keep this client-only lease deliberately shorter than the
// three-hour mini ticket/session window documented by Alibaba Cloud. A lease
// must not be inferred from elapsed time or a cross-origin iframe load.
const SIGNIN_BOOTSTRAP_LEASE_MS = 30 * 60 * 1000;
const SIGNIN_BOOTSTRAP_WAIT_MS = 2500;
const signinBootstrapLeases = new Map();

const EMBEDDED_CONSOLE_HOSTS = Object.freeze({
  "ros-intl.console.alibabacloud.com": "ros4service.console.alibabacloud.com",
});

// These released ROS links do not currently have a reachable domestic mini
// console. Keep their verified ordinary-console URL as the external fallback
// instead of manufacturing a 4service destination that returns 504/DNS errors.
// Audited against the production hosts on 2026-09-12.
const EXTERNAL_ONLY_CONSOLE_HOSTS = new Set([
  "acmnext.console.aliyun.com",
  "apigateway.console.aliyun.com",
  "appflow.console.aliyun.com",
  "dhsnext.console.aliyun.com",
  "dms.console.aliyun.com",
  "ehpc.console.aliyun.com",
  "emr.console.aliyun.com",
  "emr-next.console.aliyun.com",
  "essnew.console.aliyun.com",
  "gws.console.aliyun.com",
  "cloudmonitor.console.aliyun.com",
]);

// RAM's domestic mini console can render the User/Group shells, but its
// embedded backend rejects the detail calls with ServiceNotSupported
// (ram:GetUser / ram:GetGroup). Keep Role and ManagedPolicy embedded: their
// dedicated detail routes have been verified independently.
const EXTERNAL_ONLY_DOMESTIC_RESOURCE_TYPES = new Set([
  // ALB's public TLS/security-policy list is verified; its 4service iframe
  // surface is not usable, so keep this resource type on the external path.
  "ALIYUN::ALB::SecurityPolicy",
  // CEN bandwidth packages expose a public list with a working resource-name
  // filter, but the domestic 4service list shell does not expose that filter.
  // Keep the CEN host available for other resource types whose embedded
  // detail pages are verified.
  "ALIYUN::CEN::CenBandwidthPackage",
  "ALIYUN::RAM::Group",
  "ALIYUN::RAM::User",
]);

export function closeResourceLinkPreview() {
  closeActive?.();
}

function previewTarget(resolution, rules) {
  const rule = rules.find((item) => item.ruleId === resolution?.ruleId && item.releaseStatus === "verified");
  if (!rule || typeof resolution?.url !== "string") return null;
  try {
    const target = new URL(resolution.url);
    const verified = buildResourceConsoleUrl(rule, resolution.reference);
    return target.protocol === "https:" && verified === target.href && !target.username && !target.password && !target.port
      ? target : null;
  } catch (_error) { return null; }
}

function embeddedConsoleHost(hostname) {
  const explicit = EMBEDDED_CONSOLE_HOSTS[hostname];
  if (explicit) return explicit;
  if (EXTERNAL_ONLY_CONSOLE_HOSTS.has(hostname) || !hostname.endsWith(".console.aliyun.com")) return null;
  const product = hostname.slice(0, -".console.aliyun.com".length);
  // Elasticsearch puts the region between the product and console suffix.
  // Its mini-console host keeps that order after the 4service marker.
  if (product.startsWith("elasticsearch-")) {
    return `elasticsearch4service-${product.slice("elasticsearch-".length)}.console.aliyun.com`;
  }
  return `${product}4service.console.aliyun.com`;
}

function embeddedConsoleUrl(value) {
  try {
    const target = new URL(value);
    const host = embeddedConsoleHost(target.hostname);
    if (!host || target.protocol !== "https:" || target.username || target.password || target.port) return null;
    if (target.hostname === "ram.console.aliyun.com") {
      const policy = /^\/policies\/([^/]+)\/Custom\/?$/.exec(target.pathname);
      const policyName = target.pathname === "/policies/detail" &&
        ["System", "Custom"].includes(target.searchParams.get("policyType")) &&
        target.searchParams.get("policyName");
      const legacyRole = /^\/roles\/([^/]+)\/?$/.exec(target.pathname);
      const roleName = target.pathname === "/roles/detail" && target.searchParams.get("roleName");
      const groupName = /^\/groups\/([^/]+)\/?$/.exec(target.pathname);
      const userId = target.pathname === "/users/detail" && target.searchParams.get("userId");
      if (policy) {
        target.pathname = "/policies/detail";
        target.search = "";
        target.searchParams.set("policyType", "Custom");
        target.searchParams.set("policyName", decodeURIComponent(policy[1]));
      } else if (policyName && literalConsoleValue(policyName) && [...target.searchParams].length === 2) {
        const policyType = target.searchParams.get("policyType");
        target.search = "";
        target.searchParams.set("policyType", policyType);
        target.searchParams.set("policyName", policyName);
      } else if (legacyRole && literalConsoleValue(decodeURIComponent(legacyRole[1])) && !target.search) {
        target.pathname = "/roles/detail";
        target.searchParams.set("roleName", decodeURIComponent(legacyRole[1]));
      } else if (roleName && literalConsoleValue(roleName) && [...target.searchParams].length === 1) {
        target.search = "";
        target.searchParams.set("roleName", roleName);
      } else if (groupName && literalConsoleValue(decodeURIComponent(groupName[1])) && !target.search) {
        target.pathname = `/groups/${encodeURIComponent(decodeURIComponent(groupName[1]))}`;
      } else if (target.pathname === "/groups" && !target.search) {
        target.pathname = "/groups";
      } else if (userId && literalConsoleValue(userId) && [...target.searchParams].length === 1) {
        target.search = "";
        target.searchParams.set("userId", userId);
      } else {
        // Other RAM paths come from the same checked-in ROS CHANNEL_LINKS
        // rule that produced the external URL. Preserve legacy hash routes.
        target.hostname = host;
        return target.href;
      }
      target.hash = "";
    }
    target.hostname = host;
    return target.href;
  } catch (_error) { return null; }
}

function literalConsoleValue(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 1024 &&
    !/[\s\x00-\x1f\x7f*<>`]|\$[\w{(]|\.\.\.|…|:\/\//.test(value);
}

export function buildEmbeddedSigninRequest(resolution, rules = RESOURCE_LINK_RULES) {
  const current = rules.find((item) => item.ruleId === resolution?.ruleId && item.releaseStatus === "verified");
  const reference = resolution?.reference;
  if (!current || !reference?.accountScope) return null;
  if (reference.consolePartition === "aliyun" && EXTERNAL_ONLY_DOMESTIC_RESOURCE_TYPES.has(current.resourceType)) {
    return null;
  }
  const destinations = {};
  for (const partition of ["aliyun", "alibabacloud"]) {
    const rule = partition === current.partition ? current : rules.find((item) =>
      item.releaseStatus === "verified" && item.partition === partition &&
      item.resourceType === current.resourceType && item.sourceType === current.sourceType);
    const url = buildResourceConsoleUrl(rule, {...reference, consolePartition:partition});
    const embedded = embeddedConsoleUrl(url);
    if (embedded) destinations[partition] = embedded;
  }
  return destinations[reference.consolePartition] ? {
    accountScope: reference.accountScope,
    preferredPartition: reference.consolePartition,
    destinations,
  } : null;
}

export function supportsEmbeddedSignin(resolution, rules = RESOURCE_LINK_RULES) {
  return Boolean(buildEmbeddedSigninRequest(resolution, rules));
}

function signinBootstrapKey(request) {
  if (!request?.accountScope || !request?.preferredPartition) return "";
  try {
    const destination = new URL(request.destinations?.[request.preferredPartition]);
    return `${request.preferredPartition}\u0000${request.accountScope}\u0000${destination.origin}`;
  } catch (_error) { return ""; }
}

export function needsTopLevelSigninBootstrap(request, now = Date.now()) {
  const key = signinBootstrapKey(request);
  return Boolean(key) && (signinBootstrapLeases.get(key) || 0) <= now;
}

export function rememberTopLevelSigninBootstrap(request, now = Date.now()) {
  const key = signinBootstrapKey(request);
  if (key) signinBootstrapLeases.set(key, now + SIGNIN_BOOTSTRAP_LEASE_MS);
}

export function resetTopLevelSigninBootstrapForTests() {
  signinBootstrapLeases.clear();
}

export function openTopLevelSigninBootstrap(doc) {
  try {
    const popup = doc.defaultView?.open?.(
      "about:blank",
      "_blank",
      "popup,width=420,height=160,resizable=yes,scrollbars=no",
    );
    if (!popup) return null;
    try {
      popup.document.title = t("Loading…");
      popup.document.body.textContent = t("Loading…");
    } catch (_error) { /* The popup may already have changed origin. */ }
    return popup;
  } catch (_error) { return null; }
}

function closeSigninBootstrap(popup) {
  try { popup?.close?.(); } catch (_error) { /* Already closed or cross-origin. */ }
}

function verifiedSigninResult(value, destinations) {
  if (!value || value.status !== "ready" || !["aliyun","alibabacloud"].includes(value.consolePartition) ||
      value.destination !== destinations?.[value.consolePartition] || typeof value.url !== "string") return null;
  try {
    const target = new URL(value.url, globalThis.location?.origin || "http://127.0.0.1");
    const valid = target.origin === (globalThis.location?.origin || "http://127.0.0.1") &&
      !target.search && !target.hash && !target.username && !target.password &&
      /^\/api\/sessions\/[^/]+\/resource-references\/signin-relay\/[a-f0-9]{64}$/.test(target.pathname);
    return valid ? target.href : null;
  } catch (_error) { return null; }
}


export function openResourceLinkExternal(anchor, resolution, {
  rules = RESOURCE_LINK_RULES,
  index = null,
  createSigninUrl = null,
  isCurrent = () => anchor.isConnected,
  onInvalidate = () => () => {},
} = {}) {
  const target = previewTarget(resolution, rules);
  const reference = resolution?.reference;
  const partition = reference?.consolePartition;
  if (!target || !reference?.accountScope || !["aliyun","alibabacloud"].includes(partition) ||
      typeof createSigninUrl !== "function" || !isCurrent()) return false;
  let popup;
  try {
    popup = anchor.ownerDocument.defaultView?.open?.("about:blank", "_blank");
    if (!popup) return false;
    popup.opener = null;
  } catch (_error) {
    closeSigninBootstrap(popup);
    return false;
  }
  const generation = index?.generation;
  const locator = JSON.stringify([reference.identifierKind, reference.identifier]);
  let canceled = false;
  const invalidate = () => { canceled = true; closeSigninBootstrap(popup); };
  const unsubscribeSource = onInvalidate(invalidate);
  const unsubscribeIndex = index?.subscribe?.((change) => {
    if (index.generation !== generation || change.changed.has(locator)) invalidate();
  }) || (() => {});
  const current = () => !canceled && !popup.closed && isCurrent();
  const request = {accountScope:reference.accountScope,preferredPartition:partition,
    destinations:{[partition]:target.href}};
  const navigate = async () => {
    try {
      const result = await createSigninUrl(request);
      const url = verifiedSigninResult(result, request.destinations);
      if (!url) throw new Error("resource_signin_response_invalid");
      if (current()) popup.location.replace(url);
      else closeSigninBootstrap(popup);
    } catch (_error) {
      if (current()) {
        try { popup.location.replace(target.href); } catch (_navigationError) { closeSigninBootstrap(popup); }
      } else closeSigninBootstrap(popup);
    } finally {
      unsubscribeSource();
      unsubscribeIndex();
    }
  };
  void navigate();
  return true;
}

function signinErrorMessage(error) {
  const code = error?.payload?.error?.code || error?.payload?.error?.message;
  if (["resource_signin_temporary_credentials_required","resource_signin_credentials_required",
      "resource_signin_credentials_rejected","resource_signin_role_required"].includes(code))
    return t("No cloud credential context is available. Configure credentials, then retry.");
  if (["resource_signin_account_changed","resource_signin_site_mismatch"].includes(code))
    return t("Cloud credentials for this resource are unavailable or have changed.");
  return t("Resource lookup failed. Retry or enter the missing information.");
}

/** User-initiated, allowlisted console preview. External navigation remains available in the header. */
export function openResourceLinkPreview(anchor, occurrence, resolution, {
  rules = RESOURCE_LINK_RULES,
  index = null,
  createSigninUrl = null,
  isCurrent = () => anchor.isConnected,
  onInvalidate = () => () => {},
  restoreFocus = () => anchor.isConnected && anchor.focus({preventScroll:true}),
} = {}) {
  const target = previewTarget(resolution, rules);
  if (!target) return false;
  closeActive?.();
  const doc = anchor.ownerDocument;
  const signin = buildEmbeddedSigninRequest(resolution, rules);
  // This must happen synchronously inside the trusted resource-link click.
  // Waiting for the server to mint a SigninToken first would let popup
  // blockers reject the top-level federation bootstrap.
  let signinBootstrap = createSigninUrl && signin && needsTopLevelSigninBootstrap(signin)
    ? openTopLevelSigninBootstrap(doc) : null;
  const backdrop = doc.createElement("div");
  backdrop.className = "resource-link-preview-backdrop";
  const panel = doc.createElement("section");
  panel.className = "resource-link-preview";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", t("Resource link"));
  const header = doc.createElement("header");
  const titleGroup = doc.createElement("div");
  titleGroup.className = "resource-link-preview-title";
  const title = doc.createElement("strong");
  title.textContent = occurrence.displayText || occurrence.identifier;
  title.title = title.textContent;
  const actions = doc.createElement("div");
  actions.className = "resource-link-preview-actions";
  const external = doc.createElement("a");
  external.href = target.href;
  external.target = "_blank";
  external.rel = "noopener noreferrer";
  external.textContent = target.hostname;
  external.title = target.href;
  const retry = doc.createElement("button");
  retry.type = "button";
  retry.className = "resource-link-preview-retry";
  retry.textContent = t("Retry");
  const close = doc.createElement("button");
  close.type = "button";
  close.textContent = "\u00d7";
  close.setAttribute("aria-label", t("Close"));
  close.title = t("Close");
  const body = doc.createElement("div");
  body.className = "resource-link-preview-body";
  const status = doc.createElement("p");
  status.className = "resource-link-preview-status";
  status.setAttribute("role", "status");
  status.textContent = t("Loading…");
  const frame = doc.createElement("iframe");
  frame.title = occurrence.displayText || occurrence.identifier;
  frame.referrerPolicy = "no-referrer";
  titleGroup.append(title);
  actions.append(external, retry, close);
  header.append(titleGroup, actions);
  body.append(status, frame);
  panel.append(header, body);
  backdrop.append(panel);
  doc.body.append(backdrop);
  const background = [...(doc.body.children || [])].filter(element => element !== backdrop)
    .map(element => [element, element.inert]);
  for (const [element] of background) element.inert = true;
  let disposed = false;
  const generation = index?.generation;
  const locator = JSON.stringify([occurrence.identifierKind, occurrence.identifier]);
  const unsubscribeIndex = index?.subscribe?.((change) => {
    if (index.generation !== generation || change.changed.has(locator)) dismiss();
  }) || (() => {});
  const unsubscribeSource = onInvalidate(dismiss);
  function dismiss() {
    if (disposed) return;
    disposed = true;
    closeSigninBootstrap(signinBootstrap);
    signinBootstrap = null;
    backdrop.classList.remove("is-open");
    backdrop.remove();
    for (const [element, inert] of background) element.inert = inert;
    unsubscribeIndex();
    unsubscribeSource();
    doc.removeEventListener("keydown", keydown);
    if (closeActive === dismiss) closeActive = null;
    restoreFocus();
  }
  const keydown = (event) => {
    if (event.key === "Escape") { event.stopPropagation(); dismiss(); }
  };
  close.addEventListener("click", dismiss);
  retry.addEventListener("click", () => {
    if (disposed || !isCurrent()) return;
    dismiss();
    openResourceLinkPreview(anchor, occurrence, resolution, {
      rules, index, createSigninUrl, isCurrent, onInvalidate, restoreFocus,
    });
  });
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) dismiss(); });
  doc.addEventListener("keydown", keydown);
  closeActive = dismiss;
  doc.defaultView.requestAnimationFrame(() => {
    if (!disposed && isCurrent()) backdrop.classList.add("is-open");
    else dismiss();
  });
  const load = async () => {
    let url = target.href;
    if (createSigninUrl && !signin) {
      status.textContent = t("Resource lookup failed. Retry or enter the missing information.");
      return;
    }
    if (createSigninUrl) {
      try {
        const result = await createSigninUrl(signin);
        url = verifiedSigninResult(result, signin.destinations);
        if (!url) throw new Error("resource_signin_response_invalid");
        if (signinBootstrap) {
          // Consume the one-use relay in a first-party browsing context. The
          // exact destination returned by the server is already checked
          // against the request above and is safe to load after the cookies
          // have been established.
          signinBootstrap.location.replace(url);
          await new Promise((resolve) => setTimeout(resolve, SIGNIN_BOOTSTRAP_WAIT_MS));
          if (disposed || !isCurrent()) return;
          // This delay is only a head start, not evidence that login finished.
          // Keep the helper alive until the user closes it or dismisses/retries
          // the preview; a slow federation redirect must be allowed to finish.
          url = result.destination;
        }
      } catch (error) {
        closeSigninBootstrap(signinBootstrap);
        signinBootstrap = null;
        if (!disposed) status.textContent = signinErrorMessage(error);
        return;
      }
    }
    if (disposed || !isCurrent()) return;
    frame.addEventListener("load", () => {
      if (disposed) return;
      status.hidden = true;
      // A cross-origin login/error page also fires load. It cannot establish
      // a successful bootstrap lease or justify canceling the helper window.
    }, {once:true});
    frame.src = url;
  };
  void load();
  close.focus({preventScroll:true});
  return true;
}
