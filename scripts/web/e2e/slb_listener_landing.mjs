// A listener has no standalone detail ID: identify a visible frontend tuple
// on its exact parent. Never submit the compound ID to listener-name search.
export function isSlbListener(reference) {
  return reference?.resourceType === "alicloud_slb_listener";
}

export function slbListenerIdentity(reference) {
  if (!isSlbListener(reference) || !reference.regionId) return null;
  const match = /^(lb-[A-Za-z0-9]+):(http|https|tcp|udp):([1-9][0-9]*)$/.exec(reference.identifier || "");
  if (!match || Number(match[3]) > 65535) return null;
  const attrs = reference.attributes || {};
  for (const key of ["LoadBalancerId", "load_balancer_id"]) {
    if (attrs[key] != null && attrs[key] !== match[1]) return null;
  }
  for (const key of ["Protocol", "ListenerProtocol", "protocol"]) {
    if (attrs[key] != null && String(attrs[key]).toLowerCase() !== match[2]) return null;
  }
  for (const key of ["ListenerPort", "frontend_port"]) {
    if (attrs[key] != null && String(attrs[key]) !== match[3]) return null;
  }
  return { parent: match[1], protocol: match[2], port: match[3] };
}

export function slbListenerParentRouteMatches(url, reference) {
  const identity = slbListenerIdentity(reference);
  if (!identity) return false;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !u.username && !u.password && !u.port && !u.hash && !u.search &&
      ["slb.console.aliyun.com", "slb4service.console.aliyun.com"].includes(u.hostname) &&
      u.pathname === `/slb/${encodeURIComponent(reference.regionId)}/slbs/${identity.parent}/listeners`;
  } catch { return false; }
}

export function slbListenerMatchingRows(state, reference) {
  const identity = slbListenerIdentity(reference);
  if (!identity || state.visibleLoading) return [];
  const tuple = new RegExp(`^${identity.protocol}\\s*[:/：]?\\s*${identity.port}$`, "i");
  return (state.rows || []).filter(row => {
    // Header-bound frontend cell only: a backend port, filter chip, breadcrumb,
    // empty-table message or incidental whole-body text is not an identity.
    const columns = (row.headers || []).map((header, index) => ({ header: header.replace(/\s/g, ""), index }))
      .filter(column => ["前端协议/端口", "前端协议及端口", "FrontendProtocol/Port"].includes(column.header));
    return columns.length === 1 && row.cells.length === row.headers.length &&
      tuple.test(String(row.cells[columns[0].index] || "").trim());
  });
}

export function slbListenerSurfaceState(body) {
  const visible = node => {
    const rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) return false;
    for (let parent = node; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const nodes = [...body.querySelectorAll("tbody tr")].filter(visible);
  const rows = nodes.map(row => {
    // art-table renders the header and body in sibling HTML tables. Bind to
    // that single nearest component, never to an unrelated page-wide header.
    const table = row.closest('.art-table, .next-table, .ant-table, [role="table"]') || row.closest("table");
    const headers = table ? [...table.querySelectorAll('thead th, [role="columnheader"]')].filter(visible) : [];
    return { headers: headers.map(node => node.innerText.trim()),
      cells: [...row.querySelectorAll("td")].filter(visible).map(node => node.innerText.trim()) };
  });
  const loaders = [...body.querySelectorAll(
    '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
  )].filter(visible);
  return { rows, visibleLoading: !nodes.length || loaders.some(node => nodes.some(row => {
    const a = row.getBoundingClientRect(), b = node.getBoundingClientRect();
    return b.left < a.right && b.right > a.left && b.top < a.bottom && b.bottom > a.top;
  })) };
}

export async function slbListenerLanding(surface, initialUrl, reference, deadline) {
  const result = { matched: false, parentRouteMatched: slbListenerParentRouteMatches(initialUrl, reference),
    searchFallbackSuppressed: true, rows: [], visibleLoading: true, finalUrl: initialUrl, bodyTextAfter: "" };
  if (!surface || !result.parentRouteMatched) return result;
  let previous = "", stable = 0;
  while (Date.now() < deadline) {
    const state = await surface.locator("body").evaluate(slbListenerSurfaceState)
      .catch(() => ({ rows: [], visibleLoading: true }));
    Object.assign(result, state, { finalUrl: surface.url() });
    const fingerprint = JSON.stringify(state);
    stable = fingerprint === previous ? stable + 1 : 0; previous = fingerprint;
    if (stable >= 2 && slbListenerParentRouteMatches(result.finalUrl, reference) &&
        slbListenerMatchingRows(state, reference).length === 1) {
      result.matched = true;
      result.bodyTextAfter = await surface.locator("body").innerText({ timeout: 1000 }).catch(() => "");
      break;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return result;
}
