import { scanResourceOccurrencesWork, resourceContextFromObject } from "./resource_occurrences.js?v=resource-links-v114";
import { resolveResourceOccurrence, resolveResourceChoice, resourceSpec, RESOURCE_LINK_RULES } from "./resource_link_rules.js?v=resource-links-v82";
import { canonicalSourceJson, digestResourceSource, resourceOccurrenceGroup, resourceBindingSourceKey } from "./resource_sources.js?v=resource-bindings-v52";

const ROOTS = new WeakMap();
const CONTEXTS = new WeakMap();
let activeController = null;
const EXCLUDED = new Set(["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "OPTION", "SCRIPT", "STYLE", "SVG", "MATH"]);
const BLOCKS = new Set(["P", "DIV", "PRE", "LI", "TD", "TH", "DD", "DT", "SUMMARY", "SECTION", "H1", "H2", "H3", "H4", "BR"]);
const TABLE_REGION_HEADERS = new Set([
  "region", "regionid", "resource region", "resource region id",
  "地域", "地域id", "地域regionid", "资源地域", "资源地域id",
  "区域", "区域id", "区域regionid", "资源区域", "资源区域id",
]);
const TABLE_FIELD_HEADERS = new Set(["field", "property", "字段", "属性"]);
const TABLE_VALUE_HEADERS = new Set(["value", "值"]);
const own = (node) => node?.nodeType === 1 && node.hasAttribute("data-resource-link");

function nearestProductScope(text, start) {
  const prefix = String(text || "").slice(0, Math.max(0, start));
  // API result headings are rendered in several equivalent forms, e.g.
  // “产品: VPC（vpc） / Action: Describe…”, “产品: vpc | Action: …”.
  // Keep the product scope attached to generic fields so a same-spelled
  // Name/Id from another product cannot steal the link.
  const labels = [
    ...prefix.matchAll(/(?:产品|product)\s*[:：]\s*([a-z][a-z0-9_-]*)\s*[|｜]\s*(?:Action|动作)\s*[:：]/giu),
    ...prefix.matchAll(/(?:产品|product)\s*[:：]\s*[^\n|｜]*?[（(]\s*([a-z][a-z0-9_-]*)\s*[）)]/giu),
  ];
  labels.sort((a, b) => a.index - b.index);
  if (labels.length) return labels.at(-1)[1].toUpperCase();
  const batchHeadings = [...prefix.matchAll(
    /(?:^|\n)\s*(?:\d+[.)]\s*)?[^\n]{0,160}\b([A-Z][A-Z0-9]{1,15})\b[^\n]*(?:—|–|-)\s*[A-Za-z][A-Za-z0-9]*\s*\([^\n]*\)/gu,
  )];
  return batchHeadings.length ? batchHeadings.at(-1)[1].toUpperCase() : null;
}
// turnId narrows facts while rendering one live turn. Durable bindings address
// the exact visible message/tool source and must not send that transient hint.
const bindingSource = (source = {}) => {
  const {turnId: _turnId, ...descriptor} = source;
  return descriptor;
};

export function setResourceTextContext(root, context) {
  if (root && typeof root === "object") CONTEXTS.set(root, context);
}

export function registerResourceText(root, options = {}) {
  if (!root || typeof root !== "object") return root;
  ROOTS.set(root, options);
  activeController?.register(root, options);
  return root;
}

export function configureResourceLinks(options) {
  activeController?.dispose();
  activeController = createResourceLinkController(options);
  return activeController;
}

export function removeResourceLinkDecorations(root) {
  for (const _ of removeDecorationWork(root)) { /* Synchronous small-container API. */ }
  // Do not normalize() the whole container: that would invalidate user ranges
  // and nodes owned by a syntax highlighter. Adjacent text nodes are supported.
}

function* removeDecorationWork(root) {
  let count = 0;
  for (const wrapper of root.querySelectorAll("[data-resource-link]")) {
    // Registered descendants are independent source owners. The traversal
    // already skips them; cleanup must obey the same boundary, including when
    // a child happened to finish decorating before its new parent is queued.
    let nestedOwner = false;
    for (let ancestor = wrapper.parentElement; ancestor && ancestor !== root; ancestor = ancestor.parentElement) {
      if (ROOTS.has(ancestor)) { nestedOwner = true; break; }
    }
    if (!nestedOwner) wrapper.replaceWith(...wrapper.childNodes);
    if (++count % 128 === 0) yield;
  }
}

function hasSelection(root, selection = root.ownerDocument?.getSelection?.()) {
  return Boolean(selection && !selection.isCollapsed &&
    (root.contains(selection.anchorNode) || root.contains(selection.focusNode)));
}

const normalizedTableHeader = (value) => String(value || "").normalize("NFKC").trim().toLowerCase()
  .replace(/[\s_()（）/·:：-]+/g, " ").trim();

function strictTableRegion(value) {
  const normalized = String(value || "").normalize("NFKC").trim();
  const match = /^(?:(?:home\s*region|region\s*id|regionid|region|地域|区域)\s*[=:]\s*)?([a-z]{2,}(?:-[a-z0-9]+)+)\s*(?:\([^()]*\))?$/i.exec(normalized);
  return match?.[1];
}

export function resourceTableContextFromObject(object, base = {}) {
  if (!object || typeof object !== "object" || Array.isArray(object)) return base;
  // A displayed region cell may include a label or explanatory suffix. Do not
  // feed the whole presentation string into the resource context as RegionId.
  const withoutRegionFields = Object.fromEntries(Object.entries(object)
    .filter(([key]) => !["RegionId", "region_id", "regionId"].includes(key)));
  let context = resourceContextFromObject(withoutRegionFields, base);
  for (const [key, value] of Object.entries(object)) {
    const header = normalizedTableHeader(key);
    const compact = header.replaceAll(" ", "");
    if (!TABLE_REGION_HEADERS.has(header) && !TABLE_REGION_HEADERS.has(compact)) continue;
    const regionId = strictTableRegion(value);
    if (regionId) context = resourceContextFromObject({RegionId:regionId}, context);
    break;
  }
  return context;
}

export function resourceTableRecordFromRows(headers, rows) {
  if (!Array.isArray(headers) || headers.length !== 2 || !Array.isArray(rows)) return null;
  const normalized = headers.map(normalizedTableHeader);
  if (!TABLE_FIELD_HEADERS.has(normalized[0]) || !TABLE_VALUE_HEADERS.has(normalized[1])) return null;
  const entries = rows.filter((row) => Array.isArray(row) && row.length === 2)
    .map(([field, value]) => [String(field || "").trim(), String(value || "").trim()])
    .filter(([field]) => field);
  return entries.length ? Object.fromEntries(entries) : null;
}

function tableRecordRegionContext(object, base) {
  return resourceTableContextFromObject(object, base);
}

function tableContext(node, base, tables) {
  const row = node.parentElement?.closest("tr");
  if (!row) return base;
  const table = row.closest("table");
  let data = tables.get(table);
  if (!data) {
    const headers = [...(table?.querySelectorAll("thead th") || [])].map((cell) => cell.textContent.trim());
    const fieldRecord = headers.length === 2 && TABLE_FIELD_HEADERS.has(normalizedTableHeader(headers[0])) &&
      TABLE_VALUE_HEADERS.has(normalizedTableHeader(headers[1]));
    const record = fieldRecord ? resourceTableRecordFromRows(headers,
      [...table.querySelectorAll("tbody tr")].map(item => [...item.children].map(cell => cell.textContent.trim()))) : null;
    data = {headers, record, contexts: new Map(),
      structuralScope: record ? JSON.stringify(["table-record", record]) : null};
    if (table) tables.set(table, data);
  }
  const {headers, record:recordObject} = data;
  const cells = [...row.children];
  const object = recordObject || Object.fromEntries(headers.map((header, i) => [header, cells[i]?.textContent.trim()]));
  let context;
  if (recordObject) {
    const key = JSON.stringify(base);
    if (!data.contexts.has(key)) data.contexts.set(key, tableRecordRegionContext(object, base));
    const cached = data.contexts.get(key);
    context = {...cached, attributes: {...cached.attributes}};
  } else context = resourceTableContextFromObject(object, base);
  const cell = node.parentElement?.closest("td");
  const explicit = CONTEXTS.get(cell);
  context.structuralScope = explicit?.structuralScope || data.structuralScope || JSON.stringify(["table-row", object]);
  if (explicit?.field) context.field = explicit.field;
  else if (recordObject && cells.length === 2) context.field = cells[0]?.textContent.trim();
  else if (cell && headers[cells.indexOf(cell)]) context.field = headers[cells.indexOf(cell)];
  return context;
}

function nodeContext(node, root, base, tables) {
  const ancestors = [];
  for (let element = node.parentElement; element; element = element.parentElement) {
    ancestors.unshift(element);
    if (element === root) break;
  }
  let result = {...base};
  for (const element of ancestors) {
    const context = CONTEXTS.get(element);
    if (context) result = {...result, ...context, attributes: {...result.attributes, ...context.attributes}};
  }
  return tableContext(node, result, tables);
}

function* releaseObservedResourceAutolinks(root, options, index, rules, track, resolveMatch, tables) {
  let count = 0;
  for (const anchor of root.querySelectorAll('a[data-resource-auto-link="true"]')) {
    let nestedOwner = false;
    for (let ancestor = anchor.parentElement; ancestor && ancestor !== root; ancestor = ancestor.parentElement) {
      if (ROOTS.has(ancestor)) { nestedOwner = true; break; }
    }
    if (nestedOwner || anchor.childElementCount || anchor.childNodes.length !== 1 || anchor.firstChild?.nodeType !== 3) {
      continue;
    }
    const value = anchor.textContent || "";
    const context = {...nodeContext(anchor.firstChild, root, options.context || {}, tables), ...options.source};
    const occurrences = yield* scanResourceOccurrencesWork(value, {
      index, context, complete: true, trackCandidate: track,
    });
    for (const occurrence of occurrences) track(occurrence);
    const exact = occurrences.find((item) => item.start === 0 && item.end === value.length);
    if (exact && ["resolved", "needs_context", "ambiguous"].includes(resolveMatch(exact).status)) {
      anchor.replaceWith(...anchor.childNodes);
    }
    if (++count % 32 === 0) yield;
  }
}

function* logicalSegments(root, base, tables) {
  let parts = [], text = "", context = null;
  let contextKey = null;
  let textOffset = 0;
  let visited = 0;
  const stack = [{node: root, exit: false}];
  while (stack.length) {
    if (++visited % 128 === 0) yield null;
    const {node, exit} = stack.pop();
    if (!exit && node !== root && node.nextSibling) stack.push({node:node.nextSibling,exit:false});
    if (node.nodeType === 1) {
      const generated = own(node);
      if (!generated && (EXCLUDED.has(node.tagName) || node.hasAttribute("contenteditable") || node.hasAttribute("data-resource-ignore"))) {
        if (parts.length) { yield {parts, text, context, start:textOffset - text.length}; parts = []; text = ""; }
        textOffset += node.textContent.length;
        continue;
      }
      if (node !== root && ROOTS.has(node)) {
        if (parts.length) { yield {parts, text, context, start:textOffset - text.length}; parts = []; text = ""; }
        textOffset += node.textContent.length;
        continue;
      }
      if (node !== root && BLOCKS.has(node.tagName) && parts.length) {
        yield {parts, text, context, start:textOffset - text.length}; parts = []; text = "";
      }
      if (!exit) {
        stack.push({node, exit: true});
        // Enqueue one sibling at a time. A previously decorated megabyte PRE
        // can contain tens of thousands of children; pushing them all at once
        // would bypass the cooperative traversal budget.
        if (node.firstChild) stack.push({node:node.firstChild,exit:false});
      }
    } else if (node.nodeType === 3 && node.data) {
      const nextContext = nodeContext(node, root, base, tables);
      // A field/value table shares one potentially long, exact scope string.
      // Compare it directly instead of copying it into every serialized key.
      const {structuralScope, ...fields} = nextContext;
      const key = JSON.stringify(fields);
      if (parts.length && (key !== contextKey || structuralScope !== context.structuralScope)) {
        yield {parts, text, context, start:textOffset - text.length}; parts = []; text = "";
      }
      context = nextContext;
      contextKey = key;
      textOffset += node.data.length;
      // Repeatedly splitting a megabyte text node copies/invalidates an ever
      // shrinking megabyte prefix. Bound physical nodes before matching while
      // keeping one logical segment (including cross-node names/highlighting).
      if (node.data.length > 8192) {
        const original = node.data;
        const fragment = node.ownerDocument.createDocumentFragment();
        for (let offset = 0; offset < original.length;) {
          let end = Math.min(offset + 8192, original.length);
          if (end < original.length && /[\uD800-\uDBFF]/.test(original[end - 1])) end -= 1;
          const chunk = node.ownerDocument.createTextNode(original.slice(offset, end));
          fragment.append(chunk);
          parts.push({node: chunk, start: text.length + offset, end: text.length + end});
          offset = end;
        }
        node.replaceWith(fragment);
        text += original;
      } else {
        parts.push({node, start: text.length, end: text.length + node.data.length});
        text += node.data;
      }
    }
  }
  if (parts.length) yield {parts, text, context, start:textOffset - text.length};
}

function* nextSegment(segments) {
  let step = segments.next();
  // Markdown renderers append whitespace outside their final block. It is not
  // another completed source segment and must not finalize a partial locator.
  while (!step.done && (!step.value || !step.value.text.trim())) {yield; step = segments.next();}
  return step;
}

export function resolveResourceOccurrenceAlternatives(occurrence, index, rules = RESOURCE_LINK_RULES) {
  const alternatives = [occurrence, ...(occurrence.alternatives || [])];
  let rowValues = [];
  try {
    const scope = JSON.parse(occurrence.structuralScope || "null");
    if (Array.isArray(scope) && ["table-row", "table-record"].includes(scope[0]) && scope[1] &&
        typeof scope[1] === "object" && !Array.isArray(scope[1])) {
      rowValues = Object.values(scope[1]).filter((value) => typeof value === "string").flatMap((value) => {
        const exact = value.trim();
        const assignment = /^[A-Za-z][A-Za-z0-9_.-]*\s*=\s*([A-Za-z0-9_:@./%+*-]+)\s*$/.exec(exact);
        return assignment ? [exact, assignment[1]] : [exact];
      });
    }
  } catch (_error) { /* Opaque non-table scopes carry no cross-cell evidence. */ }
  const results = alternatives.map((item) => {
    let result = resolveResourceOccurrence(item, index, rules);
    if (result.status !== "ambiguous" || !result.candidates?.length) return result;
    // The target locator may have historical observations under multiple
    // accounts. A different real API fact in this visible turn can prove the
    // current account/partition/region without making configured credentials
    // a silent global default. Same-turn multi-account output stays ambiguous.
    const scopeKinds = new Set(result.candidates.map((reference) => reference.scopeKind));
    if (scopeKinds.size === 1) {
      const regional = scopeKinds.has("regional");
      const context = index.observedContextFor?.(item, result.candidates[0]) || {};
      const keys = ["accountScope", "consolePartition", ...(regional ? ["regionId"] : [])];
      if (keys.every((key) => typeof context[key] === "string" && context[key])) {
        const scoped = result.candidates.filter((reference) => keys.every((key) => reference[key] === context[key]));
        if (scoped.length === 1) return resolveResourceChoice(item, scoped[0], index, rules) || result;
        if (scoped.length) result = {...result, candidates: scoped};
      }
    }
    if (!rowValues.length) return result;
    // A repeated display name can identify several resources of the same type
    // and region.  An exact primary ID (or another exact alias) in this table
    // row identifies the intended API object without asking the user to choose.
    // Keep ambiguity when the same locator exists in multiple accounts.
    const candidates = result.candidates.filter((reference) =>
      [reference, ...(reference.aliases || [])].some((locator) =>
        locator.identifier !== occurrence.identifier && rowValues.includes(locator.identifier)));
    if (candidates.length !== 1) return result;
    return resolveResourceChoice(item, candidates[0], index, rules) || result;
  });
  // A ResourceType in the same structured object/table row is stronger than
  // other resource facts that happen to reuse the same physical ID. This is
  // how attachment/enable helper resources avoid an unnecessary chooser while
  // an untyped shared field remains explicitly ambiguous.
  if (occurrence.semantic?.typed && results[0]?.status === "resolved") return results[0];
  // A structured tool scalar has an exact API field path. Prefer the one
  // primary identity actually observed at that path over another type's alias
  // of the same value (e.g. SAE NamespaceName vs a Terraform name alias).
  // Same URLs alone prove nothing; multiple primary identities/accounts remain
  // ambiguous, as do prose, sibling rows and facts from another tool call.
  const primary = alternatives.flatMap((item, position) => {
    const field = item.field;
    const scope = item.structuralScope;
    if (!item.toolUseId || typeof field !== "string" || typeof scope !== "string" ||
        !scope.startsWith("$/") || !scope.endsWith("/" + field.replaceAll("~", "~0").replaceAll("/", "~1"))) {
      return [];
    }
    const result = results[position];
    const candidates = result.candidates?.length ? result.candidates : result.reference ? [result.reference] : [];
    const owned = candidates.some((reference) =>
      reference.evidence === "api_response" && reference.identifier === item.identifier &&
      reference.identifierKind === item.identifierKind && reference.identifierField === field &&
      resourceSpec(reference.resourceType)?.identifierField === field &&
      reference.sources?.some((source) => source.toolUseId === item.toolUseId &&
        source.evidence === "api_response" && source.sourcePath === "/result" + scope.slice(1)));
    return owned ? [result] : [];
  });
  if (primary.length === 1 && primary[0].status === "resolved") return primary[0];
  const supported = results.filter((item) => !["unsupported", "not_resource"].includes(item.status));
  if (!supported.length) return results[0];
  if (supported.length === 1) return supported[0];
  // When a table row contains both a human-readable name and the primary ID
  // of that same API object, the second exact locator identifies the intended
  // resource more strongly than another same-turn object with the same name.
  // Only exact cell values count; substrings and values from sibling rows do
  // not participate.
  if (rowValues.length) {
    const structurallyOwned = supported.filter((item) => {
      const candidates = item.candidates?.length ? item.candidates : item.reference ? [item.reference] : [];
      return candidates.some((reference) => [reference, ...(reference.aliases || [])].some((locator) =>
        locator.identifier !== occurrence.identifier && rowValues.includes(locator.identifier)));
    });
    if (structurallyOwned.length === 1 && structurallyOwned[0].status === "resolved") return structurallyOwned[0];
  }
  // A locator can have an older observation under a type that was later
  // corrected (or can legitimately be reused by several products). When the
  // visible turn owns one or more compatible facts, those facts are stronger
  // than same-locator history from another turn. Keep ambiguity when this
  // turn itself observed multiple identities; never choose merely by age.
  const owner = occurrence.turnId ? ["turnId", occurrence.turnId]
    : occurrence.messageId ? ["messageId", occurrence.messageId]
    : occurrence.toolUseId ? ["toolUseId", occurrence.toolUseId] : null;
  const owned = owner && supported.filter((item) => {
    const candidates = item.candidates?.length ? item.candidates : item.reference ? [item.reference] : [];
    return candidates.some((reference) => (reference.sources || [])
      .some((source) => source[owner[0]] === owner[1]));
  });
  if (owned?.length === 1) return owned[0];
  if (owned?.length > 1) return {status: "ambiguous", candidates: owned.flatMap((item) =>
    item.candidates?.length ? item.candidates : item.reference ? [item.reference] : [])};
  // A reference collected from an actual tool response is stronger than a
  // prefix-only guess. For example, `i-*` is shared by ECS and ENS: when the
  // resource index proves that this exact value came from ENS, do not ask the
  // user to choose ECS merely because the token also matches the ECS pattern.
  // Keep the chooser when more than one observed reference really exists.
  const observed = supported.filter((item) => item.reference?.referenceId ||
    item.candidates?.some((candidate) => candidate.referenceId));
  if (observed.length === 1) return observed[0];
  return {status: "ambiguous", candidates: supported.flatMap((item) =>
    item.candidates?.length ? item.candidates : item.reference ? [item.reference] : [])};
}

export function resourceLinkAccessibleLabel(occurrence, resolution) {
  // A port is only a display alias. Announce the resolved listener's exact
  // parent/protocol/port rather than an ambiguous number reused by other LBs.
  const ref = resolution.status === "resolved" ? resolution.reference : null;
  if (ref?.resourceType === "alicloud_slb_listener" &&
      /^lb-[A-Za-z0-9]+:(http|https|tcp|udp):[1-9][0-9]*$/.test(ref.identifier || "")) {
    return ref.identifier;
  }
  return occurrence.displayText;
}

function setMatchAttributes(element, state, piece) {
  const {occurrence, resolution, focusKey} = state;
  const values = {"data-resource-link":`${occurrence.start}:${occurrence.end}`,
    class:resolution.status === "resolved" ? "resource-link" : "resource-link resource-link-pending",
    ...(piece === 0 ? {"data-resource-focus":focusKey, "aria-label":resourceLinkAccessibleLabel(occurrence, resolution)}
      : {tabindex:"-1", "aria-hidden":"true"}),
    ...(resolution.status === "resolved" ? {href:resolution.url, target:"_blank", rel:"noopener noreferrer"} : {type:"button"}),
  };
  for (const [name, value] of Object.entries(values)) {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
  }
}

export function recoverResourceLinkActivation(element, occurrence, previous, recover) {
  if (element?.isConnected === false || typeof recover !== "function") return null;
  let current;
  try { current = recover(element, occurrence, previous); } catch (_error) { return null; }
  if (!current?.resolution || typeof current.activate !== "function") return null;
  // A stale visible link may only recover in one click when the current
  // resource index proves the exact same reviewed target.  A changed target
  // must wait for re-decoration instead of navigating through an old href.
  if (current.resolution.status === "resolved" &&
      element.getAttribute("href") !== current.resolution.url) return null;
  return current;
}

export function openResourceTargetInNewTab(element, url) {
  const doc = element?.ownerDocument;
  const view = doc?.defaultView;
  if (typeof view?.open !== "function" || typeof url !== "string" || !url) return false;
  // Open synchronously inside the trusted Shift click. A detached synthetic
  // anchor can be popup-blocked without reporting failure, which previously
  // swallowed the original click and opened nothing. The blank tab remains
  // same-origin long enough to sever `opener` before navigating.
  let target;
  try { target = view.open("about:blank", "_blank"); } catch (_error) { return false; }
  if (!target) return false;
  try {
    target.opener = null;
    target.location.replace(url);
    return true;
  } catch (_error) {
    try { target.close(); } catch (_closeError) { /* Best effort for an unusable blank tab. */ }
    return false;
  }
}

export function isShiftNewTabGesture(event) {
  return event?.button === 0 && Boolean(event.shiftKey) && !event.metaKey && !event.ctrlKey;
}

function createMatchElement(root, state, piece) {
  const element = root.ownerDocument.createElement(state.resolution.status === "resolved" ? "a" : "button");
  setMatchAttributes(element, state, piece);
  const valid = () => root.contains(element) && state.validate(state.occurrence, state.resolution) &&
    (state.resolution.status !== "resolved" || element.getAttribute("href") === state.resolution.url);
  const activate = (activationMode) => state.activate(element, state.occurrence, state.resolution, activationMode);
  const click = (event) => {
    event.stopPropagation();
    let active = {occurrence:state.occurrence, resolution:state.resolution, activate};
    if (!valid()) {
      active = recoverResourceLinkActivation(element, state.occurrence, state.resolution, state.recover);
      if (!active) { event.preventDefault(); return; }
    }
    if (active.resolution.status !== "resolved") { event.preventDefault(); active.activate("context"); return; }
    if (isShiftNewTabGesture(event)) {
      // Native Shift+click opens a new *window* in Chromium. The product
      // contract is a new tab, so handle that exact gesture synchronously
      // inside the trusted click. Chromium may extend the current text
      // selection before dispatching this click, so honor the modifier
      // contract before the ordinary selection guard below. If the popup is
      // blocked, leave native navigation available as the fail-safe.
      let signed = false;
      try {
        signed = Boolean(state.shouldSignExternal?.(active.occurrence, active.resolution)) &&
          active.activate("external") === true;
      } catch (_error) { signed = false; }
      if (signed || openResourceTargetInNewTab(element, active.resolution.url)) event.preventDefault();
      return;
    }
    if (hasSelection(root)) { event.preventDefault(); return; }
    // Native external navigation is the fail-safe default. Only a previously
    // confirmed role-signin capability may intercept a normal activation.
    if (event.button === 0 && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
      let preview = false;
      try {
        preview = Boolean(state.shouldPreview?.(active.occurrence, active.resolution));
      } catch (_error) { /* Fall through to the native link. */ }
      if (preview) { event.preventDefault(); active.activate("preview"); }
    }
  };
  element.addEventListener("click", click);
  element.addEventListener("auxclick", click);
  element.addEventListener("contextmenu", (event) => { if (!valid()) event.preventDefault(); });
  element.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") event.stopPropagation();
    // Keep native modifier/middle-click navigation and the normal link menu.
    if (event.altKey && event.key === "Enter") { event.preventDefault(); activate("context"); }
  });
  return element;
}

