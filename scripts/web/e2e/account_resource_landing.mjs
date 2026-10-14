// Account-scoped resources have no physical region. Match the API-projected
// identity on its exact domestic parent/detail route, never a search echo.
export function isAccountResourceLanding(reference) {
  return ["ALIYUN::PVTZ::ZoneRecord", "alicloud_pvtz_zone_record",
    "ALIYUN::RAM::Group", "alicloud_ram_group"].includes(reference?.resourceType);
}

function safe(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password &&
      !parsed.port && !parsed.hash ? parsed : null;
  } catch { return null; }
}

export function matchAccountResourceLanding(finalUrl, expectedUrl, reference, evidence) {
  const result = { matched: false, parentRouteMatched: false, searchFallbackSuppressed: true };
  const actual = safe(finalUrl), expected = safe(expectedUrl), attrs = reference?.attributes || {};
  if (!actual || !expected || !reference.identifier || !isAccountResourceLanding(reference)) return result;
  if (["ALIYUN::PVTZ::ZoneRecord", "alicloud_pvtz_zone_record"].includes(reference.resourceType)) {
    const route = "/privateDNS/zones/" + encodeURIComponent(attrs.ZoneId || "") + "/records";
    const hosts = ["dnsnext.console.aliyun.com", "dnsnext4service.console.aliyun.com"];
    result.parentRouteMatched = Boolean(attrs.ZoneId && hosts.includes(actual.hostname) &&
      hosts.includes(expected.hostname) && actual.pathname === route && expected.pathname === route &&
      !actual.search && !expected.search);
    if (!result.parentRouteMatched || !attrs.Rr || !attrs.Type || !attrs.Value) return result;
    const required = [attrs.Rr, attrs.Type, attrs.Value];
    const matches = (evidence.rows || []).filter(row => required.every(value =>
      row.cells.some(cell => cell.split("\n").map(s => s.trim()).includes(value))));
    result.matched = matches.length === 1;
    result.matchCount = matches.length;
    result.identity = { Rr: attrs.Rr, Type: attrs.Type, Value: attrs.Value };
    return result;
  }
  const name = attrs.GroupName;
  if (!name || attrs.GroupId !== reference.identifier) return result;
  const route = "/groups/" + encodeURIComponent(name);
  const groupRoute = url => url.pathname === route && !url.search ||
    url.pathname === "/groups/detail" && url.searchParams.getAll("groupName").length === 1 &&
    url.searchParams.get("groupName") === name && [...url.searchParams.keys()].every(k =>
      k === "groupName" || k === "activeTab") &&
    (!url.searchParams.has("activeTab") || url.searchParams.get("activeTab") === "MemberUser");
  result.parentRouteMatched = ["ram.console.aliyun.com", "ram4service.console.aliyun.com"]
    .includes(actual.hostname) && ["ram.console.aliyun.com", "ram4service.console.aliyun.com"]
    .includes(expected.hostname) && groupRoute(actual) && groupRoute(expected);
  // API GroupId is a legacy g-* key; the console renders a numeric Group ID.
  // The exact, account-unique API GroupName must also appear in its labelled
  // basic-information field, not just a breadcrumb, URL or member-search input.
  result.matched = result.parentRouteMatched && evidence.groupNames?.length === 1 &&
    evidence.groupNames[0] === name && evidence.groupIds?.length === 1 &&
    /^[0-9]+$/.test(evidence.groupIds[0]);
  result.identity = { GroupName: name, displayedGroupId: evidence.groupIds?.[0] || null };
  return result;
}

export async function accountResourceLanding(surface, finalUrl, expectedUrl, reference) {
  if (!surface) return { matched: false, searchFallbackSuppressed: true };
  const evidence = await surface.locator("body").evaluate(body => {
    const shown = node => {
      const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility === "visible" &&
        style.display !== "none" && !node.closest('header, nav, [role="navigation"]');
    };
    const values = label => {
      const found = [];
      for (const node of body.querySelectorAll("*")) {
        if (!shown(node) || node.children.length || node.innerText?.trim() !== label) continue;
        let parent = node.parentElement;
        for (let level = 0; parent && level < 3; level++, parent = parent.parentElement) {
          const lines = parent.innerText.split("\n").map(s => s.trim()).filter(Boolean);
          if (lines.length > 12) break;
          const index = lines.indexOf(label), value = lines[index + 1];
          if (value && !["复制", "编辑", "用户组名称", "用户组 ID"].includes(value)) {
            found.push(value); break;
          }
        }
      }
      return [...new Set(found)];
    };
    return {
      rows: [...body.querySelectorAll("tbody tr")].filter(shown).map(row => ({
        cells: [...row.querySelectorAll("td")].filter(shown).map(cell => cell.innerText.trim()),
      })),
      groupNames: values("用户组名称"), groupIds: values("用户组 ID"),
    };
  }).catch(() => ({ rows: [], groupNames: [], groupIds: [], unreadable: true }));
  return { ...matchAccountResourceLanding(finalUrl, expectedUrl, reference, evidence), evidence, readOnly: true };
}
