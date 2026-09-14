import { t } from "./i18n.js?v=web-repl-ui-277";
import { renderMermaid } from "./mermaid_render.js?v=arch-diagram-v5";
import { getArchitectureDiagramRenderer } from "./api.js?v=web-repl-ui-313";

const RENDERER_EVENT = "iac-code:architecture-renderer-changed";
const THEME_EVENT = "iac-code:theme-changed";
const FRAME_TIMEOUT_MS = 7000;
const LAYOUT_TIMEOUT_MS = 2500;
const MERMAID_TIMEOUT_MS = 7000;
const MAX_LAYOUT_CACHE = 24;
let requestSequence = 0;
let workerSourcePromise = null;
let frameSourcesPromise = null;
const activeRecords = new Set();
const recordsByContainer = new WeakMap();
const layoutCache = new Map();
let disconnectObserver = null;

function nextRequestId() {
  requestSequence += 1;
  const random = new Uint32Array(4);
  crypto.getRandomValues(random);
  return `architecture-${requestSequence}-${[...random].map((value) => value.toString(16).padStart(8, "0")).join("")}`;
}

function preferredRenderer() {
  if (!document.body) return "mermaid";
  return document.body?.dataset.architectureDiagramRenderer === "mermaid" ? "mermaid" : "eraser";
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "graphite";
}

function usableGraph(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      value.version === 1 &&
      Array.isArray(value.nodes) &&
      Array.isArray(value.containers) &&
      Array.isArray(value.edges),
  );
}

function workerSource() {
  if (!workerSourcePromise) {
    workerSourcePromise = fetch("/static/js/eraser_layout_worker.js?v=iac-layered-v7", {
      cache: "force-cache",
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok) throw new Error("Eraser layout worker failed to load");
        return response.text();
      })
      .catch((error) => {
        workerSourcePromise = null;
        throw error;
      });
  }
  return workerSourcePromise;
}

function frameSources() {
  if (!frameSourcesPromise) {
    frameSourcesPromise = Promise.all([
      fetch("/static/js/vendor/eraser-diagrams.min.js?v=0.1.0-iac1", {
        cache: "force-cache",
        credentials: "same-origin",
      }),
      fetch("/static/js/eraser_frame.js?v=eraser-browser-v7", {
        cache: "force-cache",
        credentials: "same-origin",
      }),
    ])
      .then(async ([rendererResponse, frameResponse]) => {
        if (!rendererResponse.ok || !frameResponse.ok) throw new Error("Eraser frame scripts failed to load");
        return { rendererSource: await rendererResponse.text(), frameSource: await frameResponse.text() };
      })
      .catch((error) => {
        frameSourcesPromise = null;
        throw error;
      });
  }
  return frameSourcesPromise;
}

function layoutKey(graph, theme) {
  const geometryTheme = theme === "ivory" ? "light" : "dark";
  return `eraser@0.1.0|iac-layered-v7|font-v3|icons-v1|${geometryTheme}|${JSON.stringify(graph)}`;
}

function rememberLayout(key, layout) {
  if (!layout || typeof layout !== "object") return;
  layoutCache.delete(key);
  layoutCache.set(key, layout);
  while (layoutCache.size > MAX_LAYOUT_CACHE) layoutCache.delete(layoutCache.keys().next().value);
}

function disposeFrame(record) {
  const cancelFrame = record.cancelFrame;
  record.cancelFrame = null;
  cancelFrame?.();
  record.port?.postMessage({ type: "cancel" });
  record.port?.close();
  record.port = null;
  record.frame?.remove();
  record.frame = null;
}

function unregister(record) {
  record.generation += 1;
  disposeFrame(record);
  activeRecords.delete(record);
  if (recordsByContainer.get(record.container) === record) recordsByContainer.delete(record.container);
}

export function disposeArchitectureDiagram(container) {
  const record = recordsByContainer.get(container);
  if (record) unregister(record);
}

function fallbackNotice(message) {
  const notice = document.createElement("p");
  notice.className = "architecture-diagram-fallback";
  notice.setAttribute("role", "status");
  notice.textContent = message;
  return notice;
}

function notifyIntrinsicSize(record, width, height = 0) {
  const normalizedWidth = Number(width);
  const normalizedHeight = Number(height);
  if (!Number.isFinite(normalizedWidth) || normalizedWidth <= 0) return;
  record.onSize?.({
    width: Math.ceil(normalizedWidth),
    height: Number.isFinite(normalizedHeight) && normalizedHeight > 0 ? Math.ceil(normalizedHeight) : 0,
  });
}

