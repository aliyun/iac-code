// Source-version encoding shared with web/resource_sources.py. No raw source is
// persisted here; the request sends only a digest and an appearance group.
import { resourceSpec } from "./resource_link_rules.js?v=resource-links-v82";

function canonical(value) {
  if (value === null || ["string", "boolean"].includes(typeof value)) return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    const bytes = new Uint8Array(8);
    new DataView(bytes.buffer).setFloat64(0,value,false);
    return ["number",[...bytes].map((byte) => byte.toString(16).padStart(2,"0")).join("")];
  }
  if (Array.isArray(value)) return ["array",value.map(canonical)];
  if (value && typeof value === "object") return ["object",Object.keys(value).sort().map((key) => [key,canonical(value[key])])];
  throw Error("resource_source_invalid");
}

export const canonicalSourceJson = (value) => JSON.stringify(canonical(value));

export function toolResourceSourceValue(tool) {
  let input = tool.input ?? null;
  if (typeof input === "string") {
    try {input = JSON.parse(input);} catch (_error) { /* Keep visible literal text. */ }
  }
  const progress = tool.stackProgress;
  const stackProgress = progress && typeof progress === "object" && !Array.isArray(progress) ? {
    StackId:progress.stackId ?? null,
    resources:(Array.isArray(progress.resources) ? progress.resources : []).filter((row) => row && typeof row === "object" && !Array.isArray(row))
      .map((row) => {
        const spec=resourceSpec(row.resource_type);
        const attributes=Object.fromEntries([spec?.identifierField,...(spec?.parentFields || [])].filter((field) =>
          field && typeof row.attributes?.[field] === "string").map((field) => [field,row.attributes[field]]));
        return {resourceType:row.resource_type ?? null,PhysicalResourceId:row.physical_resource_id ?? null,attributes};
      }),
  } : null;
  return {toolName:tool.toolName || "",input,stackProgress,results:(Array.isArray(tool.results) ? tool.results : []).filter((item) =>
    typeof item === "string" || (item && typeof item === "object" && !Array.isArray(item)))
    .map((item) => typeof item === "string" ? item : item.content ?? item.summary ?? item.result ?? "")};
}

export async function digestResourceSource(source, value) {
  let descriptor;
  if (source.messageId && !source.toolUseId && ["/text","/thinking"].includes(source.sourcePath)) {
    descriptor = {message_id:source.messageId,source_path:source.sourcePath};
  } else if (source.toolUseId && !source.messageId && source.sourcePath === "/tool") {
    descriptor = {tool_use_id:source.toolUseId,source_path:"/tool"};
  } else if (!source.messageId && !source.toolUseId && /^blocking:(permission|question|elicitation):.+$/.test(source.sourceId || "") &&
    /^\/(prompt|command|fields\/(0|[1-9][0-9]*)\/(label|description))$/.test(source.sourcePath || "")) {
    descriptor = {source_id:source.sourceId,source_path:source.sourcePath};
  } else if (!source.messageId && !source.toolUseId && /^session-error:[1-9][0-9]*:.+$/.test(source.sourceId || "") &&
    source.sourcePath === "/message") {
    descriptor = {source_id:source.sourceId,source_path:source.sourcePath};
  } else if (!source.messageId && !source.toolUseId && /^output-preview:[0-9a-f]{64}$/.test(source.sourceId || "") &&
    source.sourcePath === "/content") {
    descriptor = {source_id:source.sourceId,source_path:source.sourcePath};
  } else return null;
  const bytes = new TextEncoder().encode(canonicalSourceJson([descriptor,value]));
  const hash = await globalThis.crypto.subtle.digest("SHA-256",bytes);
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2,"0")).join("");
}

export function resourceOccurrenceGroup(occurrence) {
  return [occurrence.identifierKind,occurrence.identifier,occurrence.identifierField,occurrence.resourceType,
    occurrence.accountScope || null,occurrence.consolePartition || null,occurrence.regionId || null,
    Object.entries(occurrence.attributes || {}).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0),
    occurrence.semantic || null,occurrence.structuralScope || null];
}

export const resourceBindingSourceKey = (digest, group) => `source-v1:${digest}:${canonicalSourceJson(group)}`;
