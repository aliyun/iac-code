// Facts only: URL construction and text recognition live in separate modules.
const IDENTITY_FIELDS = [
  "provider", "resourceType", "identifier", "identifierKind", "identifierField", "scopeKind",
  "accountScope", "consolePartition", "regionId", "stackId", "logicalResourceId", "status",
  "evidence", "referenceId",
];
const SOURCE_FIELDS = [
  "toolUseId", "messageId", "turnId", "sourceId", "sourcePath", "requestRegionId",
  "stackId", "logicalResourceId", "action", "product", "evidence", "observedAt",
];
const BINDING_FIELDS = ["sourceKey", "identifier", "identifierKind", "referenceId"];
const isObject = (value) => Boolean(value && typeof value === "object" && !Array.isArray(value));
const validRevision = (value) => Number.isSafeInteger(value) && value >= 0;
const locatorKey = (kind, value) => JSON.stringify([kind, value]);
const bindingKey = (binding) => JSON.stringify([binding.sourceKey, binding.identifierKind, binding.identifier]);
export const isDeclaredResource = (reference) => Boolean(reference?.sources?.length &&
  reference.sources.every((source) => source.sourceId?.startsWith("web-choice:")));

/** Load local credential context without adding it to resource facts. */
export async function loadResourceAccountContext(index, {sessionId, isCurrent, load}) {
  const generation = index.generation;
  const current = () => generation === index.generation && sessionId === index.sessionId && isCurrent();
  const unavailable = {status:"unavailable"};
  if (!current()) return unavailable;
  try {
    const result = await load(sessionId);
    const choice = result?.choice;
    if (!current() || result?.status !== "available" ||
      !/^account-context:[0-9a-f]{32}$/.test(choice?.accountContextId || "") ||
      typeof choice?.accountScope !== "string" || !choice.accountScope || choice.accountScope.length > 1024 ||
      /[\u0000-\u001f\u007f]/.test(choice.accountScope) ||
      (choice.consolePartition && !["aliyun","alibabacloud"].includes(choice.consolePartition))) return unavailable;
    const safeChoice = {accountContextId:choice.accountContextId,accountScope:choice.accountScope,
      ...(choice.consolePartition ? {consolePartition:choice.consolePartition} : {})};
    // This is only a default scope for facts already proved by a tool response;
    // it never creates a resource fact or makes unrelated prose linkable.
    index.setAccountContext(safeChoice);
    return {status:"available",choice:safeChoice};
  } catch (_error) { return unavailable; }
}

/** User-triggered save only; a late response cannot update another source/session. */
export async function persistResourceChoice(index, payload, {sessionId, isCurrent, save, fetchPage}) {
  const generation = index.generation;
  const current = () => generation === index.generation && sessionId === index.sessionId && isCurrent();
  if (!current()) return {status:"not_saved"};
  try {
    const result = await save(sessionId,payload);
    if (!current()) return {status:"not_saved"};
    const loaded = result?.snapshot && await index.loadSnapshot(result.snapshot,fetchPage,current);
    if (result?.status === "saved") {
      const binding = index.bindingFor({sourceKey:result.sourceKey,
        identifier:payload.identifier,identifierKind:payload.identifierKind});
      if (!binding || binding.referenceId !== (payload.referenceId || result.referenceId)) return {status:"not_saved"};
      for (const [field,value] of Object.entries(payload.context || {})) {
        if (field === "attributes") {
          if (Object.entries(value).some(([key,item]) => binding.attributes?.[key] !== item)) return {status:"not_saved"};
        } else if (binding[field] !== value) return {status:"not_saved"};
      }
    }
    return current() && loaded ? result : {status:"not_saved"};
  } catch (error) {
    if (current() && error.status === 409) await index.refresh(fetchPage,current);
    return {status:"not_saved"};
  }
}

/** Explicit attribute lookup; never called by rendering, matching or refresh. */
export async function queryResourceAttributes(index, payload, {sessionId, isCurrent, query, fetchPage}) {
  const generation = index.generation;
  const current = () => generation === index.generation && sessionId === index.sessionId && isCurrent();
  const stale = {status:"needs_context",reason:"resource_query_source_changed"};
  const original = index.references.get(payload.referenceId);
  if (!current() || !original || !index.hasLocator(payload.referenceId,payload)) return stale;
  try {
    const result = await query(sessionId,payload);
    if (!current()) return stale;
    if (result?.status !== "attributes_loaded") return {status:"needs_context",reason:result?.reason || "resource_query_failed"};
    if (!result.snapshot || !await index.loadSnapshot(result.snapshot,fetchPage,current) || !current()) return stale;
    // Never use a server-supplied URL or treat a partial/failed snapshot as a
    // successful lookup. The popover resolves the updated fact through rules.
    const updated = index.references.get(result.referenceId);
    if (!updated || !index.hasLocator(result.referenceId,payload) ||
      index.canonicalType(updated.resourceType) !== index.canonicalType(original.resourceType) ||
      updated.accountScope !== original.accountScope || updated.consolePartition !== original.consolePartition) return stale;
    return {status:"attributes_loaded",referenceId:result.referenceId,persistenceStatus:result.persistenceStatus};
  } catch (error) {
    if (current() && error?.status === 409) await index.refresh(fetchPage,current);
    return current() ? {status:"needs_context",reason:error?.status === 409 ? "resource_query_source_changed" : "resource_query_failed"} : stale;
  }
}

