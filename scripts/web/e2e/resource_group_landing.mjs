// Resource Manager retains shell loaders after the exact group overview is
// loaded. Inspect only its visible detail container; never accept a URL alone.
export function isResourceGroup(reference) {
  return ["ALIYUN::ResourceManager::ResourceGroup", "alicloud_resource_manager_resource_group"]
    .includes(reference?.resourceType);
}

export function resourceGroupContentReady(text, reference) {
  if (!isResourceGroup(reference) || !reference.identifier || !reference.attributes?.DisplayName) return false;
  const lines = String(text || "").split("\n").map(s => s.trim()).filter(Boolean);
  return lines.some(s => s.split(/[/\s]+/).includes(reference.identifier)) &&
    lines.includes(reference.attributes.DisplayName) && ["概览", "资源管理", "权限管理", "设置", "总量排序"]
      .every(label => lines.some(s => s === label || s.startsWith(label))) &&
    lines.some((s, i) => /^(?:资源编排|云服务器ECS|日志服务)$/.test(s) && /^\d+$/.test(lines[i + 1] || ""));
}

export async function resourceGroupSurfaceState(surface, reference) {
  const state = await surface.locator("body").evaluate((body, ref) => {
    const visible = node => {
      const rect = node.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      for (let p = node; p; p = p.parentElement) {
        const s = getComputedStyle(p);
        if (s.visibility === "hidden" || s.display === "none" || Number(s.opacity) === 0) return false;
      }
      return true;
    };
    const headings = [...body.querySelectorAll("*")].filter(n => visible(n) &&
      (!n.children.length || /^H[1-3]$/.test(n.tagName) || n.getAttribute("role") === "heading") &&
      n.innerText?.trim() === ref.attributes?.DisplayName);
    const cards = new Set();
    for (const heading of headings) {
      for (let n = heading.parentElement; n && n !== body; n = n.parentElement) {
        const text = n.innerText || "";
        if (text.includes(ref.identifier) && /权限管理/.test(text) && /总量排序/.test(text)) {
          cards.add(n); break;
        }
      }
    }
    if (cards.size !== 1) return { text: "", scoped: false, visibleLoading: true };
    const card = [...cards][0], area = card.getBoundingClientRect();
    const visibleLoading = [...body.querySelectorAll(
      '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
    )].some(n => {
      if (!visible(n)) return false;
      const r = n.getBoundingClientRect();
      return r.left < area.right && r.right > area.left && r.top < area.bottom && r.bottom > area.top;
    });
    return { text: card.innerText, scoped: true, visibleLoading };
  }, reference).catch(() => ({ text: "", scoped: false, visibleLoading: true }));
  return { ...state, contentReady: state.scoped && !state.visibleLoading &&
    resourceGroupContentReady(state.text, reference) };
}
