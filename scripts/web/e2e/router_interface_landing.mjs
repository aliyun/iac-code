// Read-only child lookup on the API-projected VBR parent. Neither a parent
// heading nor an ID echoed into global product search identifies an interface.
export function isVbrRouterInterface(reference) {
  return ["ALIYUN::VPC::RouterInterface", "alicloud_router_interface"].includes(reference?.resourceType) &&
    reference.attributes?.RouterType === "VBR" && /^vbr-/.test(reference.attributes?.RouterId || "") &&
    /^ri-/.test(reference.identifier || "") && Boolean(reference.regionId);
}

export function vbrParentRouteMatches(url, reference) {
  if (!isVbrRouterInterface(reference)) return false;
  try {
    const u = new URL(url), parent = reference.attributes.RouterId;
    return u.protocol === "https:" && !u.username && !u.password && !u.port && !u.hash &&
      ["expressconnect.console.aliyun.com", "expressconnect4service.console.aliyun.com"].includes(u.hostname) &&
      u.pathname === "/vbr/" + encodeURIComponent(reference.regionId) + "/detail/" + encodeURIComponent(parent) &&
      [...u.searchParams.keys()].every(k => ["RouterId", "TabKey"].includes(k)) &&
      (!u.searchParams.has("RouterId") || u.searchParams.getAll("RouterId").length === 1 &&
        u.searchParams.get("RouterId") === parent) &&
      (!u.searchParams.has("TabKey") || u.searchParams.getAll("TabKey").length === 1 &&
        u.searchParams.get("TabKey") === "vbr2vpc");
  } catch { return false; }
}

export function routerInterfaceRowsMatch(state, reference) {
  if (!isVbrRouterInterface(reference) || !state.tabSelected || state.visibleLoading) return false;
  const matches = (state.rows || []).filter(row => row.cells.some(cell => {
    const lines = cell.split("\n").map(s => s.trim()).filter(Boolean);
    return lines.includes(reference.identifier) && lines.includes(reference.attributes.RouterId);
  }));
  return matches.length === 1;
}

export async function routerInterfaceLanding(surface, initialUrl, reference, deadline) {
  const result = { matched: false, parentRouteMatched: vbrParentRouteMatches(initialUrl, reference),
    searchFallbackSuppressed: true, tabClicked: false, tabSelected: false, visibleLoading: true, rows: [],
    finalUrl: initialUrl, bodyTextAfter: "" };
  if (!surface || !result.parentRouteMatched) return result;
  const tab = surface.getByRole("tab", { name: "对等连接", exact: true });
  if (await tab.count() !== 1 || !await tab.isVisible().catch(() => false)) return result;
  if (await tab.getAttribute("aria-selected") !== "true") {
    try {
      await tab.click({ timeout: Math.max(1, Math.min(5000, deadline - Date.now())) });
    } catch { return result; }
    result.tabClicked = true;
  }
  let previous = "", stable = 0;
  while (Date.now() < deadline) {
    const state = await surface.locator("body").evaluate(body => {
      const visible = n => {
        const r = n.getBoundingClientRect();
        if (!r.width || !r.height) return false;
        for (let p = n; p; p = p.parentElement) {
          const s = getComputedStyle(p);
          if (s.visibility === "hidden" || s.display === "none" || Number(s.opacity) === 0) return false;
        }
        return true;
      };
      const tabs = [...body.querySelectorAll('[role="tab"]')].filter(n =>
        visible(n) && n.innerText?.trim() === "对等连接");
      const rows = [...body.querySelectorAll("tbody tr")].filter(visible);
      const loaders = [...body.querySelectorAll(
        '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
      )].filter(visible);
      return { tabSelected: tabs.length === 1 && tabs[0].getAttribute("aria-selected") === "true",
        rows: rows.map(row => ({ cells: [...row.querySelectorAll("td")].filter(visible).map(n => n.innerText.trim()) })),
        visibleLoading: !rows.length || loaders.some(n => rows.some(row => {
          const a = row.getBoundingClientRect(), b = n.getBoundingClientRect();
          return b.left < a.right && b.right > a.left && b.top < a.bottom && b.bottom > a.top;
        })) };
    }).catch(() => ({ tabSelected: false, visibleLoading: true, rows: [] }));
    Object.assign(result, state, { finalUrl: surface.url() });
    const fingerprint = JSON.stringify(state);
    stable = fingerprint === previous ? stable + 1 : 0; previous = fingerprint;
    if (stable >= 2 && vbrParentRouteMatches(result.finalUrl, reference) &&
        routerInterfaceRowsMatch(state, reference)) {
      result.matched = true;
      result.bodyTextAfter = await surface.locator("body").innerText({ timeout: 1000 }).catch(() => "");
      break;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return result;
}
