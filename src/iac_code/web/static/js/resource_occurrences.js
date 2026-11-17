import { RESOURCE_TYPES, canonicalResourceType, literalIdentifier, resourceIdentifier, resourceSpec, specsForField } from "./resource_link_rules.js?v=resource-links-v82";

const TYPE_KEYS = new Set(["Type", "ResourceType", "resource_type", "resourceType", "type"]);
const REGION_KEYS = new Set(["RegionId", "region_id", "regionId"]);
const LOCATOR_FIELDS = new Set(RESOURCE_TYPES.flatMap((spec) => [...spec.parentFields, ...spec.attributeFields]));
const PHYSICAL_FIELDS = new Set(["id", "physical_resource_id", "PhysicalResourceId"]);
const GENERIC_ID_FIELDS = new Set(["id", "resourceid", "identifier", "resourceidentifier", "标识", "完整标识", "资源标识"]);
// Keep generic name labels localized and presentation-oriented. A bare
// `name = ...` assignment is common in HCL/configuration and is not proof that
// the value denotes the already-observed cloud resource.
const GENERIC_NAME_FIELDS = new Set(["名称", "资源名称"]);
const WORD = /[A-Za-z0-9_:@./%+*${}<>-]+/g;
const NATURAL_NAMES = [
  ["ALIYUN::RAM::Role", /\bRAM\s*(?:role\b|角色)/giu],
  ["ALIYUN::RAM::User", /\bRAM\s*(?:user\b|用户)/giu],
  ["ALIYUN::OSS::Bucket", /\bOSS\s*(?:bucket\b|存储桶)/giu],
  ["ALIYUN::FC::Function", /\bFC\s*(?:function\b|函数)/giu],
];

