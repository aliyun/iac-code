// Readiness is scoped to the loaded instance-information card, not the console
// shell's permanent Loading text or navigation skeletons. No navigation here.
export function isOtsInstance(reference) {
  return ["ALIYUN::OTS::Instance", "alicloud_ots_instance"].includes(reference?.resourceType);
}

export function otsInstanceContentReady(text, reference) {
  if (!isOtsInstance(reference) || !reference.identifier) return false;
  const value = String(text || "");
  const name = value.match(/实例名称\s*[:：]\s*([\s\S]*?)实例别名\s*[:：]/)?.[1];
  return Boolean(name && name.replace(/\s/g, "") === reference.identifier &&
    /实例基础信息/.test(value) && /实例模型\s*[:：]\s*(?:CU模式|Tunnel模式)/.test(value) &&
    /创建时间\s*[:：]\s*\d{4}年\d{1,2}月\d{1,2}日/.test(value) &&
    /存储规格\s*[:：]\s*(?:存储|容量|高性能)/.test(value) &&
    /冗余类型\s*[:：]\s*(?:同城冗余|本地冗余|ZRS|LRS)/.test(value));
}

export async function otsInstanceSurfaceState(surface, reference) {
  const state = await surface.locator("body").evaluate(body => {
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
      visible(node) && !node.children.length && node.innerText?.trim() === "实例基础信息");
    const cards = new Set();
    for (const heading of headings) {
      for (let node = heading.parentElement; node && node !== body; node = node.parentElement) {
        const text = node.innerText || "";
        if (/实例名称/.test(text) && /创建时间/.test(text) && /存储规格/.test(text) && /冗余类型/.test(text)) {
          cards.add(node);
          break;
        }
      }
    }
    if (cards.size !== 1) return { text: "", scoped: false, visibleLoading: true };
    const card = [...cards][0], area = card.getBoundingClientRect();
    const visibleLoading = [...body.querySelectorAll(
      '[aria-busy="true"], [role="progressbar"], .ant-spin-spinning, .next-loading-indicator, .loading-spinner, [class*="skeleton" i]',
    )].some(node => {
      if (!visible(node)) return false;
      const r = node.getBoundingClientRect();
      return r.left < area.right && r.right > area.left && r.top < area.bottom && r.bottom > area.top;
    });
    return { text: card.innerText, scoped: true, visibleLoading };
  }).catch(() => ({ text: "", scoped: false, visibleLoading: true }));
  return { ...state, contentReady: state.scoped && !state.visibleLoading && otsInstanceContentReady(state.text, reference) };
}
