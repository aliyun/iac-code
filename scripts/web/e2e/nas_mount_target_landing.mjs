// NAS mount pages expose the domain in a read-only table input, not necessarily
// body.innerText. Match the exact domain on its exact parent route, never the
// parent alone or the console's global product-search field.
export function isNasMountTarget(reference) {
  return ["ALIYUN::NAS::MountTarget", "alicloud_nas_mount_target"].includes(reference?.resourceType);
}

export function matchNasMountTarget(finalUrl, expectedUrl, reference, rows) {
  const result = { matched: false, parentRouteMatched: false, rowIndex: null, cellIndex: null };
  if (!isNasMountTarget(reference) || !reference.identifier || !reference.regionId ||
      !reference.attributes?.FileSystemId) return result;
  let actual, expected;
  try { actual = new URL(finalUrl); expected = new URL(expectedUrl); } catch { return result; }
  const hosts = new Set(["nasnext.console.aliyun.com", "nasnext4service.console.aliyun.com"]);
  const route = `/${encodeURIComponent(reference.regionId)}/filesystem/` +
    `${encodeURIComponent(reference.attributes.FileSystemId)}/mount`;
  result.parentRouteMatched = actual.protocol === "https:" && expected.protocol === "https:" &&
    hosts.has(actual.hostname) && hosts.has(expected.hostname) && !actual.username && !actual.password &&
    !expected.username && !expected.password && actual.pathname === route && expected.pathname === route &&
    !actual.hash && !expected.hash;
  if (!result.parentRouteMatched) return result;
  const matches = rows.flatMap((row, rowIndex) => {
    const cells = row.cells || [];
    const cellIndex = cells.findIndex(cell => cell.values?.includes(reference.identifier));
    return cellIndex < 0 ? [] : [{ rowIndex, cellIndex }];
  });
  if (matches.length === 1) Object.assign(result, matches[0], { matched: true });
  return result;
}

export async function nasMountTargetLanding(surface, finalUrl, expectedUrl, reference) {
  if (!surface) return { matched: false, parentRouteMatched: false, rows: [] };
  const rowLocator = surface.locator("tbody tr");
  const rows = await rowLocator.evaluateAll(elements => elements.map(row => {
    const shown = node => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility === "visible";
    };
    return { cells: shown(row) ? [...row.querySelectorAll("td")].map(cell => ({
      values: shown(cell) ? [...new Set([
        (cell.innerText || "").trim(),
        ...[...cell.querySelectorAll("input[readonly],textarea[readonly]")]
          .filter(node => shown(node) && node.type !== "hidden").map(node => node.value.trim()),
        ...[...cell.querySelectorAll("[title]")].filter(shown).map(node => node.title.trim()),
      ].filter(Boolean))] : [],
    })) : [] };
  })).catch(() => []);
  const result = { ...matchNasMountTarget(finalUrl, expectedUrl, reference, rows), rows,
    searchFallbackSuppressed: true, readOnly: true };
  // Scroll the proved cell into view for the original screenshot. No typing,
  // navigation, forced click, or resource/account mutation is allowed here.
  if (result.matched) await rowLocator.nth(result.rowIndex).locator("td").nth(result.cellIndex)
    .scrollIntoViewIfNeeded({ timeout: 1000 }).catch(() => {});
  return result;
}
