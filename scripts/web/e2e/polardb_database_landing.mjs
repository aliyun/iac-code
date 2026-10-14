// Exact database rows are not cluster headings, global search text or URL IDs.
export function isPolardbDatabase(reference) {
  return reference?.resourceType === "alicloud_polardb_database";
}

export function matchPolardbDatabase(url, reference, rows) {
  const result = { matched: false, parentRouteMatched: false, rowIndex: null };
  if (!isPolardbDatabase(reference) || !reference.identifier || !reference.regionId ||
      !reference.attributes?.DBClusterId) return result;
  let actual;
  try { actual = new URL(url); } catch { return result; }
  const route = `/${encodeURIComponent(reference.regionId)}/cluster/` +
    `${encodeURIComponent(reference.attributes.DBClusterId)}/database/list`;
  result.parentRouteMatched = actual.protocol === "https:" &&
    ["polardb.console.aliyun.com", "polardb4service.console.aliyun.com"].includes(actual.hostname) &&
    actual.pathname === route && !actual.username && !actual.password && !actual.port && !actual.search && !actual.hash;
  if (!result.parentRouteMatched) return result;
  const matches = rows.flatMap((cells, i) => cells[0] === reference.identifier && cells.length >= 5 &&
    /^(运行中|Running)$/i.test(cells[1]) && cells[2] && cells[3] && /SQL/.test(cells[4]) ? [i] : []);
  if (matches.length === 1) Object.assign(result, { matched: true, rowIndex: matches[0] });
  return result;
}

export async function polardbDatabaseLanding(surface, url, reference) {
  const rows = surface ? await surface.locator("tbody tr").evaluateAll(nodes => nodes.map(row => {
    const shown = n => {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility === "visible";
    };
    return shown(row) ? [...row.querySelectorAll("td")].map(c => shown(c) ? c.innerText.trim() : "") : [];
  })).catch(() => []) : [];
  return { ...matchPolardbDatabase(url, reference, rows), rows, readOnly: true, searchFallbackSuppressed: true };
}

export function polardbClusterContentReady(text, reference) {
  if (!["ALIYUN::POLARDB::DBCluster", "alicloud_polardb_cluster"].includes(reference?.resourceType)) return false;
  const regions = {
    "cn-hangzhou": /杭州|Hangzhou/i, "cn-shanghai": /上海|Shanghai/i,
    "cn-beijing": /北京|Beijing/i, "cn-shenzhen": /深圳|Shenzhen/i,
    "cn-qingdao": /青岛|Qingdao/i, "cn-zhangjiakou": /张家口|Zhangjiakou/i,
    "cn-chengdu": /成都|Chengdu/i, "cn-wulanchabu": /乌兰察布|Ulanqab/i,
  };
  const region = regions[reference.regionId];
  if (!region) return false;
  const lines = String(text).split(/\n/).map(s => s.trim()).filter(Boolean);
  const start = lines.indexOf("集群ID");
  // Each value must be in the loaded basic-information panel, not in its heading.
  const panel = start < 0 ? [] : lines.slice(start, lines.indexOf("配置信息", start) > start
    ? lines.indexOf("配置信息", start) : undefined);
  const createdIndex = panel.indexOf("创建时间");
  const created = createdIndex < 0 ? null : panel[createdIndex + 1]?.match(
    /^(\d{4})(?:-(\d{1,2})-(\d{1,2})|年(\d{1,2})月(\d{1,2})日)(?:\s+\d{2}:\d{2}:\d{2})?$/,
  );
  const year = Number(created?.[1]), month = Number(created?.[2] || created?.[4]),
    day = Number(created?.[3] || created?.[5]);
  const date = new Date(Date.UTC(year, month - 1, day));
  const validCreationDate = Boolean(created) && date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  return panel.includes(reference.identifier) &&
    panel.some(s => region.test(s)) &&
    panel.some(s => /PostgreSQL|MySQL|Oracle/i.test(s)) &&
    validCreationDate;
}