function mermaidIntrinsicWidth(body) {
  const svg = body.querySelector?.(".mermaid-diagram svg");
  if (!svg) return body.scrollWidth || 0;
  const viewBoxWidth = Number(svg.viewBox?.baseVal?.width) || 0;
  const viewBox = String(svg.getAttribute?.("viewBox") || "").trim().split(/[ ,]+/).map(Number);
  const attributeWidth = Number.parseFloat(svg.getAttribute?.("width") || "") || 0;
  return Math.max(viewBoxWidth, viewBox.length === 4 && Number.isFinite(viewBox[2]) ? viewBox[2] : 0, attributeWidth);
}

async function showMermaid(record, view, generation, warning = "") {
  disposeFrame(record);
  const body = document.createElement("div");
  const source = view?.mermaidSource || record.fallbackSource || "";
  let timer = null;
  try {
    await Promise.race([
      renderMermaid(body, source),
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("Mermaid renderer timed out")), MERMAID_TIMEOUT_MS);
      }),
    ]);
  } catch (_error) {
    body.className = "mermaid-fallback";
    body.textContent = source;
  } finally {
    clearTimeout(timer);
  }
  if (generation !== record.generation || record.container.isConnected === false) return;
  if (warning) record.target.replaceChildren(fallbackNotice(warning), body);
  else record.target.replaceChildren(body);
  notifyIntrinsicSize(record, mermaidIntrinsicWidth(body));
}

async function renderEraserFrame(record, view, generation) {
  const graph = view.graph;
  const theme = currentTheme();
  const key = layoutKey(graph, theme);
  const [source, runtimeSources] = await Promise.all([workerSource(), frameSources()]);
  if (generation !== record.generation || record.container.isConnected === false) {
    throw new Error("Architecture render was cancelled");
  }
  const frame = document.createElement("iframe");
  const requestId = nextRequestId();
  frame.className = "architecture-eraser-frame";
  frame.title = view.title || t("Architecture diagram");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      window.removeEventListener("message", onReady);
      if (record.cancelFrame === cancel) record.cancelFrame = null;
      if (error) reject(error);
      else resolve();
    };
    const cancel = () => finish(new Error("Architecture render was cancelled"));
    record.cancelFrame = cancel;
    const timer = setTimeout(() => {
      frame.remove();
      if (record.frame === frame) record.frame = null;
      finish(new Error("Eraser renderer timed out"));
    }, FRAME_TIMEOUT_MS);
    const onReady = (event) => {
      if (event.data?.type !== "iac-eraser-ready" || event.data.requestId !== requestId) return;
      if (generation !== record.generation || !event.source || typeof event.source.postMessage !== "function") return;
      const channel = new MessageChannel();
      record.port = channel.port1;
      channel.port1.onmessage = (portEvent) => {
        if (portEvent.data?.requestId !== requestId || generation !== record.generation) return;
        if (portEvent.data.type === "rendered") {
          const height = Math.max(240, Math.min(record.maxHeight, Number(portEvent.data.height) || 360));
          frame.style.height = `${height}px`;
          notifyIntrinsicSize(record, portEvent.data.width, height);
          rememberLayout(key, portEvent.data.layout);
          finish();
        } else if (portEvent.data.type === "failed") {
          disposeFrame(record);
          finish(new Error(portEvent.data.error || "Eraser renderer failed"));
        }
      };
      event.source.postMessage(
        {
          type: "iac-eraser-init",
          requestId,
          graph,
          theme,
          workerSource: source,
          ...runtimeSources,
          cachedLayout: layoutCache.get(key) || null,
          timeoutMs: LAYOUT_TIMEOUT_MS,
        },
        "*",
        [channel.port2],
      );
    };
    window.addEventListener("message", onReady);
    record.frame = frame;
    frame.src = `/static/eraser-frame.html?v=eraser-browser-v4#${encodeURIComponent(requestId)}`;
    record.target.replaceChildren(frame);
  });
}