function pick(value, fields) {
  return Object.fromEntries(fields.filter((key) => value[key] !== undefined).map((key) => [key, value[key]]));
}

function cleanLocator(value) {
  if (!isObject(value) || !["id", "name"].includes(value.identifierKind)) return null;
  if (typeof value.identifier !== "string" || !value.identifier || value.identifier.length > 1024) return null;
  if (typeof value.identifierField !== "string") return null;
  return pick(value, ["identifier", "identifierKind", "identifierField"]);
}

function cleanReference(value) {
  if (!cleanLocator(value) || value.provider !== "aliyun") return null;
  if (typeof value.referenceId !== "string" || !value.referenceId.startsWith("ref-")) return null;
  if (typeof value.resourceType !== "string" || !["account", "regional"].includes(value.scopeKind)) return null;
  const reference = pick(value, IDENTITY_FIELDS);
  reference.attributes = Object.fromEntries(Object.entries(isObject(value.attributes) ? value.attributes : {})
    .filter(([key, item]) => !["__proto__", "constructor", "prototype"].includes(key) && typeof item === "string"));
  reference.aliases = (Array.isArray(value.aliases) ? value.aliases : []).map(cleanLocator).filter(Boolean);
  reference.sources = (Array.isArray(value.sources) ? value.sources : []).filter(isObject)
    .map((source) => pick(source, SOURCE_FIELDS));
  if (reference.scopeKind === "account") delete reference.regionId;
  return reference;
}

function locators(reference) {
  return [reference, ...reference.aliases].map((item) => locatorKey(item.identifierKind, item.identifier));
}

function contextOwners(references) {
  const owners = new Set();
  for (const reference of references) for (const source of reference?.sources || []) {
    for (const field of ["turnId", "messageId", "toolUseId"]) {
      if (source[field]) owners.add(JSON.stringify([field, source[field]]));
    }
  }
  return owners;
}

function observedContexts(index, occurrence, reference = null) {
  const owner = occurrence.turnId ? ["turnId", occurrence.turnId]
    : occurrence.messageId ? ["messageId", occurrence.messageId]
    : occurrence.toolUseId ? ["toolUseId", occurrence.toolUseId] : null;
  const contexts = new Map();
  if (!owner) return contexts;
  const regional = (reference?.scopeKind || occurrence.scopeKind) !== "account";
  for (const candidate of index.references.values()) {
    if (isDeclaredResource(candidate) || !candidate.accountScope || !candidate.consolePartition) continue;
    if (occurrence.accountScope && candidate.accountScope !== occurrence.accountScope) continue;
    if (occurrence.consolePartition && candidate.consolePartition !== occurrence.consolePartition) continue;
    if (regional && (!candidate.regionId || candidate.scopeKind !== "regional" ||
      (occurrence.regionId && candidate.regionId !== occurrence.regionId))) continue;
    if (!(candidate.sources || []).some((source) => source[owner[0]] === owner[1])) continue;
    const context = {accountScope:candidate.accountScope, consolePartition:candidate.consolePartition,
      ...(regional ? {regionId:candidate.regionId} : {})};
    contexts.set(JSON.stringify(context), context);
  }
  return contexts;
}