function wrapMatch(root, segment, state) {
  const {occurrence} = state;
  let low = 0, high = segment.parts.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (segment.parts[mid].end <= occurrence.start) low = mid + 1;
    else high = mid;
  }
  const overlaps = [];
  for (let i = low; i < segment.parts.length && segment.parts[i].start < occurrence.end; i += 1) overlaps.push(segment.parts[i]);
  for (let index = overlaps.length - 1; index >= 0; index -= 1) {
    const part = overlaps[index];
    const start = Math.max(0, occurrence.start - part.start);
    const end = Math.min(part.end - part.start, occurrence.end - part.start);
    if (end < part.node.data.length) part.node.splitText(end);
    const matched = start ? part.node.splitText(start) : part.node;
    const element = createMatchElement(root, state, index);
    matched.replaceWith(element);
    element.append(matched);
    state.nodes[index] = element;
  }
}

function reusableMatch(state, planned, positions) {
  if (!planned || !state.nodes.length) return false;
  let offset = planned.occurrence.start;
  for (const node of state.nodes) {
    const position = positions.get(node);
    if (!position || position.segment !== planned.segmentNumber || position.start !== offset) return false;
    offset = position.end;
  }
  return offset === planned.occurrence.end;
}

/** Synchronous generator allows the scheduler to pause between logical blocks. */
function* decorationWork(root, options, index, rules, activate, track,
  resolveMatch = (item) => resolveResourceOccurrenceAlternatives(item, index, rules), validate = () => true,
  selection = undefined, shouldPreview = () => false, recover = null, shouldSignExternal = () => false) {
  if (hasSelection(root, selection)) return;
  const focused = root.ownerDocument.activeElement;
  const focusKey = root.contains(focused) ? focused.getAttribute?.("data-resource-focus") : null;
  const rendered = options.renderCache || {groups:new Map(), initialized:false};
  if (!rendered.initialized) {
    yield* removeDecorationWork(root);
    rendered.initialized = true;
  }
  // Preserve ordinary authored/automatic links. Only unwrap a linkify-created
  // bare-domain anchor after the same visible source has an observed cloud
  // resource fact, allowing the normal resource wrapper to own the click.
  // Cache only this decoration pass: later SSE renders may change table cells.
  const tables = new WeakMap();
  yield* releaseObservedResourceAutolinks(root, options, index, rules, track, resolveMatch, tables);
  const planned = new Map(), positions = new Map();
  const rootText = root.textContent || "";
  const segments = logicalSegments(root, options.context || {}, tables);
  let current = yield* nextSegment(segments);
  let segmentNumber = 0;
  const sourceBoundary = typeof options.bindingValue === "string" &&
    /[\s"'`，。！？；、,;:!?)}\]]$/u.test(options.bindingValue.slice(-1));
  while (!current.done) {
    const segment = current.value;
    const next = yield* nextSegment(segments);
    for (let i = 0; i < segment.parts.length; i++) {
      const part = segment.parts[i], wrapper = part.node.parentElement;
      if (own(wrapper)) {
        const previous = positions.get(wrapper);
        positions.set(wrapper, {segment:segmentNumber,
          start:previous?.segment === segmentNumber ? previous.start : part.start, end:part.end});
      }
      if (i % 128 === 0) yield;
    }
    const context = {...segment.context, ...options.source};
    if (!context.resourceType && !context.toolUseId) {
      // Markdown assistant replies put the product/action label in a prose
      // block immediately before a fenced JSON block.  Since logicalSegments
      // intentionally scans blocks independently, recover that nearest scope
      // from the registered message root so a shared Name cannot borrow a
      // same-spelled resource from another product.
      const sectionProduct = nearestProductScope(rootText, segment.start);
      if (sectionProduct) context.sectionProduct = sectionProduct;
    }
    const complete = context.complete === false ? false : options.complete !== false || !next.done || sourceBoundary;
    const cached = options.scanCache?.get(segmentNumber);
    const {structuralScope, ...contextFields} = context;
    const contextVersion = JSON.stringify(contextFields);
    let occurrences;
    if (cached && cached.text === segment.text && cached.contextVersion === contextVersion &&
        cached.structuralScope === structuralScope &&
        cached.complete === complete && cached.revision === index.revision) {
      occurrences = cached.occurrences;
      for (let i = 0; i < cached.tracked.length; i++) {
        track(cached.tracked[i]);
        if (i % 128 === 0) yield;
      }
    } else {
      const tracked = new Map();
      occurrences = yield* scanResourceOccurrencesWork(segment.text, {
        index, context, field: context.field, complete,
        trackCandidate: (item) => {
          track(item);
          tracked.set(JSON.stringify([item.identifierKind,item.identifier]), item);
        },
      });
      options.scanCache?.set(segmentNumber, {text:segment.text, contextVersion, structuralScope, complete, revision:index.revision,
        occurrences, tracked:[...tracked.values()]});
    }
    for (let i = 0; i < occurrences.length; i++) {
      track(occurrences[i]);
      if (i % 128 === 0) yield;
    }
    for (let i = occurrences.length - 1; i >= 0; i -= 1) {
      const occurrence = occurrences[i];
      const resolution = resolveMatch(occurrence);
      if (["resolved", "needs_context", "ambiguous"].includes(resolution.status)) {
        const focusKey = `${segmentNumber}:${occurrence.start}:${occurrence.end}`;
        planned.set(focusKey, {segment, segmentNumber, occurrence, resolution, focusKey});
      }
      // Bound planning work even when every occurrence is in one large <pre>.
      if (i % 32 === 0) yield;
    }
    current = next;
    segmentNumber += 1;
    yield;
  }
  // Plan before changing the DOM. Exact text ranges, not just equal locator
  // strings, determine which existing wrappers can be retained. Remove stale
  // ranges before inserting new ones so expanded/withdrawn matches never nest.
  let count = 0;
  const retained = new Set();
  for (const [key, state] of rendered.groups) {
    if (!reusableMatch(state, planned.get(key), positions)) {
      for (const node of state.nodes) if (root.contains(node)) node.replaceWith(...node.childNodes);
      rendered.groups.delete(key);
    } else for (const node of state.nodes) retained.add(node);
    if (++count % 32 === 0) yield;
  }
  // A caller may have re-decorated the same DOM using the synchronous API.
  // Those wrappers have no live state in this controller and cannot be nested
  // or trusted as its retained event handlers.
  for (const wrapper of positions.keys()) {
    if (!retained.has(wrapper) && root.contains(wrapper)) wrapper.replaceWith(...wrapper.childNodes);
    if (++count % 32 === 0) yield;
  }
  for (const [key, item] of planned) {
    let state = rendered.groups.get(key);
    if (!state) {
      state = {occurrence:item.occurrence, resolution:item.resolution, focusKey:item.focusKey, nodes:[], activate, validate,
        shouldPreview, shouldSignExternal, recover};
      wrapMatch(root, item.segment, state);
      rendered.groups.set(key, state);
    } else {
      const changed = state.resolution.status !== item.resolution.status || state.resolution.url !== item.resolution.url ||
        state.occurrence.displayText !== item.occurrence.displayText;
      state.occurrence = item.occurrence;
      state.resolution = item.resolution;
      if (changed) for (let piece = 0; piece < state.nodes.length; piece++) {
        const node = state.nodes[piece];
        if (node.tagName === (state.resolution.status === "resolved" ? "A" : "BUTTON")) setMatchAttributes(node, state, piece);
        else {
          const replacement = createMatchElement(root, state, piece);
          node.replaceWith(replacement); replacement.append(...node.childNodes);
          state.nodes[piece] = replacement;
        }
      }
    }
    if (++count % 32 === 0) yield;
  }
  if (focusKey && root.ownerDocument.activeElement === root.ownerDocument.body) {
    // Re-decoration must not steal focus if the user moved it during a yield.
    const replacement = [...root.querySelectorAll("[data-resource-focus]")]
      .find((node) => node.getAttribute("data-resource-focus") === focusKey);
    replacement?.focus({preventScroll: true});
  }
}

export function decorateResourceLinks(root, {index, rules = RESOURCE_LINK_RULES, onActivate = () => {}, ...options}) {
  for (const _ of decorationWork(root, options, index, rules, onActivate, () => {})) { /* Synchronous test/small-container API. */ }
}

export function createResourceLinkController({index, rules = RESOURCE_LINK_RULES, onActivate = () => {},
  shouldPreview = () => false, shouldSignExternal = () => false, onPersistChoice = null, onQueryAttributes = null, onLoadAccountContext = null}) {
  const roots = new Map(), byLocator = new Map(), byOwner = new Map(), queue = new Map(), priority = new Set(), sources = new Set();
  const idleWaiters = new Set();
  const digestWork = new Set();
  let timer = null, interactionResumeTimer = null, resourceLinkPointerDown = false, disposed = false;
  let generation = index.generation;
  const scheduleFrame = globalThis.requestAnimationFrame || ((fn) => setTimeout(fn, 0));
  const cancelFrame = globalThis.cancelAnimationFrame || clearTimeout;
  const key = (occurrence) => JSON.stringify([occurrence.identifierKind, occurrence.identifier]);
  const contextKey = (occurrence) => JSON.stringify(resourceOccurrenceGroup(occurrence));
  function settle() {
    if (!queue.size && !digestWork.size) {for (const done of idleWaiters) done(); idleWaiters.clear();}
  }

  function forget(root) {
    const record = roots.get(root);
    record?.source.roots.delete(root);
    for (const locator of record?.locators || []) {
      byLocator.get(locator)?.delete(root);
      if (!byLocator.get(locator)?.size) byLocator.delete(locator);
    }
    for (const owner of record?.owners || []) {
      byOwner.get(owner)?.delete(root);
      if (!byOwner.get(owner)?.size) byOwner.delete(owner);
    }
    roots.delete(root); queue.delete(root); priority.delete(root);
  }
  function trackOwners(root, context) {
    const record = roots.get(root);
    for (const field of ["turnId", "messageId", "toolUseId"]) {
      if (!context[field]) continue;
      const owner = JSON.stringify([field, context[field]]);
      record.owners.add(owner);
      if (!byOwner.has(owner)) byOwner.set(owner, new Set());
      byOwner.get(owner).add(root);
    }
  }
  function current(root, record) {
    return !disposed && roots.get(root) === record && record.generation === index.generation && root.isConnected &&
      sources.has(record.source) && root.textContent === record.source.text;
  }
  function invalidateSource(source) {
    for (const invalidate of source.invalidators) invalidate();
    source.invalidators.clear();
  }
  function liveRoot(record) {
    // The application rebuilds transcript DOM for unrelated SSE events. An
    // interaction belongs to the exact source version, not a discarded node.
    // Never reuse it for a changed source, even if the locator text is equal.
    for (const root of record.source.roots) {
      const owner = roots.get(root);
      if (owner?.source === record.source && current(root, owner)) return root;
    }
    return null;
  }
  function enqueueSource(source) {
    for (const root of source.roots) enqueue(root);
  }
  function resolutionFor(occurrence, record) {
    const variants = [occurrence, ...(occurrence.alternatives || [])];
    const choice = record.source.choices.get(contextKey(occurrence));
    if (choice) {
      const selected = variants
        .map((item) => resolveResourceChoice(item, choice, index, rules)).find(Boolean);
      if (selected) return selected;
      record.source.choices.delete(contextKey(occurrence));
    }
    if (record.source.digest) {
      for (const item of variants) {
        const binding = index.bindingFor({...item,
          sourceKey:resourceBindingSourceKey(record.source.digest,resourceOccurrenceGroup(item))});
        const selected = binding && resolveResourceChoice(item,binding,index,rules);
        if (selected) return selected;
      }
    }
    return resolveResourceOccurrenceAlternatives(occurrence, index, rules);
  }
  function activate(root, record, element, occurrence, _resolution, activationMode = "default") {
    if (!current(root, record)) return;
    const savedChoice = record.source.choices.get(contextKey(occurrence)) || (record.source.digest &&
      [occurrence,...(occurrence.alternatives || [])].map((item) => index.bindingFor({...item,
        sourceKey:resourceBindingSourceKey(record.source.digest,resourceOccurrenceGroup(item))})).find(Boolean));
    let initial = resolveResourceOccurrenceAlternatives(occurrence,index,rules);
    const selected = savedChoice && resolutionFor(occurrence,record);
    if (selected?.reason === "deleted_name") {
      // Opening an old bound name must explain that lifecycle before offering
      // replacements, even when the unbound name now has multiple candidates.
      initial = selected;
    } else if (savedChoice?.referenceId && !initial.candidates?.length) {
      initial = {...initial,reference:index.references.get(savedChoice.referenceId) || initial.reference};
    }
    if (["preview", "external"].includes(activationMode) && _resolution.status === "resolved") initial = _resolution;
    // Context editing starts from all currently compatible facts, not the old
    // selected URL. The callback applies only to this exact source version.
    const isCurrentSource = () => Boolean(liveRoot(record));
    return onActivate(element, occurrence, initial, {
      ...record.options,
      activationMode,
      currentChoice: savedChoice,
      loadAccountContext: onLoadAccountContext ? (isOpen = () => true) => onLoadAccountContext({
        sessionId:index.sessionId,isCurrent:() => isCurrentSource() && isOpen(),
      }) : null,
      isCurrent: isCurrentSource,
      onInvalidate(listener) { record.source.invalidators.add(listener); return () => record.source.invalidators.delete(listener); },
      restoreFocus() {
        const owner = liveRoot(record);
        if (!owner) return;
        const focus = element.getAttribute("data-resource-focus");
        [...owner.querySelectorAll("[data-resource-focus]")].find((node) => node.getAttribute("data-resource-focus") === focus)
          ?.focus({preventScroll:true});
      },
      queryAttributes: onQueryAttributes ? async (referenceId, isOpen = () => true) => {
        const isCurrent = () => isCurrentSource() && isOpen();
        const stale = {status:"needs_context",reason:"resource_query_source_changed"};
        if (!isCurrent()) return stale;
        const digest = await ensureDigest(record,isCurrent);
        if (!digest || !isCurrent()) return stale;
        const reference = index.references.get(referenceId);
        const spec = resourceSpec(reference?.resourceType);
        const specific = [occurrence,...(occurrence.alternatives || [])].find((item) => item.resourceType === spec?.resourceType);
        if (!specific || !index.hasLocator(referenceId,specific)) return stale;
        return onQueryAttributes({expectedRevision:index.revision,source:bindingSource(record.options.source),sourceDigest:digest,
          group:resourceOccurrenceGroup(specific),identifier:specific.identifier,identifierKind:specific.identifierKind,
          referenceId,queryMissingAttributes:true}, {sessionId:index.sessionId,isCurrent});
      } : null,
      persistChoice: onPersistChoice ? async (choice) => {
        if (!isCurrentSource()) return {status:"not_saved"};
        const digest = await ensureDigest(record,isCurrentSource);
        if (!digest || !isCurrentSource()) return {status:"not_saved"};
        const spec = resourceSpec(index.references.get(choice.referenceId)?.resourceType || choice.resourceType || occurrence.resourceType);
        const specific = [occurrence,...(occurrence.alternatives || [])].find((item) => item.resourceType === spec?.resourceType);
        if (!specific) return {status:"not_saved"};
        const resolved = resolveResourceChoice(specific,choice,index,rules);
        if (resolved?.status !== "resolved") return {status:"not_saved"};
        const referenceId = choice.referenceId || resolved.reference?.referenceId;
        const context = Object.fromEntries(["accountScope","consolePartition",...(spec?.scopeKind === "regional" ? ["regionId"] : [])]
          .filter((field) => choice[field]).map((field) => [field,choice[field]]));
        context.attributes = Object.fromEntries((spec?.parentFields || []).filter((field) => choice.attributes?.[field])
          .map((field) => [field,choice.attributes[field]]));
        const result = await onPersistChoice({expectedRevision:index.revision,source:bindingSource(record.options.source),sourceDigest:digest,
          group:resourceOccurrenceGroup(specific),identifier:specific.identifier,identifierKind:specific.identifierKind,
          referenceId,context,...(choice.accountContextId ? {accountContextId:choice.accountContextId} : {})},
          {sessionId:index.sessionId,isCurrent:isCurrentSource});
        if (result?.status === "saved" && isCurrentSource()) {
          // A durable choice must subsequently follow server revocation, not
          // remain shadowed by the optimistic in-memory selection.
          record.source.choices.delete(contextKey(occurrence));
          enqueueSource(record.source);
        }
        return result;
      } : null,
      onChoose(choice) {
        if (!isCurrentSource()) return false;
        const result = [occurrence, ...(occurrence.alternatives || [])]
          .map((item) => resolveResourceChoice(item, choice, index, rules)).find((item) => item?.status === "resolved");
        if (!result) return false;
        record.source.choices.set(contextKey(occurrence), {
          referenceId: choice.referenceId,
          accountScope: choice.accountScope, consolePartition: choice.consolePartition, regionId: choice.regionId,
          attributes: {...choice.attributes},
        });
        enqueueSource(record.source);
        return true;
      },
    });
  }
  async function ensureDigest(record, isCurrent) {
    if (!Object.prototype.hasOwnProperty.call(record.options,"bindingValue")) return null;
    if (record.source.digest) return record.source.digest;
    if (!record.source.digestPromise) {
      const pending = digestResourceSource(bindingSource(record.options.source),record.options.bindingValue).catch(() => null);
      record.source.digestPromise = pending;
      digestWork.add(pending);
      pending.finally(() => {digestWork.delete(pending); queueMicrotask(settle);});
    }
    const digest = await record.source.digestPromise;
    if (!isCurrent()) return null;
    if (digest && record.source.digest !== digest) {
      record.source.digest = digest;
      enqueueSource(record.source);
    }
    return digest;
  }
  function enqueue(root, prioritize = false) {
    const record = roots.get(root);
    if (!record || disposed) return;
    if (index.hasSourceBindings?.(record.options.source || {})) void ensureDigest(record,() => Boolean(liveRoot(record)));
    queue.set(root, null);
    if (prioritize) {
      // A resource-reference delta can arrive after the newest message has
      // already completed its first scan. Move that affected root to the end
      // of the insertion-ordered set so the reverse-priority pass processes
      // it next instead of leaving it behind a full historical transcript.
      priority.delete(root);
      priority.add(root);
    }
    if (timer === null && !resourceLinkPointerDown) timer = scheduleFrame(run);
  }
  function run() {
    timer = null;
    // Freeze cooperative DOM reconciliation for the active pointer gesture so
    // a long transcript cannot replace or move the exact anchor between
    // pointerdown and click.
    if (resourceLinkPointerDown) return;
    for (const root of roots.keys()) if (!root.isConnected) forget(root);
    for (const source of sources) if (!source.roots.size) {
      invalidateSource(source);
      sources.delete(source);
    }
    const deadline = performance.now() + 8;
    // Selection getters can force layout. Read once before this frame's DOM
    // writes; never re-read after each block or again after inserting wrappers.
    const selection = globalThis.document?.getSelection?.();
    const selectionState = selection ? {
      isCollapsed: selection.isCollapsed, anchorNode: selection.anchorNode, focusNode: selection.focusNode,
    } : null;
    // A transcript rebuild registers every historical message before the new
    // answer. Give each freshly registered DOM root one immediate slice in
    // newest-first order so the visible result does not wait dozens of frames.
    // After that slice an unfinished root rejoins the ordinary round-robin.
    // Registration follows transcript DOM order, so reverse the one-shot
    // priority set: the newest visible answer is registered last and must be
    // decorated before the historical messages rebuilt ahead of it.
    const ordered = [...priority].reverse().filter((root) => queue.has(root));
    for (const root of queue.keys()) if (!priority.has(root)) ordered.push(root);
    for (const root of ordered) {
      const existing = queue.get(root);
      if (!root.isConnected) { forget(root); continue; }
      const record = roots.get(root);
      if (!record || record.generation !== index.generation) { forget(root); continue; }
      if (hasSelection(root, selectionState)) continue;
      priority.delete(root);
      const work = existing || decorationWork(root, {...record.options, scanCache:record.source.scans, renderCache:record.rendered}, index, rules,
        (element, occurrence, resolution, activationMode) => activate(root, record, element, occurrence, resolution, activationMode),
        (occurrence) => {
          trackOwners(root, occurrence);
          const locator = key(occurrence);
          record.locators.add(locator);
          if (!byLocator.has(locator)) byLocator.set(locator, new Set());
          byLocator.get(locator).add(root);
      }, (occurrence) => resolutionFor(occurrence, record), (occurrence, previous) => {
          if (!current(root, record)) return false;
          const latest = resolutionFor(occurrence, record);
          return latest.status === previous.status && latest.url === previous.url;
        }, selectionState, shouldPreview,
        (element, occurrence, previous) => {
          const live = roots.get(root);
          if (!live || !current(root, live)) return null;
          const resolution = resolutionFor(occurrence, live);
          enqueue(root, true);
          return {occurrence, resolution,
            activate:(activationMode) => activate(root, live, element, occurrence, resolution, activationMode)};
        }, shouldSignExternal);
      queue.set(root, work);
      let step;
      do { step = work.next(); } while (!step.done && performance.now() < deadline);
      if (step.done) queue.delete(root);
      else {
        // Round-robin unfinished roots.  A single historical megabyte reply
        // must not consume every animation frame while a new assistant reply
        // containing freshly observed resource facts waits behind it.
        queue.delete(root);
        queue.set(root, work);
      }
      if (performance.now() >= deadline) break;
    }
    if ([...queue.keys()].some((root) => !hasSelection(root, selectionState))) timer = scheduleFrame(run);
    settle();
  }
  const unsubscribe = index.subscribe((change) => {
    if (index.generation !== generation) {
      generation = index.generation;
      for (const root of roots.keys()) forget(root);
      for (const source of sources) invalidateSource(source);
      sources.clear();
      settle();
      return;
    }
    for (const locator of change.changed) for (const root of byLocator.get(locator) || []) enqueue(root, true);
    for (const owner of change.changedOwners || []) for (const root of byOwner.get(owner) || []) enqueue(root, true);
  });
  const scheduleQueuedWork = () => {
    if (!disposed && !resourceLinkPointerDown && queue.size && timer === null) timer = scheduleFrame(run);
  };
  const preventShiftResourceLinkSelection = (event) => {
    if (event.target?.closest?.("[data-resource-link]") && isShiftNewTabGesture(event)) event.preventDefault();
  };
  const pauseForResourceLinkPointer = (event) => {
    if (!event.target?.closest?.("[data-resource-link]")) return;
    // Shift+pointerdown on text can extend the current selection before the
    // subsequent click reaches the link. Suppress only that browser default;
    // the click handler still owns the reviewed new-tab navigation.
    preventShiftResourceLinkSelection(event);
    resourceLinkPointerDown = true;
    if (interactionResumeTimer !== null) {
      clearTimeout(interactionResumeTimer);
      interactionResumeTimer = null;
    }
    if (timer !== null) {
      cancelFrame(timer);
      timer = null;
    }
  };
  const resumeAfterResourceLinkPointer = (event) => {
    if (!resourceLinkPointerDown) return;
    if (interactionResumeTimer !== null) clearTimeout(interactionResumeTimer);
    // A document capture listener observes click before the link's own
    // handler. Defer resumption so the trusted activation finishes against
    // the stable anchor before any queued reconciliation can run.
    interactionResumeTimer = setTimeout(() => {
      interactionResumeTimer = null;
      resourceLinkPointerDown = false;
      scheduleQueuedWork();
    }, 0);
  };
  const selectionChanged = scheduleQueuedWork;
  const document = globalThis.document;
  document?.addEventListener?.("selectionchange", selectionChanged);
  document?.addEventListener?.("pointerdown", pauseForResourceLinkPointer, true);
  // Playwright, assistive technology, and older Chromium paths can emit the
  // compatibility mouse event without a cancelable PointerEvent. Cover both
  // so a Shift activation never becomes a range-selection gesture.
  document?.addEventListener?.("mousedown", preventShiftResourceLinkSelection, true);
  for (const event of ["pointerup", "pointercancel", "click", "auxclick"]) {
    document?.addEventListener?.(event, resumeAfterResourceLinkPointer, true);
  }
  return {
    register(root, options) {
      if (!root?.ownerDocument?.createTreeWalker) return; // Inert outside a real DOM.
      forget(root);
      // Keep raw text once per registered source, not once per occurrence. It
      // never leaves this in-memory UI store and is not a persistence claim.
      const sourceId = Object.keys(options.source || {}).length ? JSON.stringify(options.source) : root;
      const text = root.textContent, context = JSON.stringify(options.context || {});
      let bindingVersion;
      try { bindingVersion = typeof options.bindingValue === "string" ? options.bindingValue
        : options.bindingValue === undefined ? undefined : canonicalSourceJson(options.bindingValue); }
      catch (_error) { bindingVersion = Symbol("invalid-source"); }
      const complete = options.complete !== false;
      let source = [...sources].find((candidate) => candidate.sourceId === sourceId && candidate.text === text &&
        candidate.context === context && candidate.bindingVersion === bindingVersion && candidate.complete === complete);
      if (!source) {
        source = {sourceId, text, context, bindingVersion, complete, roots:new Set(), invalidators:new Set(),
          choices: new Map(), scans: new Map(),digest:null,digestPromise:null};
        sources.add(source);
      }
      source.roots.add(root);
      roots.set(root, {options, sourceId, source, generation: index.generation, locators: new Set(), owners: new Set(),
        rendered:{groups:new Map(), initialized:false}});
      trackOwners(root, {...options.context, ...options.source});
      // renderMessages replaces the whole transcript. Even unchanged sources
      // therefore own fresh DOM nodes; prioritizing every fresh root and
      // consuming them newest-first is more robust than inferring which source
      // text changed during the final streaming render.
      enqueue(root, true);
    },
    invalidate(root) { enqueue(root); },
    whenIdle() { return queue.size || digestWork.size ? new Promise((done) => idleWaiters.add(done)) : Promise.resolve(); },
    dispose() {
      disposed = true; unsubscribe();
      if (timer !== null) cancelFrame(timer);
      if (interactionResumeTimer !== null) clearTimeout(interactionResumeTimer);
      for (const root of roots.keys()) forget(root);
      for (const source of sources) invalidateSource(source);
      sources.clear();
      priority.clear();
      digestWork.clear();
      settle();
      document?.removeEventListener?.("selectionchange", selectionChanged);
      document?.removeEventListener?.("pointerdown", pauseForResourceLinkPointer, true);
      document?.removeEventListener?.("mousedown", preventShiftResourceLinkSelection, true);
      for (const event of ["pointerup", "pointercancel", "click", "auxclick"]) {
        document?.removeEventListener?.(event, resumeAfterResourceLinkPointer, true);
      }
    },
  };
}