async function renderActiveView(record) {
  record.generation += 1;
  const generation = record.generation;
  disposeFrame(record);
  const view = record.views[record.activeIndex] || record.views[0];
  if (!view) {
    record.target.replaceChildren();
    return;
  }
  if (preferredRenderer() === "mermaid") {
    await showMermaid(record, view, generation);
    return;
  }
  if (!usableGraph(view.graph)) {
    await showMermaid(record, view, generation, t("This historical diagram is shown with Mermaid."));
    return;
  }
  try {
    await renderEraserFrame(record, view, generation);
  } catch (error) {
    if (generation !== record.generation) return;
    console.warn("Eraser architecture render failed", error);
    await showMermaid(record, view, generation, t("This diagram is temporarily shown with Mermaid."));
  }
}

function makeTabs(record) {
  if (record.views.length < 2) return null;
  const tabs = document.createElement("div");
  tabs.className = "diagram-view-tabs";
  record.views.forEach((view, index) => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "diagram-view-tab" + (index === record.activeIndex ? " is-active" : "");
    tab.textContent = view.title || view.id || t("View {n}", { n: index + 1 });
    tab.addEventListener("click", () => {
      record.activeIndex = index;
      for (const [position, item] of [...tabs.children].entries()) {
        item.className = "diagram-view-tab" + (position === index ? " is-active" : "");
      }
      renderActiveView(record);
    });
    tabs.append(tab);
  });
  return tabs;
}

export async function renderArchitectureDiagram(container, diagram, options = {}) {
  disposeArchitectureDiagram(container);
  const rawViews = Array.isArray(diagram?.views) && diagram.views.length
    ? diagram.views
    : [{ id: "overview", title: "", mermaidSource: diagram?.mermaidSource || diagram?.mermaid_source || "", graph: diagram?.graph }];
  const normalizedViews = rawViews.map((view) => ({
    ...view,
    mermaidSource: view?.mermaidSource || view?.mermaid_source || "",
  }));
  const views = normalizedViews.filter((view) => view && (view.mermaidSource || usableGraph(view.graph)));
  const shell = document.createElement("div");
  shell.className = "architecture-diagram-shell";
  const target = document.createElement("div");
  target.className = "architecture-diagram-view";
  const record = {
    container,
    shell,
    target,
    views,
    fallbackSource: diagram?.mermaidSource || diagram?.mermaid_source || "",
    activeIndex: 0,
    generation: 0,
    frame: null,
    port: null,
    cancelFrame: null,
    onSize: typeof options.onSize === "function" ? options.onSize : null,
    maxHeight:
      Number.isFinite(options.maxHeight) && options.maxHeight >= 240 ? Math.min(2400, options.maxHeight) : 720,
  };
  const tabs = makeTabs(record);
  shell.append(...(tabs ? [tabs] : []), target);
  container.replaceChildren(shell);
  recordsByContainer.set(container, record);
  activeRecords.add(record);
  observeDisconnects();
  await renderActiveView(record);
}

function observeDisconnects() {
  if (disconnectObserver || typeof MutationObserver === "undefined" || !document.documentElement) return;
  disconnectObserver = new MutationObserver(() => {
    for (const record of [...activeRecords]) {
      if (record.container.isConnected === false) unregister(record);
    }
  });
  disconnectObserver.observe(document.documentElement, { childList: true, subtree: true });
}

function rerenderConnected() {
  for (const record of [...activeRecords]) {
    if (record.container.isConnected === false) {
      unregister(record);
      continue;
    }
    renderActiveView(record);
  }
}

async function refreshPreferredRenderer() {
  try {
    const payload = await getArchitectureDiagramRenderer();
    const renderer = payload?.renderer === "mermaid" ? "mermaid" : "eraser";
    if (preferredRenderer() === renderer) return;
    document.body.dataset.architectureDiagramRenderer = renderer;
    window.dispatchEvent(new CustomEvent(RENDERER_EVENT));
  } catch (_error) {
    /* Keep the server-injected preference when a background refresh is unavailable. */
  }
}

if (typeof window !== "undefined" && window.addEventListener) {
  window.addEventListener(RENDERER_EVENT, rerenderConnected);
  window.addEventListener(THEME_EVENT, rerenderConnected);
  window.addEventListener("focus", refreshPreferredRenderer);
}

export function notifyArchitectureRendererChanged() {
  window.dispatchEvent(new CustomEvent(RENDERER_EVENT));
}

export function notifyArchitectureThemeChanged() {
  window.dispatchEvent(new CustomEvent(THEME_EVENT));
}