/** One in-memory index per selected session; never part of the text-delta reducer. */
export class ResourceIndex {
  constructor({ canonicalType = (value) => value } = {}) {
    this.canonicalType = canonicalType;
    this.sessionId = null;
    this.generation = 0;
    this.revision = 0;
    this.observedRevision = 0;
    this.references = new Map();
    this.bindings = new Map();
    this.byLocator = new Map();
    this.listeners = new Set();
    this.needsSnapshot = false;
    this.persistenceStatus = "saved";
    this.accountContext = null;
    this._loading = null;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  _notify(changed, reset = false, changedOwners = new Set()) {
    const change = { changed, reset, changedOwners, revision: this.revision, generation: this.generation };
    for (const listener of this.listeners) {
      try { listener(change); } catch (_error) { /* Decoration cannot fail the fact stream. */ }
    }
  }

  setAccountContext(value) {
    const next = isObject(value) && typeof value.accountScope === "string" && value.accountScope &&
      (!value.consolePartition || ["aliyun","alibabacloud"].includes(value.consolePartition))
      ? pick(value, ["accountScope","consolePartition"])
      : null;
    if (JSON.stringify(next) === JSON.stringify(this.accountContext)) return;
    this.accountContext = next;
    this._notify(new Set(this.byLocator.keys()));
  }

  reset(sessionId) {
    this.generation += 1;
    this.sessionId = sessionId;
    const changed = new Set(this.byLocator.keys());
    this.references.clear();
    this.bindings.clear();
    this.byLocator.clear();
    this.revision = 0;
    this.observedRevision = 0;
    this.needsSnapshot = false;
    this.persistenceStatus = "saved";
    this.accountContext = null;
    this._loading = null;
    this._notify(changed, true);
  }

  _remove(referenceId, changed) {
    const previous = this.references.get(referenceId);
    if (!previous) return;
    for (const key of locators(previous)) {
      changed.add(key);
      const ids = this.byLocator.get(key);
      ids?.delete(referenceId);
      if (!ids?.size) this.byLocator.delete(key);
    }
    this.references.delete(referenceId);
  }

  _upsert(reference, changed) {
    this._remove(reference.referenceId, changed);
    this.references.set(reference.referenceId, reference);
    for (const key of locators(reference)) {
      changed.add(key);
      if (!this.byLocator.has(key)) this.byLocator.set(key, new Set());
      this.byLocator.get(key).add(reference.referenceId);
    }
  }

  _bindings(items) {
    for (const value of Array.isArray(items) ? items : []) {
      if (!isObject(value) || !BINDING_FIELDS.every((key) => typeof value[key] === "string")) continue;
      const binding = pick(value, BINDING_FIELDS);
      if (isObject(value.context)) {
        binding.context = Object.fromEntries(["accountScope", "consolePartition", "regionId"]
          .filter((key) => typeof value.context[key] === "string").map((key) => [key,value.context[key]]));
        binding.context.attributes = Object.fromEntries(Object.entries(isObject(value.context.attributes) ? value.context.attributes : {})
          .filter(([key,item]) => !["__proto__","constructor","prototype"].includes(key) && typeof item === "string"));
      }
      if (isObject(value.source)) binding.source = pick(value.source,["messageId","toolUseId","sourceId","sourcePath"]);
      if (this.hasLocator(binding.referenceId, binding)) this.bindings.set(bindingKey(binding), binding);
    }
    for (const [key, binding] of this.bindings) {
      if (!this.hasLocator(binding.referenceId, binding)) this.bindings.delete(key);
    }
  }

  hasLocator(referenceId, { identifierKind, identifier }) {
    return this.byLocator.get(locatorKey(identifierKind, identifier))?.has(referenceId) === true;
  }

  hasSourceBindings(source) {
    return [...this.bindings.values()].some((binding) => binding.source &&
      binding.source.messageId === source.messageId && binding.source.toolUseId === source.toolUseId &&
      binding.source.sourceId === source.sourceId &&
      binding.source.sourcePath === source.sourcePath);
  }

  bindingFor(occurrence) {
    const binding = this.bindings.get(bindingKey(occurrence));
    const reference = binding && this.references.get(binding.referenceId);
    if (!reference) return null;
    const context = binding.context || {};
    const result = {...reference,attributes:{...reference.attributes}};
    for (const key of ["accountScope","consolePartition",...(reference.scopeKind === "regional" ? ["regionId"] : [])]) {
      if (context[key] && reference[key] && context[key] !== reference[key]) return null;
      if (!result[key] && context[key]) result[key] = context[key];
    }
    for (const [key,value] of Object.entries(context.attributes || {})) {
      if (result.attributes[key] && result.attributes[key] !== value) return null;
      if (!result.attributes[key]) result.attributes[key] = value;
    }
    return result;
  }

  candidates(occurrence) {
    const ids = this.byLocator.get(locatorKey(occurrence.identifierKind, occurrence.identifier)) || [];
    return [...ids].map((id) => this.references.get(id)).filter((reference) => {
      // Explicit user context is not a session-wide observation. Another
      // occurrence must make its own choice until an independent fact arrives.
      if (isDeclaredResource(reference) && this.bindings.get(bindingKey(occurrence))?.referenceId !== reference.referenceId) return false;
      if (occurrence.resourceType && this.canonicalType(occurrence.resourceType) !== this.canonicalType(reference.resourceType)) {
        return false;
      }
      for (const key of ["accountScope", "consolePartition", ...(reference.scopeKind === "regional" ? ["regionId"] : [])]) {
        if (occurrence[key] && reference[key] && occurrence[key] !== reference[key]) return false;
      }
      for (const [key, value] of Object.entries(occurrence.attributes || {})) {
        if (reference.attributes[key] && reference.attributes[key] !== value) return false;
      }
      return true;
    });
  }

  /** Return only context proved by real resource facts in this visible turn. */
  observedContextFor(occurrence, reference = null) {
    const contexts = observedContexts(this, occurrence, reference);
    return contexts.size === 1 ? contexts.values().next().value : {};
  }

  /** Scope a recognized ID to one unambiguous cloud context proved in its visible turn. */
  contextFor(occurrence, reference = null) {
    const contexts = observedContexts(this, occurrence, reference);
    return contexts.size === 1 ? contexts.values().next().value : contexts.size ? {} : (this.accountContext || {});
  }

  applyDelta(payload) {
    if (!isObject(payload) || payload.schemaVersion !== 1 || !validRevision(payload.revision)) return "invalid";
    this.observedRevision = Math.max(this.observedRevision, payload.revision);
    if (payload.revision <= this.revision) return "stale";
    if (payload.baseRevision !== this.revision) {
      this.needsSnapshot = true;
      return "gap";
    }
    if (!Array.isArray(payload.upserts) || !Array.isArray(payload.removedReferenceIds)) return "invalid";
    const rows = payload.upserts.map(cleanReference);
    if (rows.some((row) => !row)) {
      this.needsSnapshot = true;
      return "invalid";
    }
    const changed = new Set();
    // Both old and new owners can lose/gain context even if their rendered IDs
    // differ from the updated fact. Do not invalidate unrelated transcript turns.
    const changedOwners = contextOwners([
      ...payload.removedReferenceIds.map((id) => this.references.get(id)),
      ...rows.map((row) => this.references.get(row.referenceId)), ...rows,
    ]);
    for (const id of payload.removedReferenceIds) this._remove(id, changed);
    for (const row of rows) this._upsert(row, changed);
    for (const binding of Array.isArray(payload.removedBindings) ? payload.removedBindings : []) {
      if (!isObject(binding) || !BINDING_FIELDS.every((key) => typeof binding[key] === "string")) continue;
      this.bindings.delete(bindingKey(binding));
      changed.add(locatorKey(binding.identifierKind,binding.identifier));
    }
    this._bindings(payload.bindings);
    for (const binding of payload.bindings || []) changed.add(locatorKey(binding.identifierKind, binding.identifier));
    this.revision = payload.revision;
    this.needsSnapshot = this.revision < this.observedRevision;
    this._notify(changed, false, changedOwners);
    return this.needsSnapshot ? "gap" : "applied";
  }

  /** Assemble all pages at one revision; never expose a mixed/partial snapshot. */
  async loadSnapshot(seed, fetchPage, isCurrent = () => true) {
    const generation = this.generation;
    let first = seed;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (generation !== this.generation || !isCurrent()) return false;
      try {
        let page = first || await fetchPage(null);
        const revision = page?.revision;
        if (page?.schemaVersion !== 1 || !validRevision(revision)) throw new Error("resource_snapshot_invalid");
        const rows = [], bindings = [], cursors = new Set();
        const persistenceStatus = page.persistenceStatus || "saved";
        for (;;) {
          if (generation !== this.generation || !isCurrent()) return false;
          if (page.schemaVersion !== 1 || page.revision !== revision || !Array.isArray(page.items)) {
            throw new Error("resource_revision_conflict");
          }
          rows.push(...page.items);
          bindings.push(...(page.bindings || []));
          if (!page.nextCursor) break;
          if (cursors.has(page.nextCursor)) throw new Error("resource_cursor_invalid");
          cursors.add(page.nextCursor);
          page = await fetchPage(page.nextCursor);
        }
        if (generation !== this.generation || !isCurrent()) return false;
        if (revision < this.revision || revision < this.observedRevision) throw new Error("resource_snapshot_outdated");
        const cleanRows = rows.map(cleanReference);
        if (cleanRows.some((row) => !row)) throw new Error("resource_snapshot_invalid");
        const changed = new Set(this.byLocator.keys());
        const changedOwners = contextOwners([...this.references.values(), ...cleanRows]);
        this.references.clear();
        this.byLocator.clear();
        this.bindings.clear();
        for (const row of cleanRows) this._upsert(row, changed);
        this._bindings(bindings);
        this.revision = revision;
        this.observedRevision = revision;
        this.persistenceStatus = persistenceStatus;
        this.needsSnapshot = false;
        this._notify(changed, true, changedOwners);
        return true;
      } catch (_error) {
        first = null;
      }
    }
    if (generation === this.generation && isCurrent()) this.needsSnapshot = true;
    return false;
  }

  refresh(fetchPage, isCurrent = () => true) {
    if (this._loading) return this._loading;
    const loading = this.loadSnapshot(null, fetchPage, isCurrent);
    this._loading = loading;
    loading.finally(() => { if (this._loading === loading) this._loading = null; });
    return loading;
  }
}
