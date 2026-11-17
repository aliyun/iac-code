// Scope Table readiness to its loaded detail panel. A permanent shell loader
// is not a login failure, but an intersecting loader or error dialog is unsafe.
export function isOtsTable(reference) {
  return ["ALIYUN::OTS::Table", "alicloud_ots_table"].includes(reference?.resourceType);
}

export function otsTableContentReady(text, reference) {
  if (!isOtsTable(reference) || !reference.identifier) return false;
  const value = String(text || "");
  return value.split("\n").some(line => line.trim() === reference.identifier) &&
    /数据生命周期TTL\s*[:：]\s*(?:永久|\d+)/.test(value) &&
    /基本详情/.test(value) && /数据管理/.test(value) &&
    /查询数据/.test(value) && /插入数据/.test(value) && /主键/.test(value) &&
    !/OTSNoPermissionAccess|you have no permission to access|请登录|加载中/i.test(value);
}

export async function otsTableSurfaceState(surface, reference) {
  const state = await surface.locator("body").evaluate((body, identifier) => {
    const visible = node => {
      const r = node.getBoundingClientRect();
      if (!r.width || !r.height) return false;
      for (let p = node; p; p = p.parentElement) {
        const s = getComputedStyle(p);
        if (s.visibility === "hidden" || s.display === "none" || Number(s.opacity) === 0) return false;
      }
      return true;
    };
    const headings = [...body.querySelectorAll("*")].filter(node =>
      visible(node) && !node.children.length && node.innerText?.trim() === identifier);
    const panels = new Set();
    for (const heading of headings) {
      for (let node = heading.parentElement; node && node !== body; node = node.parentElement) {
        const text = node.innerText || "";
        if (/数据生命周期TTL/.test(text) && /基本详情/.test(text) && /查询数据/.test(text) &&
            node.querySelectorAll("thead").length) {
          panels.add(node);
          break;
        }
      }
    }
    if (panels.size !== 1) return { text: "", scoped: false, visibleLoading: true };
    const panel = [...panels][0], area = panel.getBoundingClientRect();
    const visibleLoading = [...body.querySelectorAll(
      '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
    )].some(node => {
      if (!visible(node)) return false;
      const r = node.getBoundingClientRect();
      return r.left < area.right && r.right > area.left && r.top < area.bottom && r.bottom > area.top;
    });
    const errorDialog = [...body.querySelectorAll('[role="dialog"], .next-dialog, .ant-modal')].some(node =>
      visible(node) && /OTSNoPermissionAccess|you have no permission to access|错误提示|权限不足/i.test(node.innerText || ""));
    return { text: panel.innerText, scoped: true, visibleLoading, errorDialog };
  }, reference.identifier).catch(() => ({ text: "", scoped: false, visibleLoading: true }));
  return { ...state, contentReady: state.scoped && !state.visibleLoading && !state.errorDialog &&
    otsTableContentReady(state.text, reference) };
}
