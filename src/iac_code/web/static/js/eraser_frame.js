(function () {
  "use strict";
  let requestId = "";
  let activeWorker = null;
  let rendererPromise = null;
  let port = null;

  const glyph = (body) =>
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const icons = Object.freeze({
    network: glyph('<circle cx="6" cy="12" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="m9 11 6-4m-6 6 6 4"/>'),
    compute: glyph('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h1m-1 9h1m4-9h6m-6 9h6"/>'),
    database: glyph('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>'),
    storage: glyph('<path d="M4 6h16l-2 14H6L4 6Z"/><path d="M8 6V4h8v2m-7 6h6"/>'),
    governance: glyph('<path d="M12 3 4 7v5c0 5 3.4 8 8 9 4.6-1 8-4 8-9V7l-8-4Z"/><path d="m9 12 2 2 4-5"/>'),
    generic: glyph('<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>'),
  });

  function loadRenderer() {
    if (rendererPromise) return rendererPromise;
    if (!window.createIacEraserRenderer) return Promise.reject(new Error("Eraser renderer failed to load"));
    rendererPromise = window.createIacEraserRenderer(icons);
    return rendererPromise;
  }

  function category(item) {
    const value = `${item.resourceType} ${item.product} ${item.role}`.toLowerCase();
    if (/vpc|vswitch|network|cen|loadbalancer|alb|slb|eip|gateway|dns/.test(value)) return ["network", "blue"];
    if (/ecs|compute|server|function|ack|container|kubernetes/.test(value)) return ["compute", "orange"];
    if (/rds|database|polardb|redis|cache|mongodb|kafka/.test(value)) return ["database", "purple"];
    if (/oss|storage|nas|disk|bucket/.test(value)) return ["storage", "green"];
    if (/ram|log|sls|monitor|audit|governance|security/.test(value)) return ["governance", "gray"];
    return ["generic", "gray"];
  }

  function eraserInput(layout, theme) {
    const dark = theme !== "ivory";
    const containerById = new Map(
      layout.items.filter((item) => item.kind === "container").map((item) => [item.id, item]),
    );
    const containerDepth = (item) => {
      let depth = 0;
      let current = item;
      while (current.parentId && containerById.has(current.parentId)) {
        depth += 1;
        current = containerById.get(current.parentId);
      }
      return depth;
    };
    const containers = layout.items
      .filter((item) => item.kind === "container")
      .sort((a, b) => containerDepth(a) - containerDepth(b) || a.id.localeCompare(b.id))
      .map((item) => ({
        tag: "Group",
        id: item.id,
        x: item.x,
        y: item.y,
        width: item.width,
        height: item.height,
        ...(item.parentId ? { containerId: item.parentId } : {}),
        title: { text: item.label, width: "full" },
        color: dark ? "gray" : "blue",
        styleMode: "plain",
        borderStyle: item.parentId ? "dashed" : "solid",
      }));
    const nodes = layout.items
      .filter((item) => item.kind === "node")
      .map((item) => {
        const [icon, color] = category(item);
        return {
          tag: "Shape",
          id: item.id,
          x: item.x,
          y: item.y,
          width: item.width,
          height: item.height,
          ...(item.parentId ? { containerId: item.parentId } : {}),
          icon,
          color,
          texts: [{ text: item.label }],
          styleMode: "shadow",
          cornerRadius: "round",
        };
      });
    const connections = layout.edges.map((edge) => {
      const [originX, originY] = edge.points[0];
      return {
        id: edge.id,
        from: edge.from,
        to: edge.to,
        label: edge.label,
        x: originX,
        y: originY,
        points: edge.points.map(([x, y]) => ({ x: x - originX, y: y - originY })),
        fromPort: edge.fromPort,
        toPort: edge.toPort,
        ...(edge.labelPlacement
          ? {
              labelPlacement: {
                x: edge.labelPlacement.x - originX,
                y: edge.labelPlacement.y - originY,
                width: edge.labelPlacement.width,
                height: edge.labelPlacement.height,
              },
            }
          : {}),
        connectorStyle: "straight",
        cornerStyle: "elbow",
        lineStyle: edge.style === "solid_arrow" ? "solid" : "dashed",
        endArrowhead: edge.style === "dotted_open" ? "arrow" : "triangle",
        color: dark ? "#aab6c8" : "#536a88",
      };
    });
    return { entities: [...containers, ...nodes], connections };
  }

  function runWorker(workerSource, graph, timeoutMs) {
    activeWorker?.terminate();
    const url = URL.createObjectURL(new Blob([workerSource], { type: "text/javascript" }));
    const worker = new Worker(url);
    URL.revokeObjectURL(url);
    activeWorker = worker;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        worker.terminate();
        if (activeWorker === worker) activeWorker = null;
        reject(new Error("Eraser layout timed out"));
      }, timeoutMs);
      worker.onmessage = (event) => {
        if (event.data?.requestId !== requestId) return;
        clearTimeout(timer);
        worker.terminate();
        if (activeWorker === worker) activeWorker = null;
        if (event.data.ok) resolve(event.data.layout);
        else reject(new Error(event.data.error || "Eraser layout failed"));
      };
      worker.onerror = () => {
        clearTimeout(timer);
        worker.terminate();
        if (activeWorker === worker) activeWorker = null;
        reject(new Error("Eraser layout worker failed"));
      };
      worker.postMessage({ requestId, graph });
    });
  }

  function applyThemeStyles(theme) {
    let style = document.getElementById("iac-eraser-theme");
    if (!style) {
      style = document.createElement("style");
      style.id = "iac-eraser-theme";
      document.head.append(style);
    }
    const dark = theme !== "ivory";
    style.textContent = `
      #eraser-scene .er-rel__label {
        color: ${dark ? "#e4eaf2" : "#34445b"} !important;
        background: ${dark ? "#242424" : "#ffffff"} !important;
        border-radius: 3px;
      }
    `;
  }

  async function render(payload) {
    document.documentElement.dataset.colorScheme = payload.theme === "ivory" ? "light" : "dark";
    const layout = payload.cachedLayout || await runWorker(payload.workerSource, payload.graph, payload.timeoutMs || 2500);
    const renderer = await loadRenderer();
    await renderer(eraserInput(layout, payload.theme));
    applyThemeStyles(payload.theme);
    const scene = document.getElementById("eraser-scene");
    if (!scene) throw new Error("Eraser scene was not created");
    const bounds = scene.getBoundingClientRect();
    const width = Math.max(
      360,
      Math.ceil(Math.max(Number(layout.width) || 0, scene.scrollWidth || 0, bounds.width) + 20),
    );
    const height = Math.max(240, Math.min(720, Math.ceil(bounds.height + 20)));
    port.postMessage({ type: "rendered", requestId, width, height, layoutVersion: layout.layoutVersion, layout });
  }

  window.startIacEraserFrame = (payload, nextPort, nextRequestId) => {
    if (!nextPort || port) return;
    requestId = nextRequestId;
    port = nextPort;
    port.onmessage = (portEvent) => {
      if (portEvent.data?.type === "cancel") {
        activeWorker?.terminate();
        activeWorker = null;
      }
    };
    render(payload).catch((error) => {
      port.postMessage({ type: "failed", requestId, error: error instanceof Error ? error.message : String(error) });
    });
  };
})();
