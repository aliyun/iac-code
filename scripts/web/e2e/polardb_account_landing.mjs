// A database account is a child row, never the cluster heading or global search.
export function isPolardbAccount(reference) {
  return ["ALIYUN::POLARDB::Account", "alicloud_polardb_account"].includes(reference?.resourceType);
}

export function polardbAccountContentReady(text) {
  return /账号名/.test(text) && /状态/.test(text) && /类型/.test(text) && /用户账号/.test(text);
}

export function matchPolardbAccount(url, reference, rows) {
  const result = { matched: false, parentRouteMatched: false, rowIndex: null };
  if (!isPolardbAccount(reference) || !reference.identifier || !reference.regionId ||
      !reference.attributes?.DBClusterId) return result;
  let actual;
  try { actual = new URL(url); } catch { return result; }
  const route = `/${encodeURIComponent(reference.regionId)}/cluster/` +
    `${encodeURIComponent(reference.attributes.DBClusterId)}/account/user`;
  result.parentRouteMatched = actual.protocol === "https:" &&
    ["polardb.console.aliyun.com", "polardb4service.console.aliyun.com"].includes(actual.hostname) &&
    actual.pathname === route && !actual.username && !actual.password && !actual.search && !actual.hash;
  if (!result.parentRouteMatched) return result;
  const matches = rows.flatMap((cells, rowIndex) =>
    cells[0] === reference.identifier && cells.length >= 4 && cells[1] ? [rowIndex] : []);
  if (matches.length === 1) Object.assign(result, { matched: true, rowIndex: matches[0] });
  return result;
}

export async function polardbAccountLanding(surface, url, reference) {
  const rows = surface ? await surface.locator("tbody tr").evaluateAll(nodes => nodes.map(row => {
    const shown = n => {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility === "visible";
    };
    return shown(row) ? [...row.querySelectorAll("td")].map(c => shown(c) ? c.innerText.trim() : "") : [];
  })).catch(() => []) : [];
  return { ...matchPolardbAccount(url, reference, rows), rows, readOnly: true, searchFallbackSuppressed: true };
}
