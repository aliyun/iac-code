// Endpoint details share the parent's baseInfo route. Never credit the parent
// heading or a global search; require the actual visible endpoint/address card.
export function isPolardbEndpoint(reference) {
  return ["ALIYUN::POLARDB::DBClusterEndpoint", "alicloud_polardb_endpoint",
    "ALIYUN::POLARDB::DBClusterEndpointAddress", "alicloud_polardb_endpoint_address"]
    .includes(reference?.resourceType);
}

function exact(text, value) {
  if (!value) return false;
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^A-Za-z0-9_.-])${escaped}($|[^A-Za-z0-9_.-])`).test(text);
}

export function matchPolardbEndpoint(url, reference, cards) {
  const result = { matched: false, parentRouteMatched: false, matchedCards: 0 };
  if (!isPolardbEndpoint(reference) || !reference.identifier || !reference.regionId ||
      !reference.attributes?.DBClusterId) return result;
  let actual;
  try { actual = new URL(url); } catch { return result; }
  result.parentRouteMatched = actual.protocol === "https:" &&
    ["polardb.console.aliyun.com", "polardb4service.console.aliyun.com"].includes(actual.hostname) &&
    actual.pathname === `/${encodeURIComponent(reference.regionId)}/cluster/` +
      `${encodeURIComponent(reference.attributes.DBClusterId)}/baseInfo` &&
    !actual.search && !actual.hash && !actual.port && !actual.username && !actual.password;
  if (!result.parentRouteMatched) return result;
  const address = reference.resourceType.includes("Address") || reference.resourceType.endsWith("_address");
  const endpoint = address ? reference.attributes.DBEndpointId : reference.identifier;
  if (!endpoint || !/^pe-[A-Za-z0-9]+$/.test(endpoint)) return result;
  const escapedAddress = reference.identifier.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const addressPort = text => /(?:端口|Port)\s*[:：]?\s*\d{1,5}\b/i.test(text) ||
    new RegExp(`(^|[^A-Za-z0-9_.-])${escapedAddress}:\\d{1,5}\\b`).test(text);
  const matches = cards.filter(card => card.visible && card.connectionScoped &&
    (/Endpoint\s*ID|端点\s*ID/i.test(card.text) ||
      (card.identityLabel === 'visible_default_endpoint' &&
        new RegExp(`默认地址\\s*[（(]\\s*${endpoint}\\s*[）)]`).test(card.text))) &&
    exact(card.text, endpoint) &&
    (!address || (exact(card.text, reference.identifier) && addressPort(card.text))));
  result.matchedCards = matches.length;
  result.matched = matches.length === 1;
  return result;
}

async function visibleConnectionCards(surface) {
  const cards = await surface.locator('section, [role="tooltip"], [class*="tooltip" i], .next-balloon, .ant-tooltip, .topology-balloon-wrap, ' +
    '[class*="endpoint" i], [class*="connection" i]').evaluateAll(nodes => {
    const visible = n => {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility === "visible";
    };
    const candidates = nodes.filter(n => visible(n) && /Endpoint\s*ID|端点\s*ID/i.test(n.innerText || ""));
    // Nested wrapper copies of one card are not independent identity facts.
    return candidates.filter(n => !candidates.some(other => other !== n && n.contains(other)))
      .map(n => ({ visible: true, connectionScoped: Boolean(n.matches('[role="tooltip"], [class*="tooltip" i], .next-balloon, ' +
        '.ant-tooltip, .topology-balloon-wrap, [class*="endpoint" i], [class*="connection" i]')) ||
        /数据库连接|Database\s+Connection/i.test(n.innerText || ""), text: n.innerText || "" }));
  });
  const heading = surface.getByText(/^(?:数据库连接|Database Connection)$/i);
  if (await heading.count() !== 1) return cards;
  // r17's real list renders "默认地址（pe-FULL）", not an EndpointId
  // column. Restrict to this connection section and its smallest complete
  // visible card; a parent heading/body or another card supplies no identity.
  const listScope = heading.locator('xpath=ancestor::*[self::div or self::section]' +
    '[.//*[contains(text(), "默认地址")]][1]');
  if (await listScope.count() === 1) {
    cards.push(...await listScope.locator('div, section').evaluateAll(collectDefaultEndpointCards));
  }
  const scope = heading.locator('xpath=ancestor::*[self::div or self::section][.//table][1]');
  if (await scope.count() !== 1) return cards;
  const rows = await scope.locator('table').evaluateAll(collectConnectionTableCards);
  return [...cards, ...rows];
}

export function collectDefaultEndpointCards(nodes) {
  const visible = n => {
    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
      s.visibility === 'visible' && Number(s.opacity || 1) !== 0;
  };
  const candidates = nodes.slice(0, 4096).filter(n => {
    if (!visible(n)) return false;
    const text = n.innerText || '';
    // Parent innerText may include transparent descendants. Never credit a
    // full identity that is present only in an invisible child of the card.
    if ([...n.querySelectorAll('*')].some(child => !visible(child) &&
        /pe-[A-Za-z0-9]+|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+:\d{1,5}/.test(child.innerText || ''))) return false;
    const ids = text.match(/默认地址\s*[（(]\s*pe-[A-Za-z0-9]+\s*[）)]/g) || [];
    const addresses = text.match(/\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+:\d{1,5}\b/g) || [];
    return ids.length === 1 && addresses.length === 1 &&
      !/主地址|数据库连接|Database\s+Connection/i.test(text) &&
      (text.match(/\bpe-[A-Za-z0-9]+\b/g) || []).length === 1;
  });
  return candidates.filter(n => !candidates.some(other => other !== n && n.contains(other)))
    .slice(0, 50).map(n => ({ visible: true, connectionScoped: true,
      identityLabel: 'visible_default_endpoint', text: n.innerText }));
}

// List view uses DOM cells, unlike the canvas. A labelled EndpointId column
// and its complete value must belong to one visible row; never join identities
// across rows or read hidden data attributes/framework state.
export function collectConnectionTableCards(tables) {
  const visible = n => {
    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility === 'visible';
  };
  const cards = [];
  for (const table of tables.slice(0, 4)) {
    if (!visible(table)) continue;
    const headers = [...table.querySelectorAll('thead th')].map(n => (n.innerText || '').trim());
    if (!headers.some(h => /^(?:Endpoint\s*ID|端点\s*ID)$/i.test(h))) continue;
    for (const row of [...table.querySelectorAll('tbody tr')].slice(0, 50)) {
      if (!visible(row)) continue;
      const cells = [...row.querySelectorAll('td')];
      if (cells.length !== headers.length || !cells.every(visible)) continue;
      cards.push({ visible: true, connectionScoped: true,
        text: cells.map((cell, index) => `${headers[index]}: ${(cell.innerText || '').trim()}`).join('\n') });
    }
  }
  return cards;
}

async function visibleTooltipTexts(surface) {
  return surface.locator('[role="tooltip"], [class*="tooltip" i], .next-balloon, .ant-tooltip, .topology-balloon-wrap')
    .evaluateAll(nodes => nodes.filter(n => {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
    }).map(n => ({ text: (n.innerText || n.textContent || "").slice(0, 2000) })).slice(-8));
}

// r13 proves that the real canvas hover uses this custom panel, not a tooltip
// role. Preserve it directly even if it has no EndpointId (no identity credit).
// Also expose the rendered view-control semantics for a later, evidence-based
// read-only list-view path. Do not guess an icon selector or click any control.
export function collectConnectionViewControls(nodes) {
  return { diagnosticOnly: true, controls: nodes.slice(0, 64).filter(n => {
    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    const view = n.ownerDocument.defaultView;
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
      r.top < view.innerHeight && r.left < view.innerWidth &&
      s.display !== "none" && s.visibility === "visible" && Number(s.opacity) !== 0;
  }).slice(0, 16).map(n => ({ tag: n.tagName, class: n.getAttribute("class"),
    role: n.getAttribute("role"), title: n.getAttribute("title"), ariaLabel: n.getAttribute("aria-label"),
    text: (n.innerText || n.textContent || "").slice(0, 300),
    structure: (n.outerHTML || "").slice(0, 1800) })) };
}

// A read-only presentation switch is allowed only after the rendered control
// itself (or a fresh hover label) identifies it as list view. r15 also captured
// the exact live presentation-control key/feature and parent binding. That
// allowlisted operation may select list view, but is never resource identity.
// Icon position or CSS class alone must not select a blank button.
export function isObservedConnectionListControl(metadata, parent) {
  if (!parent || !metadata?.autolog) return false;
  const values = new URLSearchParams(metadata.autolog);
  if ([...values.keys()].length !== 3 ||
      values.get('key') !== 'polardb_clusterDetailList' ||
      values.get('feature') !== 'clusterDetailList' || values.get('c1') !== parent) return false;
  const labels = [metadata.title, metadata.ariaLabel, metadata.text].filter(x => x?.trim());
  return labels.every(x => /^(?:列表|列表视图|列表模式|List View|List Mode)$/i.test(x.trim()));
}

export function isConnectionListViewControl(label, rect, headingRect) {
  return /^(?:列表|列表视图|列表模式|List View|List Mode)$/i.test((label || "").trim()) &&
    Boolean(rect && headingRect && rect.width > 0 && rect.height > 0 &&
      rect.width <= 100 && rect.height <= 60 && rect.x > headingRect.x + headingRect.width &&
      Math.abs(rect.y + rect.height / 2 - headingRect.y - headingRect.height / 2) <= 28);
}

export async function switchConnectionListView(surface, url, reference, deadline) {
  const state = { attempted: false, switched: false, readOnly: true, controls: [] };
  if (!surface || !matchPolardbEndpoint(url, reference, []).parentRouteMatched ||
      !Number.isFinite(deadline) || deadline <= Date.now()) return state;
  const heading = surface.getByText(/^(?:数据库连接|Database Connection)$/i);
  if (await heading.count() !== 1) return state;
  const scope = heading.locator('xpath=ancestor::*[self::div or self::section][.//canvas][1]');
  if (await scope.count() !== 1) return state;
  const headingRect = await heading.boundingBox();
  const buttons = scope.locator('button');
  const count = await buttons.count();
  // Four buttons are present in r14's real graph; node-list controls below the
  // connection header are excluded by geometry, irrespective of their labels.
  if (count > 8) return state;
  const cap = Math.min(deadline, Date.now() + 6000);
  const candidates = [];
  for (let index = 0; index < count && Date.now() < cap; index++) {
    const button = buttons.nth(index);
    if (!await button.isVisible()) continue;
    const rect = await button.boundingBox();
    if (!isConnectionListViewControl('列表视图', rect, headingRect)) continue;
    state.attempted = true;
    const metadata = await button.evaluate(n => ({ title: n.getAttribute('title'),
      ariaLabel: n.getAttribute('aria-label'), text: n.innerText || '',
      autolog: n.getAttribute('data-autolog'),
      structure: n.outerHTML.slice(0, 1800) }));
    const labels = [metadata.title, metadata.ariaLabel, metadata.text];
    const baseline = new Set((await visibleTooltipTexts(surface)).map(t => t.text));
    if (!labels.some(label => isConnectionListViewControl(label, rect, headingRect))) {
      await button.hover({ timeout: Math.max(1, Math.min(1000, cap - Date.now())) });
      await surface.waitForTimeout(250);
      labels.push(...(await visibleTooltipTexts(surface)).filter(t => !baseline.has(t.text)).map(t => t.text));
    }
    const semanticLabel = labels.find(label => isConnectionListViewControl(label, rect, headingRect)) ||
      (isObservedConnectionListControl(metadata, reference.attributes.DBClusterId)
        ? 'observed_connection_list_control' : null);
    state.controls.push({ index, rect, metadata, labels, semanticLabel: semanticLabel || null });
    if (!semanticLabel) continue;
    candidates.push(button);
  }
  // Ambiguous controls fail closed. A control attribute proves only the
  // presentation operation, never the complete endpoint identity in its row.
  if (candidates.length === 1 && Date.now() < cap) {
    await candidates[0].click({ timeout: Math.max(1, Math.min(1000, cap - Date.now())) });
    state.switched = true;
    await surface.waitForTimeout(350);
  }
  return state;
}

// Diagnostic-only DOM projection. r12's real network/configuration hover panel
// was visible in the PNG/body text but was outside our tooltip class selectors.
// Discover rendered text and its small ancestor chain, not framework state,
// hidden attributes, arbitrary page text or inferred endpoint identity.
export function collectConnectionHoverEvidence(nodes) {
  const marker = /网络信息|Network\s+Information|Endpoint\s*ID|端点\s*ID|\bpe-[A-Za-z0-9]{8,}\b|[A-Za-z0-9.-]+\.(?:polardb\.rds|rwlb\.rds)\.aliyuncs\.com/i;
  const visible = n => {
    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    const view = n.ownerDocument.defaultView;
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
      r.top < view.innerHeight && r.left < view.innerWidth &&
      s.display !== "none" && s.visibility === "visible" && Number(s.opacity) !== 0;
  };
  const scanned = nodes.slice(0, 4096);
  const anchors = scanned.filter(n => {
    const text = n.innerText || "";
    return text.length <= 3000 && marker.test(text) && visible(n);
  });
  const leaves = anchors.filter(n => !anchors.some(other => other !== n && n.contains(other)));
  const seen = new Set(), samples = [];
  for (const anchor of leaves.slice(0, 8)) {
    let node = anchor;
    for (let depth = 0; node && depth < 4 && samples.length < 8; depth++, node = node.parentElement) {
      if (["BODY", "HTML"].includes(node.tagName) || seen.has(node) || !visible(node)) continue;
      seen.add(node);
      const text = node.innerText || "";
      if (!text || text.length > 3000) continue;
      const r = node.getBoundingClientRect();
      samples.push({ tag: node.tagName, class: node.getAttribute("class"), role: node.getAttribute("role"),
        depth, rect: { x: r.x, y: r.y, width: r.width, height: r.height }, text });
    }
  }
  return { diagnosticOnly: true, scanned: scanned.length, scanTruncated: nodes.length > scanned.length,
    anchors: leaves.length, samples };
}

async function visibleConnectionHoverEvidence(surface) {
  return surface.locator("body *").evaluateAll(collectConnectionHoverEvidence);
}

export function connectionCanvasProbeHit(node, position) {
  const r = node.getBoundingClientRect();
  const hit = node.ownerDocument.elementFromPoint(r.left + position.x, r.top + position.y);
  return { diagnosticOnly: true, canvasHit: Boolean(hit && (hit === node || node.contains(hit))),
    tag: hit?.tagName || null, class: hit?.getAttribute("class") || null,
    text: (hit?.innerText || "").slice(0, 1000) };
}

export function connectionCanvasHoverPositions(box) {
  // Positions are probes, NEVER identity facts. The observed connection graph
  // has two endpoint cards in its first row. Only hover their value area;
  // no click, canvas data extraction, clipboard read or graph state access.
  if (!box || !Number.isFinite(box.width) || !Number.isFinite(box.height) ||
      box.width < 300 || box.height < 200) return [];
  return [0.20, 0.55].flatMap(x => [0.15, 0.23, 0.30, 0.36].map(y =>
    ({ x: Math.round(box.width * x), y: Math.round(box.height * y) })));
}

export async function hoverPolardbConnectionCanvas(surface, url, reference, deadline,
    readCards = visibleConnectionCards, readTooltips = visibleTooltipTexts,
    readHoverEvidence = visibleConnectionHoverEvidence) {
  const state = { matched: false, attempted: false, count: 0, probes: [], cards: [], readOnly: true };
  if (!surface || !matchPolardbEndpoint(url, reference, []).parentRouteMatched ||
      !Number.isFinite(deadline) || deadline <= Date.now()) return state;
  // Anchor to the *documented connection heading*, not all page canvases.
  // A nodes/storage graph or an ambiguous section must never be probed.
  const heading = surface.getByText(/^(?:数据库连接|Database Connection)$/i);
  if (await heading.count() !== 1) return state;
  const scope = heading.locator('xpath=ancestor::*[self::div or self::section][.//canvas][1]');
  if (await scope.count() !== 1) return state;
  const canvas = scope.locator('canvas');
  state.count = await canvas.count();
  if (state.count !== 1 || !await canvas.isVisible()) return state;
  state.viewControls = await scope.locator('button, [role="button"], [title], [aria-label], i, svg')
    .evaluateAll(collectConnectionViewControls);
  const cap = Math.min(deadline, Date.now() + 12000);
  await canvas.scrollIntoViewIfNeeded({ timeout: Math.max(1, Math.min(2000, cap - Date.now())) });
  const box = await canvas.boundingBox();
  const positions = connectionCanvasHoverPositions(box);
  state.geometry = box ? { width: box.width, height: box.height } : null;
  // Stale tooltips outside this new hover cannot supply child identity.
  const baseline = new Set((await readCards(surface)).map(card => JSON.stringify(card)));
  state.hoverBaseline = await readHoverEvidence(surface);
  for (const position of positions) {
    if (Date.now() >= cap) break;
    state.attempted = true;
    await canvas.hover({ position, timeout: Math.max(1, Math.min(1500, cap - Date.now())) });
    await surface.waitForTimeout(Math.max(0, Math.min(350, cap - Date.now())));
    const cards = (await readCards(surface)).filter(card => !baseline.has(JSON.stringify(card)));
    // Preserve even incomplete/unlabelled tooltips for independent diagnosis;
    // they still cannot enter the precise card matcher as identity evidence.
    state.probes.push({ position, cards, tooltips: await readTooltips(surface),
      hoverEvidence: await readHoverEvidence(surface),
      pointerHit: typeof canvas.evaluate === "function" ?
        await canvas.evaluate(connectionCanvasProbeHit, position) : null });
    state.cards = cards;
    Object.assign(state, matchPolardbEndpoint(url, reference, cards));
    if (state.matched) break;
  }
  return state;
}

export async function polardbEndpointLanding(surface, url, reference, deadline = Date.now()) {
  const state = { matched: false, cards: [], hoverTargets: [], section: { count: 0, scrolled: false },
    connectionContent: { ready: false },
    labelCandidates: [], readOnly: true, searchFallbackSuppressed: true };
  if (!surface || !isPolardbEndpoint(reference)) return state;
  // Fail closed before interacting with an unrelated or non-parent console.
  if (!matchPolardbEndpoint(url, reference, []).parentRouteMatched) return state;
  try {
    state.cards = await visibleConnectionCards(surface);
    Object.assign(state, matchPolardbEndpoint(url, reference, state.cards));
    if (state.matched) return state;
    // Loaded basic information is not loaded connection information. Wait for
    // the documented section and scroll it into view (also activates lazy UI).
    // Neither an absent section nor a heading establishes child identity.
    if (Date.now() < deadline) {
      const section = surface.getByText(/^(?:数据库连接|Database Connection)$/i);
      try {
        await section.waitFor({ state: "attached", timeout: Math.max(1, Math.min(12000, deadline - Date.now())) });
      } catch (error) { state.section.waitError = String(error.message || error).slice(0, 200); }
      state.section.count = await section.count();
      if (state.section.count === 1) {
        await section.scrollIntoViewIfNeeded({ timeout: Math.max(1, Math.min(2000, deadline - Date.now())) });
        state.section.scrolled = true;
      }
    }
    // The heading exists before the connection graph renders. The actual
    // primary-address label, not that heading, is the bounded readiness gate.
    // r10's screenshot contained the graph after the earlier DOM capture.
    if (Date.now() < deadline) {
      try {
        await surface.getByText(/^(?:主地址|Primary Endpoint)$/i).waitFor({
          state: "visible", timeout: Math.max(1, Math.min(8000, deadline - Date.now())),
        });
        state.connectionContent.ready = true;
      } catch (error) { state.connectionContent.waitError = String(error.message || error).slice(0, 200); }
      state.cards = await visibleConnectionCards(surface);
      Object.assign(state, matchPolardbEndpoint(url, reference, state.cards));
    }
    // The documented cluster label includes a recommendation suffix. Hover is
    // read-only; never click edit/apply/create, navigate or mutate the DOM.
    const labels = ["主地址", "集群地址", "Primary Endpoint", "Cluster Endpoint",
      /^集群地址\s*(?:〖推荐〗|【推荐】|\(推荐\)|（推荐）)$/];
    if (reference.resourceType.includes("Address") || reference.resourceType.endsWith("_address")) {
      // Exact address from the live product DOM; no navigation to its value.
      labels.unshift(reference.identifier);
    }
    for (const label of labels) {
      if (Date.now() >= deadline) break;
      const target = surface.getByText(label, { exact: true });
      const count = await target.count();
      state.labelCandidates.push({ label: String(label), count });
      if (count !== 1 || !await target.isVisible()) continue;
      await target.scrollIntoViewIfNeeded({ timeout: Math.max(1, Math.min(2000, deadline - Date.now())) });
      await target.hover({ timeout: Math.max(1, Math.min(2000, deadline - Date.now())) });
      state.hoverTargets.push(String(label));
      await surface.waitForTimeout(300);
      state.cards = await visibleConnectionCards(surface);
      Object.assign(state, matchPolardbEndpoint(url, reference, state.cards));
      if (state.matched) break;
    }
    if (!state.matched && state.section.count === 1 && Date.now() < deadline) {
      state.listView = await switchConnectionListView(surface, url, reference, deadline);
      state.cards = await visibleConnectionCards(surface);
      Object.assign(state, matchPolardbEndpoint(url, reference, state.cards));
    }
    if (!state.matched && state.section.count === 1 && Date.now() < deadline) {
      state.canvas = await hoverPolardbConnectionCanvas(surface, url, reference, deadline);
      if (state.canvas.matched) {
        state.cards = state.canvas.cards;
        Object.assign(state, matchPolardbEndpoint(url, reference, state.cards));
      }
    }
  } catch (error) {
    state.matched = false;
    state.readError = String(error.message || error).slice(0, 200);
  }
  // Preserve the actual lower-page/connection text for independent diagnosis.
  // It is evidence only: never use arbitrary body text as the child verdict.
  try { state.bodyTail = (await surface.locator("body").innerText({ timeout: 1000 })).slice(-12000); }
  catch (error) { state.bodyReadError = String(error.message || error).slice(0, 200); }
  // Read rendered graph structure for diagnosis, never as an identity verdict.
  // SVG text is absent from innerText; canvas has no inspectable child text.
  try {
    state.graphEvidence = await surface.locator('svg, canvas').evaluateAll(nodes => nodes.map(n => {
      const r = n.getBoundingClientRect(), s = getComputedStyle(n);
      return { tag: n.tagName, class: n.getAttribute("class"),
        visible: r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden",
        text: (n.textContent || "").slice(0, 6000),
        structure: n.outerHTML.slice(0, 12000) };
    }).filter(n => n.visible).slice(-8));
  } catch (error) { state.graphReadError = String(error.message || error).slice(0, 200); }
  return state;
}