function safeDisplayName(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 1024 &&
    !/[\x00-\x1f\x7f`<>$]|[;；,，\n\r\t]/u.test(value);
}

const DIRECT_API_CONTRACTS = new Map();
for (const item of globalThis.__IAC_RESOURCE_LINKS__?.directApiContracts || []) {
  if (!item || typeof item.product !== "string" || typeof item.action !== "string" ||
      !resourceSpec(item.resourceType)) continue;
  const key = `${item.product.toLowerCase()}\u0000${item.action.toLowerCase()}`;
  const current = DIRECT_API_CONTRACTS.get(key) || new Set();
  current.add(item.resourceType);
  DIRECT_API_CONTRACTS.set(key, current);
}

export function resourceContextFromObject(object, base = {}) {
  if (!object || typeof object !== "object" || Array.isArray(object)) return base;
  const context = {...base, attributes: {...base.attributes}};
  delete context.physicalName;
  for (const [key, value] of Object.entries(object)) {
    if (TYPE_KEYS.has(key) && resourceSpec(value)) context.resourceType = value;
    if (REGION_KEYS.has(key) && literalIdentifier(value)) context.regionId = value;
    if (LOCATOR_FIELDS.has(key) && literalIdentifier(value)) context.attributes[key] = value;
    // Do not accept credential/URL fields from arbitrary displayed objects.
  }
  const spec = resourceSpec(context.resourceType);
  if (spec?.identifierKind === "name") {
    // Only the current object's explicit locator field can explain an opaque
    // physical value. A same-name resource elsewhere in the session cannot.
    const names = new Set([object[spec.identifierField], object.ResourceAttributes?.[spec.identifierField],
      object.attributes?.[spec.identifierField]].filter(literalIdentifier));
    if (names.size === 1) context.physicalName = {resourceType:spec.resourceType,identifier:[...names][0]};
  }
  return context;
}

export function resourceToolContext(tool, {requestValues = false} = {}) {
  // Tool snapshots have appeared with all three protocol spellings over the
  // Web/A2A lifecycle. Use the same precedence as tool-card rendering so the
  // product/action contract is not silently lost when only ``args`` is kept.
  let input = tool.input ?? tool.args ?? tool.arguments;
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch (_error) { input = {}; }
  }
  const context = {toolUseId: tool.toolUseId};
  const toolName = String(tool.toolName || tool.name || tool.kind || "").toLowerCase();
  if (!(toolName === "aliyun_api" || toolName === "aliyun-api") || !input || typeof input !== "object") return context;
  const product = typeof input.product === "string" ? input.product.toLowerCase()
    : typeof tool.product === "string" ? tool.product.toLowerCase() : "";
  const action = typeof input.action === "string" ? input.action
    : typeof tool.action === "string" ? tool.action : "";
  const contracts = {
    ram: /Role/.test(action) ? "ALIYUN::RAM::Role" : /User/.test(action) ? "ALIYUN::RAM::User" : null,
    ecs: /NetworkInterface/.test(action) ? "ALIYUN::ECS::NetworkInterface"
      : /SecurityGroup/.test(action) ? "ALIYUN::ECS::SecurityGroup"
      : /Snapshot/.test(action) ? "ALIYUN::ECS::Snapshot"
      : /KeyPair/.test(action) ? "ALIYUN::ECS::SSHKeyPair"
      : /Image/.test(action) ? "ALIYUN::ECS::Image"
      : /Disk/.test(action) ? "ALIYUN::ECS::Disk"
      : /Instance/.test(action) ? "ALIYUN::ECS::Instance" : null,
    ros: /Stack/.test(action) ? "ALIYUN::ROS::Stack" : null,
  };
  if (contracts[product]) context.resourceType = contracts[product];
  const direct = DIRECT_API_CONTRACTS.get(`${product}\u0000${action.toLowerCase()}`);
  if (direct?.size === 1) context.resourceType = direct.values().next().value;
  // Request region is a hint for request values; per-response object regions
  // override it. Unknown account and console partition remain unknown.
  if (literalIdentifier(input.region_id)) context.requestRegionId = input.region_id;
  if (literalIdentifier(input.params?.RegionId)) context.requestRegionId = input.params.RegionId;
  if (requestValues && context.requestRegionId) context.regionId = context.requestRegionId;
  return context;
}

function* tokenize(text) {
  const tokens = [];
  const pattern = /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[\p{L}\p{N}_@./%+*-]+|[^\s]/gu;
  for (const match of text.matchAll(pattern)) {
    const raw = match[0];
    const quoted = (raw[0] === '"' || raw[0] === "'") && raw.at(-1) === raw[0] && raw.length > 1;
    let value = quoted ? raw.slice(1, -1) : raw;
    if (quoted && raw[0] === '"') {
      try { value = JSON.parse(raw); } catch (_error) { /* Invalid literals are not expanded. */ }
    }
    tokens.push({value, raw, quoted, start: match.index + (quoted ? 1 : 0), end: match.index + raw.length - (quoted ? 1 : 0)});
    if (tokens.length % 128 === 0) yield;
  }
  return tokens;
}

function structuredField(tokens, index) {
  const token = tokens[index];
  const suffix = new Map([
    ["ID", "Id"], ["Id", "Id"], ["ARN", "Arn"], ["Arn", "Arn"], ["Name", "Name"],
  ]).get(token.value);
  const prefix = tokens[index - 1];
  if (!suffix || prefix?.quoted || !/^[A-Za-z][A-Za-z0-9_]*$/.test(prefix?.value || "")) {
    return {field: token.value, keyStart: token.start};
  }
  return {field: `${prefix.value}${suffix}`, keyStart: prefix.start};
}

function* structuredPairs(text, base) {
  if (!text.includes(":") && !text.includes("=")) return [];
  // A complete Alibaba Cloud ARN is one locator, not a chain of unquoted
  // `key:value` pairs. Let the fact-backed token scanner handle it as a whole.
  if (/^acs:[^\s]+$/i.test(text)) return [];
  const tokens = yield* tokenize(text);
  const pairs = [];
  const root = {fields: {}, parent: null, type: base.resourceType, start: 0};
  const stack = [root];
  // Brace scopes also cover displayed HCL resource blocks. They never execute.
  for (let i = 0; i < tokens.length; i += 1) {
    if (i % 128 === 0) yield;
    const token = tokens[i];
    const current = stack.at(-1);
    if (!token.quoted && ["{", "["].includes(token.raw)) {
      const scope = {fields: {}, parent: current, type: null, start: token.start};
      if (token.raw === "{" && tokens[i - 1]?.raw === ":" &&
          ["ResourceAttributes", "attributes"].includes(tokens[i - 2]?.value)) {
        (current.locatorAttributes ||= {})[tokens[i - 2].value] = scope.fields;
      }
      if (tokens[i - 3]?.value === "resource" && resourceSpec(tokens[i - 2]?.value)) scope.type = tokens[i - 2].value;
      stack.push(scope);
    } else if (!token.quoted && ["}", "]"].includes(token.raw)) {
      if (stack.length > 1) stack.pop();
    } else if ([":", "="].includes(tokens[i + 1]?.raw) && tokens[i + 2]) {
      const value = tokens[i + 2];
      if (!value.quoted && ["{", "["].includes(value.raw)) continue;
      if (typeof value.value !== "string") continue;
      const {field, keyStart} = structuredField(tokens, i);
      current.fields[field] = value.value;
      pairs.push({field, token: value, scope: current, keyStart});
    }
  }
  // YAML has no braces. Use indentation and list-item boundaries, never a
  // region on the previous sibling row. Discard brace-parser YAML pairs first.
  if (!/[{}]/.test(text) && /(^|\n)\s*(?:-\s+)?[\w]+\s*:/u.test(text)) {
    pairs.length = 0;
    let root = {fields: {}, parent: null, type: base.resourceType, start: 0};
    const scopes = [{indent: -1, scope: root}];
    let offset = 0;
    for (const line of text.split("\n")) {
      // A YAML document separator starts a fresh object scope.  Without this
      // reset, successive documents share the previous root fields and a
      // later ResourceType/RegionId can incorrectly classify earlier values.
      if (/^\s*---\s*(?:#.*)?$/u.test(line)) {
        root = {fields: {}, parent: null, type: base.resourceType, start: offset};
        scopes.length = 0;
        scopes.push({indent: -1, scope: root});
        offset += line.length + 1;
        continue;
      }
      const match = /^(\s*)(-\s+)?([\w]+)\s*:\s*(.*)$/.exec(line);
      if (match) {
        const indent = match[1].replaceAll("\t", "    ").length;
        while (scopes.length > 1 && (scopes.at(-1).indent > indent || (match[2] && scopes.at(-1).indent >= indent))) scopes.pop();
        if (match[2] || scopes.at(-1).indent < indent) {
          const parent = scopes.at(-1);
          const scope = {fields: {}, parent: parent.scope, type: null, start: offset};
          if (!match[2] && ["ResourceAttributes", "attributes"].includes(parent.pendingField)) {
            (parent.scope.locatorAttributes ||= {})[parent.pendingField] = scope.fields;
          }
          parent.pendingField = null;
          scopes.push({indent: indent + (match[2]?.length || 0), scope});
        }
        const scope = scopes.at(-1).scope;
        scopes.at(-1).pendingField = match[4].trim() ? null : match[3];
        const valueOffset = line.lastIndexOf(match[4]);
        const value = (yield* tokenize(match[4]))[0];
        if (value && !value.quoted) {
          // Colons inside a plain scalar (ALIYUN::FC::Function, composite IDs)
          // are not mapping separators. Do not reduce them to the first token.
          const scalar = match[4].replace(/\s+#.*$/, "").trimEnd();
          const annotatedId = specsForField(match[3], base.resourceType)
            .some((spec) => spec.identifierKind === "id" && spec.pattern?.test(value.value)) &&
            /^(?:（[^（）]*）|\([^()]*\))$/u.test(scalar.slice(value.end).trim());
          value.value = annotatedId ? value.value : /^[^\s"'{}\[\],!&*|>#]+$/u.test(scalar) ? scalar : "";
          value.start = 0; value.end = annotatedId ? value.end : scalar.length;
        }
        if (value && literalIdentifier(value.value)) {
          value.start += offset + valueOffset;
          value.end += offset + valueOffset;
          scope.fields[match[3]] = value.value;
          pairs.push({field: match[3], token: value, scope, keyStart: offset + match[1].length});
        }
      }
      offset += line.length + 1;
      if (pairs.length % 128 === 0) yield;
    }
  }
  const result = [];
  for (const pair of pairs) {
    let inheritedType = pair.scope.type;
    for (let scope = pair.scope; scope && !inheritedType; scope = scope.parent) {
      inheritedType = scope.type || [...TYPE_KEYS].map((key) => scope.fields[key]).find((value) => resourceSpec(value));
    }
    result.push({...pair, context: resourceContextFromObject({...pair.scope.fields, ...pair.scope.locatorAttributes},
      {...base, resourceType: inheritedType || base.resourceType, structuralScope: `text-object:${pair.scope.start}`})});
    if (result.length % 128 === 0) yield;
  }
  return result;
}

function sentencePeriod(text, end) {
  return text[end] === "." && (end + 1 === text.length || /[\s,;!?，。；！？)\]）]/u.test(text[end + 1]));
}

function boundary(text, start, end, kind, proseEnd = false) {
  // Identifier punctuation is part of a larger literal, never an inner match.
  const continuation = kind === "id" ? /[A-Za-z0-9_@./%+*${}<>-]/ : /[\p{L}\p{N}_@./%+*${}<>-]/u;
  return !(start > 0 && continuation.test(text[start - 1])) &&
    (!(end < text.length && continuation.test(text[end])) || (proseEnd && sentencePeriod(text, end)));
}

function fieldSpecs(field, resourceType, identifier, index, context = {}) {
  const explicit = resourceSpec(resourceType);
  if (observedCmsName(explicit, field, identifier, index, context)) return [explicit];
  const specs = specsForField(field, resourceType);
  // A tool result field name describes only the response object; it is not a
  // global type assertion. For example, r-kvstore DescribeInstances exposes a
  // UserName property whose value can equal the Redis/Tair instance ID. Do not
  // reinterpret that value as a RAM user merely because RAM also has a
  // UserName identifier field. A same-tool resource fact (or an explicit type
  // for the tool) must prove the candidate first.
  if (context.toolUseId) {
    const explicit = resourceSpec(resourceType);
    return specs.filter((spec) =>
      (explicit && canonicalResourceType(spec.resourceType) === explicit.resourceType) ||
      index?.candidates({
        identifier, identifierKind: spec.identifierKind, resourceType: spec.resourceType,
      }).some((reference) => observedInContext(reference, context)));
  }
  if (specs.length < 2 || resourceSpec(resourceType)) return specs;
  // A shared field such as InstanceId or LoadBalancerId does not prove the
  // product. Prefer types already proved for this exact locator before making
  // additional context-only candidates; keep every proved type when the facts
  // themselves are ambiguous.
  const observed = specs.filter((spec) => index?.candidates({
    identifier, identifierKind: spec.identifierKind, resourceType: spec.resourceType,
  }).length);
  return observed.length ? observed : specs;
}

function observedCmsName(spec, field, identifier, index, context) {
  return spec?.resourceType === "ALIYUN::CMS::ContactGroup" && field === "Name" &&
    index?.candidates({identifier,identifierKind:"name",resourceType:spec.resourceType}).some(reference =>
      observedInContext(reference, context) && [reference,...(reference.aliases || [])].some(locator =>
        locator.identifier === identifier && locator.identifierKind === "name" && locator.identifierField === field));
}

function observedInContext(reference, context) {
  const owner = context.turnId ? ["turnId", context.turnId]
    : context.messageId ? ["messageId", context.messageId]
    : context.toolUseId ? ["toolUseId", context.toolUseId] : null;
  return Boolean(owner && (reference.sources || []).some((source) => source[owner[0]] === owner[1]));
}

function formalFieldNames(field) {
  if (typeof field !== "string" || !field.trim()) return [];
  const normalized = field.normalize("NFKC").trim();
  const names = [normalized];
  for (const match of normalized.matchAll(/[（(]\s*([A-Za-z][A-Za-z0-9_]*)\s*[)）]/g)) names.unshift(match[1]);
  return [...new Set(names)];
}

function postfixFormalField(text, end) {
  return /^\s*[（(]\s*([A-Za-z][A-Za-z0-9_]*)\s*[)）]/u.exec(text.slice(end))?.[1] || null;
}

function postfixFormalFields(text, end) {
  // A field tuple may include values after the field names, for example
  // `（GroupName；ReplicaGroupId pg-...）`.  We only need the first two
  // semicolon/comma-delimited field names to detect cross-resource collisions.
  const match = /^\s*[（(]\s*([A-Za-z][A-Za-z0-9_]*)(?:\s*[；;,，]\s*([A-Za-z][A-Za-z0-9_]*))+/u.exec(
    text.slice(end),
  );
  return match ? match.slice(1).filter(Boolean) : [];
}

function observedIdentityField(reference, identifier, field) {
  const fields = formalFieldNames(field);
  if (!fields.length) return null;
  for (const name of fields) {
    const locator = [reference, ...(reference.aliases || [])].find((item) =>
      item.identifier === identifier && item.identifierField === name);
    if (locator) return locator.identifierField;
    const spec = resourceSpec(reference.resourceType);
    if (spec?.attributeFields.includes(name) && reference.attributes?.[name] === identifier) return name;
    if (PHYSICAL_FIELDS.has(name) && reference.identifier === identifier) return "PhysicalResourceId";
  }
  return null;
}

function genericDisplayIdentityKind(field) {
  const fields = formalFieldNames(field);
  if (!fields.length) return null;
  // A parenthesized formal API field is authoritative.  For example,
  // “完整 ID（AccountId）” must not be treated as a generic resource ID.
  const specific = fields.find((name) => /^[A-Za-z][A-Za-z0-9_]*$/.test(name) &&
    !GENERIC_ID_FIELDS.has(name.toLowerCase()) && !GENERIC_NAME_FIELDS.has(name.toLowerCase()));
  if (specific) return null;
  for (const name of fields) {
    const normalized = name.normalize("NFKC").toLowerCase().replace(/[\s_:-]+/g, "");
    if (GENERIC_ID_FIELDS.has(normalized)) return "id";
    if (GENERIC_NAME_FIELDS.has(normalized)) return "name";
  }
  return null;
}

function nearbySectionProduct(text, start) {
  const prefix = text.slice(0, start);
  // Normal API transcripts often label a result as
  // “产品：cms | Action：Describe…”, rather than using a Markdown heading.
  // Treat that nearest label as the product scope so a generic Name cannot
  // borrow a same-spelled identifier from another product in the turn.
  const apiLabels = [
    ...prefix.matchAll(/(?:产品|product)\s*[:：]\s*([a-z][a-z0-9_-]*)\s*[|｜]\s*(?:Action|动作)\s*[:：]/giu),
    ...prefix.matchAll(/(?:产品|product)\s*[:：]\s*[^\n|｜]*?[（(]\s*([a-z][a-z0-9_-]*)\s*[）)]/giu),
  ];
  apiLabels.sort((a, b) => a.index - b.index);
  if (apiLabels.length) return apiLabels.at(-1)[1].toUpperCase();
  // Batch-style API headings commonly use a product code in a prose title,
  // e.g. “3. 云监控 CMS 联系组 — DescribeContactGroupList (cms, …)”.
  // Treat the nearest such heading as a product boundary for generic names;
  // otherwise a same-spelled Name can be claimed by an unrelated historical
  // rule (for example a CMS contact group by ActionTrail).
  const batchHeadings = [...prefix.matchAll(
    /(?:^|\n)\s*(?:\d+[.)]\s*)?[^\n]{0,160}\b([A-Z][A-Z0-9]{1,15})\b[^\n]*(?:—|–|-)\s*[A-Za-z][A-Za-z0-9]*\s*\([^\n]*\)/gu,
  )];
  if (batchHeadings.length) return batchHeadings.at(-1)[1].toUpperCase();
  const headings = [...prefix.matchAll(/(?:^|\n)\s*#{1,6}\s+([^\n]+)/gu)];
  const heading = headings.at(-1)?.[1] || "";
  const match = /\b([A-Z][A-Z0-9]{1,15})\b/u.exec(heading);
  return match?.[1] || null;
}

function resourceProduct(resourceType) {
  return canonicalResourceType(resourceType).split("::")[1]?.toUpperCase() || null;
}

/** Scan one logical text segment; offsets always address the unchanged source. */
export function scanResourceOccurrences(text, {index, context = {}, complete = true, field = null, trackCandidate = () => {}} = {}) {
  const work = scanResourceOccurrencesWork(text, {index, context, complete, field, trackCandidate});
  let step;
  do { step = work.next(); } while (!step.done);
  return step.value;
}

/** Cooperative version for large already-visible text; never truncates matches. */
export function* scanResourceOccurrencesWork(text, {index, context = {}, complete = true, field = null, trackCandidate = () => {}} = {}) {
  const result = [], seen = new Set();
  const urls = [];
  // Require a scheme boundary before the greedy scheme. Without it, a long
  // ordinary word retries the whole suffix at every character (quadratic).
  for (const match of text.matchAll(/(^|[^a-z0-9+.-])([a-z][a-z0-9+.-]*:\/\/[^\s<>"'`]+)/giu)) {
    urls.push([match.index + match[1].length, match.index + match[0].length]);
    if (urls.length % 128 === 0) yield;
  }
  function add(start, end, identifier, spec, semantic = null, local = context, identifierField = null,
    proseEnd = false, identifierKind = null) {
    if (observedCmsName(spec, semantic?.field, identifier, index, local)) {
      identifierField = "Name";
      identifierKind = "name";
    }
    const kind = identifierKind ||
      (identifierField === spec?.stableField || identifierField === "PhysicalResourceId" ? "id" : spec?.identifierKind);
    if (!spec || (!resourceIdentifier(identifier, spec, identifierField || spec.identifierField) &&
        !(kind === "name" && safeDisplayName(identifier))) ||
        (!complete && end === text.length)) return;
    let low = 0, high = urls.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (urls[mid][1] <= start) low = mid + 1;
      else high = mid;
    }
    if (urls[low] && urls[low][0] < end) return;
    if (!boundary(text, start, end, kind, proseEnd)) return;
    if (kind === "name" && !semantic) return;
    const key = `${start}:${end}:${spec.resourceType}:${kind}`;
    if (seen.has(key)) return;
    seen.add(key);
    result.push({...local, start, end, identifier, displayText: text.slice(start, end), identifierKind: kind,
      identifierField: identifierField || spec.identifierField, resourceType: spec.resourceType, semantic});
  }
  const pairs = yield* structuredPairs(text, context);
  const keyStarts = new Set(pairs.map((pair) => pair.keyStart));
  const valuePairs = new Map();
  for (const pair of pairs) {
    if (valuePairs.size % 128 === 0) yield;
    valuePairs.set(pair.token.start, pair);
    const specs = fieldSpecs(pair.field, pair.context.resourceType, pair.token.value, index, pair.context);
    for (const spec of specs) {
      // A generic Name/Id field in an untyped API transcript must stay within
      // the nearest product scope.  Otherwise a shared value can be linked
      // using the first historical rule with the same field (for example a
      // CMS template Name being misclassified as an ActionTrail Trail).
      const normalizedPairField = String(pair.field || "").normalize("NFKC")
        .replace(/[\s_:-]+/g, "").toLowerCase();
      if (!pair.context.resourceType && !pair.context.toolUseId &&
          ["name", "id", "physicalresourceid", "名称", "资源名称", "标识", "资源标识", "完整标识"].includes(normalizedPairField)) {
        const sectionProduct = pair.context.sectionProduct || nearbySectionProduct(text, pair.token.start);
        const candidateProduct = resourceProduct(spec.resourceType);
        // A structured API object can carry a product-specific formal ID
        // alongside a generic Name.  Use a unique formal-field contract as a
        // local hint even when the surrounding tool metadata was not retained
        // in a replayed transcript (for example TemplateId + Name from CMS).
        const scopeProducts = new Set(Object.entries(pair.scope.fields || {}).flatMap(([field, value]) => {
          const normalized = String(field || "").normalize("NFKC").replace(/[\s_:-]+/g, "").toLowerCase();
          if (["name", "id", "physicalresourceid", "名称", "资源名称", "标识", "资源标识", "完整标识"].includes(normalized) || !literalIdentifier(value)) return [];
          const formal = specsForField(field, null).filter((candidate) =>
            candidate.identifierField === field && candidate.identifierKind === "id");
          // Catalog-only resource types expose their route placeholders as
          // parent fields rather than explicit identity fields.  Those fields
          // are still a useful local discriminator when a replay omits tool
          // metadata (e.g. CMS TemplateId + Name).
          const parentContracts = RESOURCE_TYPES.filter((candidate) =>
            candidate.parentFields?.includes(field));
          const contracts = formal.length ? formal : parentContracts;
          return contracts.length === 1 ? [resourceProduct(contracts[0].resourceType)] : [];
        }).filter(Boolean));
        const objectProduct = scopeProducts.size === 1 ? [...scopeProducts][0] : null;
        // Do not resolve a generic Name/PhysicalResourceId to a catalog rule
        // unless the block supplies a product boundary.  These fields are
        // shared by many APIs and otherwise a historical ActionTrail Name
        // rule can claim an unrelated resource's display name.
        if (!sectionProduct && !objectProduct && ["Name", "PhysicalResourceId"].includes(spec.identifierField)) continue;
        const compatible = !sectionProduct || !candidateProduct ||
          sectionProduct === candidateProduct ||
          (sectionProduct === "API" && candidateProduct === "APIGATEWAY") ||
          (sectionProduct === "CR" && candidateProduct === "CR") ||
          (objectProduct && objectProduct === candidateProduct);
        if (!compatible) continue;
      }
      // ROS physical IDs of name resources are opaque until a real name exists.
      if (spec.identifierKind === "name" && PHYSICAL_FIELDS.has(pair.field) &&
          !String(pair.context.resourceType).startsWith("alicloud_") &&
          !(pair.context.physicalName?.resourceType === spec.resourceType &&
            pair.context.physicalName.identifier === pair.token.value)) continue;
      add(pair.token.start, pair.token.end, pair.token.value, spec,
        {kind: "field", field: pair.field, typed: Boolean(pair.context.resourceType)}, pair.context,
        pair.field === spec.stableField ? pair.field : null);
    }
  }
  // Some product APIs return names containing spaces in an unquoted display
  // field, e.g. `Name=ack_Default Contact Group；…`. The token parser keeps
  // only the first word, so recover the complete value only when an exact
  // observed name fact (and its product scope) proves that it is a resource.
  for (const match of text.matchAll(/(?:^|[;\n\t])(?:Name|名称)\s*=\s*([^；;\n]+)/gu)) {
    const rawValue = match[1].trim();
    if (!/\s/u.test(rawValue) || !safeDisplayName(rawValue)) continue;
    const start = match.index + match[0].lastIndexOf(rawValue);
    const sectionProduct = context.sectionProduct || nearbySectionProduct(text, start);
    const references = (index?.candidates({identifier: rawValue, identifierKind: "name"}) || [])
      .filter((reference) => {
        const product = resourceProduct(reference.resourceType);
        return (!sectionProduct || !product || sectionProduct === product) || observedInContext(reference, context);
      });
    for (const reference of references) {
      const spec = resourceSpec(reference.resourceType);
      if (!spec) continue;
      const alias = [reference, ...(reference.aliases || [])]
        .find((item) => item.identifier === rawValue && item.identifierKind === "name");
      add(start, start + rawValue.length, rawValue, spec, {kind: "observed_fact"}, context,
        alias?.identifierField || spec.identifierField, false, "name");
    }
  }
  const fieldAnnotation = field && /^([A-Za-z0-9_:@./%+*-]+)\s*[（(][^\n\r]*[）)]$/u.exec(text);
  const fieldFacts = (value, kind) => index?.candidates({...context, identifier:value, identifierKind:kind}) || [];
  const annotatedField = fieldAnnotation && !["id", "name"].some(kind => fieldFacts(text, kind).length) &&
    ["id", "name"].some(kind => fieldFacts(fieldAnnotation[1], kind).some(reference =>
      reference.evidence === "api_response" && observedInContext(reference, context) &&
      observedIdentityField(reference, fieldAnnotation[1], field)));
  const fieldValue = annotatedField ? fieldAnnotation[1] : text;
  if (field) {
    for (const spec of fieldSpecs(field, context.resourceType, fieldValue, index, context)) {
      if (!resourceIdentifier(fieldValue, spec, field) &&
          !(String(field).toLowerCase() === "name" && safeDisplayName(fieldValue))) continue;
      const named = context.physicalName?.resourceType === spec.resourceType && context.physicalName.identifier === fieldValue;
      add(0, fieldValue.length, fieldValue, spec, {kind: "field", field, typed: Boolean(context.resourceType)}, context,
        field === spec.stableField ? field : PHYSICAL_FIELDS.has(field) && spec.identifierKind === "name" && !named &&
          !String(context.resourceType).startsWith("alicloud_") ? "PhysicalResourceId" : null);
    }
  }
  for (const [type, pattern] of NATURAL_NAMES) {
    let count = 0;
    for (const match of text.matchAll(pattern)) {
      if (count++ % 128 === 0) yield;
      const tail = match.index + match[0].length;
      // Consume the connector separately: a missing/masked name must not make
      // regex backtracking reinterpret "is", "named" or "name" as the name.
      const connector = /^(?:\s+name\b|名称|名)?\s*(?:is\b\s*|named\b\s*|为|是|[:=：])?\s*[`"'“「]?/iu.exec(text.slice(tail))[0];
      const start = tail + connector.length;
      let value = /^[\p{L}\p{N}_@.-]+/u.exec(text.slice(start))?.[0];
      if (!value) continue;
      let proseEnd = false;
      if (/[^.]\.$/.test(value) && !/[`"'“「]$/.test(connector) && sentencePeriod(text,start+value.length-1) &&
          (complete || start+value.length < text.length)) {
        const trimmed = value.slice(0,-1);
        // Names may themselves contain dots: only an existing same-type fact
        // justifies the shorter reading, and an exact full-name fact wins.
        trackCandidate({identifierKind:"name",identifier:trimmed});
        const hasName = identifier => index?.candidates({identifier,identifierKind:"name"})
          .some(reference=>canonicalResourceType(reference.resourceType)===type);
        if (!hasName(value) && hasName(trimmed)) {value=trimmed;proseEnd=true;}
      }
      const naturalSpec = resourceSpec(type);
      const naturalScope = context.sectionProduct || nearbySectionProduct(text, match.index);
      if (!naturalScope && !context.resourceType && !context.toolUseId &&
          ["Name", "PhysicalResourceId"].includes(naturalSpec?.identifierField)) continue;
      add(start, start + value.length, value, naturalSpec, {kind: "explicit_type"}, context, null, proseEnd);
    }
  }
  // Names with spaces are often rendered in compact Markdown tables without a
  // `Name=` marker. Link an exact observed name only when the current block
  // establishes the product (or the fact belongs to this tool context), so
  // ordinary prose cannot acquire a cloud-resource link accidentally.
  let observedSpacedNames = 0;
  for (const reference of index?.references?.values() || []) {
    if (observedSpacedNames++ % 128 === 0) yield;
    const value = reference.identifier;
    const spec = resourceSpec(reference.resourceType);
    const cmsPhysicalName = spec?.resourceType === "ALIYUN::CMS::ContactGroup" &&
      resourceIdentifier(value, spec, reference.identifierField);
    if ((reference.identifierKind !== "name" && !cmsPhysicalName) || !/\s/u.test(value) || !safeDisplayName(value)) continue;
    if (cmsPhysicalName && context.resourceType &&
        canonicalResourceType(context.resourceType) !== spec.resourceType) continue;
    const sectionProduct = context.sectionProduct || nearbySectionProduct(text, 0);
    const product = resourceProduct(reference.resourceType);
    if (!observedInContext(reference, context) && (!sectionProduct || !product || sectionProduct !== product)) continue;
    let start = text.indexOf(value);
    while (start >= 0) {
      if (spec && boundary(text, start, start + value.length, "name") &&
          !seen.has(`${start}:${start + value.length}:${spec.resourceType}:name`)) {
        add(start, start + value.length, value, spec, {kind: "observed_fact"}, context, reference.identifierField,
          false, reference.identifierKind);
      }
      start = text.indexOf(value, start + value.length);
      if (observedSpacedNames++ % 128 === 0) yield;
    }
  }
  // Literal Alibaba CLI arguments: no variable/command expansion or execution.
  const command = /\baliyun\s+(\w+)\s+(\w+)([^\n]*)/giu;
  let commandCount = 0;
  for (const match of text.matchAll(command)) {
    if (commandCount++ % 32 === 0) yield;
    const args = [...match[3].matchAll(/--([A-Za-z][\w-]*)\s+(?:"([^"\n]*)"|'([^'\n]*)'|([^\s]+))/g)];
    const values = Object.fromEntries(args.map((arg) => [arg[1], arg[2] ?? arg[3] ?? arg[4]]));
    const commandContext = resourceToolContext({toolName: "aliyun_api", input: {product: match[1], action: match[2], params: values}}, {requestValues:true});
    if (literalIdentifier(values.region)) commandContext.regionId = values.region;
    for (const arg of args) {
      const value = arg[2] ?? arg[3] ?? arg[4];
      const start = match.index + match[0].indexOf(match[3]) + arg.index + arg[0].lastIndexOf(value);
      for (const spec of fieldSpecs(arg[1], commandContext.resourceType, value, index, commandContext)) {
        add(start, start + value.length, value, spec, {kind: "command_field", field: arg[1]}, {...context, ...commandContext});
      }
    }
  }
  let words = 0;
  for (const match of text.matchAll(WORD)) {
    if (words++ % 128 === 0) yield;
    let value = match[0];
    const start = match.index;
    let end = start + value.length, proseEnd = false;
    // A sentence-final period is not part of an alphanumeric/hyphen cloud ID.
    // Keep structured values, quoted literals, composite IDs, and ellipses exact.
    // The complete value wins if a supported resource really owns that spelling.
    if (/^[A-Za-z0-9-]+\.$/.test(value) && !field && !valuePairs.has(start) &&
        !/["'`]/.test(text[start - 1] || "") && sentencePeriod(text, end - 1) &&
        (complete || end < text.length) && !index?.candidates({identifier:value,identifierKind:"id"}).length) {
      const trimmed = value.slice(0,-1);
      if (RESOURCE_TYPES.some(spec=>spec.pattern?.test(trimmed)) ||
          index?.candidates({identifier:trimmed,identifierKind:"id"}).length) {
        value = trimmed; end -= 1; proseEnd = true;
      }
    }
    if (!literalIdentifier(value)) continue;
    trackCandidate({identifierKind: "id", identifier: value});
    trackCandidate({identifierKind: "name", identifier: value});
    if (keyStarts.has(start)) continue;
    const pair = valuePairs.get(start);
    const local = pair?.context || context;
    const patternRecognized = RESOURCE_TYPES.some((spec) => spec.pattern?.test(value));
    const contextualField = pair?.field || field || local.field;
    const postfixField = postfixFormalField(text, end);
    const contextualFormal = formalFieldNames(contextualField)
      .some((name) => /^[A-Za-z][A-Za-z0-9_]*$/.test(name));
    // A localized display heading such as “完整标识” or “名称” does not
    // describe the API value.  Prefer an immediately-following formal field
    // marker (`value`（ServerGroupId）), while retaining an explicit formal
    // context such as “完整 ID（AccountId）”.
    const visibleField = contextualFormal ? contextualField : postfixField || contextualField;
    const explicitPhysicalField = formalFieldNames(visibleField).some((name) => PHYSICAL_FIELDS.has(name));
    const explicitFormalField = formalFieldNames(visibleField)
      .some((name) => /^[A-Za-z][A-Za-z0-9_]*$/.test(name));
    const genericIdentityKind = genericDisplayIdentityKind(visibleField);
    const genericIdField = genericIdentityKind === "id";
    const genericNameField = genericIdentityKind === "name";
    const ambiguousShortNumber = /^\d{1,2}$/.test(value);
    const known = (index?.candidates({identifier: value, identifierKind: "id"}) || []).filter((reference) => {
      // Opaque IDs and name-like identifiers have no syntax that proves they
      // are resources.  A fact from another turn must not claim matching
      // ordinary prose in this owner (for example an OTS instance named
      // "fvt-ros" or "ros-ut").  A stable ID pattern remains recognizable
      // across turns and may use an observed non-pattern type (for example an
      // ENS i-* ID) to correct the prefix default.  A same-owner API fact
      // proves either kind.
      const identityField = observedIdentityField(reference, value, visibleField);
      return (!ambiguousShortNumber || Boolean(identityField) || genericIdField) &&
        (!explicitFormalField || Boolean(identityField) || explicitPhysicalField || genericIdField ||
          Boolean(local.toolUseId && observedInContext(reference, local))) &&
        (observedInContext(reference, local) || patternRecognized || explicitPhysicalField || genericIdField ||
          Boolean(identityField));
    });
    const specs = new Map(known.map((reference) => [canonicalResourceType(reference.resourceType), resourceSpec(reference.resourceType)]));
    for (const spec of RESOURCE_TYPES) if (spec.pattern?.test(value)) specs.set(spec.resourceType, spec);
    for (const spec of specs.values()) {
      // A known stable ID can point through the same object's name without
      // globally turning arbitrary numeric words into role-name occurrences.
      const alias = known.flatMap((reference) => [reference, ...(reference.aliases || [])])
        .find((item) => item.identifier === value && item.identifierKind === "id");
      const identityField = known.map((reference) => observedIdentityField(reference, value, visibleField)).find(Boolean);
      add(start, end, value, spec, identityField ? {kind:"field", field:identityField} : null, local,
        alias?.identifierField || identityField || spec?.identifierField, proseEnd);
    }
    // Opaque names (for example a short ECS key-pair name such as "ha") have
    // no safe global pattern. An exact resource fact from this visible owner is
    // sufficient semantic proof without claiming the same word in other turns.
    const knownNames = (index?.candidates({identifier: value, identifierKind: "name"}) || [])
      .filter((reference) => {
        const identityField = observedIdentityField(reference, value, visibleField);
        const sectionProduct = local.sectionProduct || nearbySectionProduct(text, start);
        const candidateProduct = resourceProduct(reference.resourceType);
        const sectionCompatible = !sectionProduct || !candidateProduct ||
          sectionProduct === candidateProduct ||
          (sectionProduct === "API" && candidateProduct === "APIGATEWAY") ||
          (sectionProduct === "CR" && candidateProduct === "CR");
        const nearbyFields = postfixFormalFields(text, end);
        const knownFields = new Set([
          reference.identifierField,
          ...(reference.aliases || []).map((item) => item.identifierField),
          ...Object.keys(reference.attributes || {}),
        ].filter(Boolean));
        // A name followed by several formal fields can describe a different
        // product's object (for example EBS GroupName + ReplicaGroupId). Do
        // not let the first matching historical name (such as a RAM group)
        // claim it unless every field in the local tuple belongs to that
        // candidate.
        const fieldsCompatible = nearbyFields.length < 2 || nearbyFields.every((item) => knownFields.has(item));
        const matchedName = [reference, ...(reference.aliases || [])]
          .find((item) => item.identifier === value && item.identifierKind === "name");
        const effectiveNameField = matchedName?.identifierField || reference.identifierField;
        if (!sectionProduct && !observedInContext(reference, local) &&
            ["Name", "PhysicalResourceId"].includes(effectiveNameField)) return false;
        // A generic Name is only safe when this visible block establishes the
        // product (or the exact fact belongs to this message/tool). Without
        // that boundary, an opaque name can be claimed by an unrelated
        // product's historical rule (for example a VPC router name by
        // ActionTrail).
        const genericNameProven = !genericNameField || Boolean(sectionProduct) || observedInContext(reference, local) ||
          !["Name", "PhysicalResourceId"].includes(effectiveNameField);
        return sectionCompatible && fieldsCompatible && genericNameProven &&
        (observedInContext(reference, local) || Boolean(identityField) || genericNameField) &&
        (!explicitFormalField || Boolean(identityField) || explicitPhysicalField || genericNameField ||
            Boolean(local.toolUseId && observedInContext(reference, local)));
      });
    for (const reference of knownNames) {
      const spec = resourceSpec(reference.resourceType);
      const alias = [reference, ...(reference.aliases || [])]
        .find((item) => item.identifier === value && item.identifierKind === "name");
      add(start, end, value, spec, {kind: "observed_fact"}, local,
        alias?.identifierField || spec?.identifierField, proseEnd, alias?.identifierKind);
    }
  }
  // The general token scanner deliberately stays ASCII so a sentence such as
  // “实例i-...已创建” does not become one giant token and hide the embedded ID.
  // Opaque Unicode names have no safe global pattern, so match only complete
  // names already observed by the same visible owner.
  let observedUnicodeNames = 0;
  for (const reference of index?.references?.values?.() || []) {
    if (observedUnicodeNames++ % 128 === 0) yield;
    if (!observedInContext(reference, context)) continue;
    for (const locator of [reference, ...(reference.aliases || [])]) {
      const value = locator.identifier;
      if (!literalIdentifier(value) || /^[\x00-\x7f]*$/.test(value)) continue;
      let start = text.indexOf(value);
      while (start >= 0) {
        const end = start + value.length;
        const spec = resourceSpec(reference.resourceType);
        if (boundary(text, start, end, "name")) {
          add(start, end, value, spec, {kind: "observed_fact"}, context, locator.identifierField,
            undefined, locator.identifierKind);
        }
        start = text.indexOf(value, end);
        if (observedUnicodeNames++ % 128 === 0) yield;
      }
    }
  }
  // Longest complete spans win. Same-span type alternatives are retained for
  // explicit ambiguity; the DOM layer renders one action, not nested wrappers.
  result.sort((a, b) => a.start - b.start || b.end - a.end || Number(Boolean(b.semantic)) - Number(Boolean(a.semantic)));
  const occurrences = [];
  for (let i = 0; i < result.length; i += 1) {
    if (i % 128 === 0) yield;
    const item = result[i];
    const previous = occurrences.at(-1);
    if (previous && item.start < previous.end) {
      if (item.start === previous.start && item.end === previous.end && item.resourceType !== previous.resourceType) {
        (previous.alternatives ||= []).push(item);
      }
      continue;
    }
    occurrences.push(item);
  }
  return occurrences;
}
