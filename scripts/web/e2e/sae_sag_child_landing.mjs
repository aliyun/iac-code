// Match visible child rows using locators projected from the SAME API object.
// No global product search, hidden row keys, navigation or account mutation.
export function childListKind(reference) {
  if (reference?.resourceType === "alicloud_sae_namespace") return "sae_namespace";
  if (["ALIYUN::SAG::ACLRule", "alicloud_sag_acl_rule"].includes(reference?.resourceType)) return "sag_rule";
  return null;
}

export function matchChildList(url, reference, state) {
  const result = { matched: false, parentRouteMatched: false, rowIndex: null };
  const kind = childListKind(reference), a = reference?.attributes || {};
  if (!kind || !reference.identifier || !reference.regionId) return result;
  let parsed;
  try { parsed = new URL(url); } catch { return result; }
  const hosts = kind === "sae_namespace"
    ? ["saenext.console.aliyun.com", "saenext4service.console.aliyun.com"]
    : ["smartag.console.aliyun.com", "smartag4service.console.aliyun.com"];
  const route = kind === "sae_namespace" ? `/${encodeURIComponent(reference.regionId)}/namespace`
    : `/acl/${encodeURIComponent(reference.regionId)}/acls/${encodeURIComponent(a.AclId || "")}`;
  result.parentRouteMatched = parsed.protocol === "https:" && hosts.includes(parsed.hostname) &&
    !parsed.username && !parsed.password && !parsed.port && !parsed.hash && !parsed.search && parsed.pathname === route;
  if (!result.parentRouteMatched || state.visibleLoading || !state.tableVisible) return result;
  const headers = state.headers || [];
  const column = label => headers.indexOf(label) === headers.lastIndexOf(label) ? headers.indexOf(label) : -1;
  let matches;
  if (kind === "sae_namespace") {
    if (!a.NameSpaceShortId || !a.NamespaceName || a.NamespaceId !== reference.identifier ||
        reference.identifier !== `${reference.regionId}:${a.NameSpaceShortId}` ||
        a.RegionId !== reference.regionId) return result;
    const index = column("命名空间名称/ID");
    if (index < 0) return result;
    matches = (state.rows || []).flatMap((row, rowIndex) => {
      const lines = (row.cells[index] || "").split("\n").map(s => s.trim()).filter(Boolean);
      return lines.length === 2 && lines[0] === a.NamespaceName && lines[1] === a.NameSpaceShortId ? [rowIndex] : [];
    });
  } else {
    if (!/^acl-/.test(a.AclId || "") || !/^acr-/.test(reference.identifier) || !a.Name ||
        !["in", "out"].includes(a.Direction) || !["accept", "drop"].includes(a.Policy) ||
        !a.IpProtocol || !a.SourceCidr || !a.DestCidr || !a.SourcePortRange || !a.DestPortRange ||
        a.Priority == null) return result;
    const expected = [["实例名称", a.Name], ["授权策略", a.Policy === "accept" ? "允许" : "拒绝"],
      ["协议类型", String(a.IpProtocol)], ["规则方向", a.Direction === "in" ? "入方向" : "出方向"],
      ["源地址", String(a.SourceCidr)], ["目的地址", String(a.DestCidr)],
      ["源端口范围", String(a.SourcePortRange)], ["目的端口范围", String(a.DestPortRange)],
      ["优先级", String(a.Priority)]];
    if (expected.some(([label]) => column(label) < 0)) return result;
    matches = (state.rows || []).flatMap((row, index) =>
      expected.every(([label, value]) => row.cells[column(label)] === value) ? [index] : []);
  }
  if (matches.length === 1) Object.assign(result, { matched: true, rowIndex: matches[0] });
  return result;
}

export async function childListLanding(surface, url, reference) {
  const empty = { tableVisible: false, visibleLoading: true, headers: [], rows: [] };
  const state = surface ? await surface.locator("body").evaluate(body => {
    const visible = node => {
      const rect = node.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      for (let n = node; n; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (s.display === "none" || s.visibility !== "visible" || Number(s.opacity) === 0) return false;
      }
      return true;
    };
    const tables = [...body.querySelectorAll("table")].filter(visible);
    const rows = tables.flatMap(t => [...t.querySelectorAll("tbody tr")].filter(visible));
    const headerRows = tables.flatMap(t => [...t.querySelectorAll("thead tr")].filter(visible))
      .map(row => [...row.querySelectorAll("th")].map(n => visible(n) ? n.innerText.trim() : ""))
      .filter(headers => headers.includes("命名空间名称/ID") || headers.includes("授权策略"));
    return { tableVisible: tables.length > 0,
      headers: headerRows.length === 1 ? headerRows[0] : [],
      visibleLoading: [...body.querySelectorAll(
        '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner',
      )].some(visible),
      rows: rows.map(row => ({ cells: [...row.querySelectorAll("td")].map(n => visible(n) ? n.innerText.trim() : "") })) };
  }).catch(() => empty) : empty;
  const result = { ...matchChildList(url, reference, state), ...state,
    readOnly: true, searchFallbackSuppressed: true };
  if (result.matched) await surface.locator("tbody tr:visible").nth(result.rowIndex)
    .scrollIntoViewIfNeeded({ timeout: 1000 }).catch(() => {});
  return result;
}
