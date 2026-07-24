import { t } from "./i18n.js?v=web-repl-ui-277";
// 懒加载 mermaid.js:首次真正渲染时才注入 vendor 脚本;text→SVG,失败回退 <pre> 原文。
let mermaidReady = null;
let idSeq = 0;

// 按 app 当前主题选 mermaid 内置主题:ivory=浅色 → default;其余(graphite/midnight/
// evergreen/sepia,均为深色)→ dark。深色主题下 mermaid 让子图/节点底盒变深、文字与
// 线条变浅,整图在深色弹窗上自洽可读——避免浅线穿浅底盒或深线压深背景的跨表面失配。
function mermaidThemeForApp() {
  return document.documentElement.getAttribute("data-theme") === "ivory" ? "default" : "dark";
}

function loadMermaid() {
  if (mermaidReady) return mermaidReady;
  mermaidReady = new Promise((resolve, reject) => {
    if (window.mermaid) return resolve(window.mermaid);
    const script = document.createElement("script");
    script.src = "/static/js/vendor/mermaid.min.js?v=10.9.3";
    script.onload = () => {
      window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
      resolve(window.mermaid);
    };
    script.onerror = () => reject(new Error("mermaid load failed"));
    document.head.append(script);
  });
  return mermaidReady;
}

// 把 mermaid 文本渲染进 container;失败(加载失败/语法错误)时回退显示原文 <pre>。
export async function renderMermaid(container, source) {
  const fallback = () => {
    const pre = document.createElement("pre");
    pre.className = "mermaid-fallback";
    pre.textContent = source || "";
    container.replaceChildren(pre);
  };
  if (!source) return fallback();
  try {
    const mermaid = await loadMermaid();
    // 每次渲染前按当前主题设 mermaid 主题:主题在图渲染后可能已切换,且 initialize 全局单次,
    // 故逐次重设,保证深/浅主题各拿到自洽配色。
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: mermaidThemeForApp() });
    idSeq += 1;
    const { svg } = await mermaid.render(`mmd-${idSeq}`, source);
    const wrap = document.createElement("div");
    wrap.className = "mermaid-diagram";
    wrap.innerHTML = svg;
    container.replaceChildren(wrap);
  } catch {
    fallback();
  }
}

// 多视图渲染:>1 视图建标签栏(点标签切换),<=1 视图退回单张 renderMermaid(零回归)。
export async function renderMermaidViews(container, views, opts = {}) {
  const list = Array.isArray(views) ? views.filter((v) => v && v.mermaidSource) : [];
  if (!list.length) {
    container.replaceChildren();
    if (opts.fallbackSource) await renderMermaid(container, opts.fallbackSource);
    return;
  }
  if (list.length === 1) {
    const body = document.createElement("div");
    container.replaceChildren(body);
    await renderMermaid(body, list[0].mermaidSource);
    return;
  }
  const tabs = document.createElement("div");
  tabs.className = "diagram-view-tabs";
  const body = document.createElement("div");
  body.className = "diagram-view-body";
  const show = async (i) => {
    [...tabs.children].forEach((el, j) => {
      el.className = "diagram-view-tab" + (j === i ? " is-active" : "");
    });
    await renderMermaid(body, list[i].mermaidSource);
  };
  list.forEach((v, i) => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "diagram-view-tab";
    tab.textContent = v.title || v.id || t("View {n}", { n: i + 1 });
    tab.addEventListener("click", () => {
      show(i);
    });
    tabs.append(tab);
  });
  container.replaceChildren(tabs, body);
  await show(0);
}

// 架构图下方的询价块:总价 + 逐资源明细(复用候选卡的 .pipeline-cost-items 样式)。
// item 来自 /outputs 的 diagram dict:命中询价时带 totalMonthlyCost(str)与 costItems
// ([{name, spec, monthly_cost}]);缺价(询价失败/尚未询价)则两者皆空 → 渲淡色「暂无询价信息」。
export function renderDiagramPrice(item) {
  const wrap = document.createElement("div");
  wrap.className = "diagram-price";
  const total = item && typeof item.totalMonthlyCost === "string" ? item.totalMonthlyCost : "";
  const items = item && Array.isArray(item.costItems) ? item.costItems : [];
  if (!total && items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "diagram-price-empty";
    empty.textContent = t("No pricing information");
    wrap.append(empty);
    return wrap;
  }
  const totalRow = document.createElement("div");
  totalRow.className = "diagram-price-total";
  const label = document.createElement("span");
  label.className = "diagram-price-label";
  label.textContent = t("Estimated monthly cost");
  const value = document.createElement("span");
  value.className = "diagram-price-value";
  value.textContent = total || "—";
  totalRow.append(label, value);
  wrap.append(totalRow);
  if (items.length) {
    const ul = document.createElement("ul");
    ul.className = "pipeline-cost-items";
    for (const it of items) {
      const li = document.createElement("li");
      const nm = (it && (it.name || it.spec)) || "";
      const cost = (it && it.monthly_cost) || "";
      li.textContent = cost ? `${nm} — ${cost}` : nm;
      ul.append(li);
    }
    wrap.append(ul);
  }
  return wrap;
}
