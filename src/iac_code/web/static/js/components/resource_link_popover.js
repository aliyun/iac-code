import { t } from "../i18n.js?v=web-repl-ui-277";
import { isDeclaredResource } from "../resource_index.js?v=resource-references-v16";
import { literalIdentifier, resourceSpec, resolveResourceChoice, RESOURCE_LINK_RULES } from "../resource_link_rules.js?v=resource-links-v82";

let closeActive = null;
let permissionPending = false;
let syncActiveVisibility = null;

export function closeResourceLinkPopover() {
  closeActive?.();
}

export function setResourceLinkPermissionPending(pending) {
  permissionPending = Boolean(pending);
  syncActiveVisibility?.();
}

function queryMessage(reason) {
  if (["resource_query_credentials_required","resource_query_account_changed"].includes(reason))
    return t("Cloud credentials for this resource are unavailable or have changed.");
  if (["resource_query_permission_required","resource_query_permission_denied","resource_query_readonly_unverified"].includes(reason))
    return t("Resource lookup needs permission. Review the permission settings, then retry.");
  if (["resource_query_source_changed","resource_query_target_changed","resource_query_lifecycle_changed"].includes(reason))
    return t("Resource information changed. Reopen the link and try again.");
  return t("Resource lookup failed. Retry or enter the missing information.");
}

/** Local, explicit context entry. Cloud lookup/persisted choices use a separate callback. */
export function openResourceLinkPopover(anchor, occurrence, initial, {
  index, rules = RESOURCE_LINK_RULES, onChoose = null, isCurrent = () => anchor.isConnected, onInvalidate = () => () => {},
  currentChoice = null, persistChoice = null, queryAttributes = null, loadAccountContext = null,
  restoreFocus = () => anchor.isConnected && anchor.focus({preventScroll:true}),
} = {}) {
  closeActive?.();
  const doc = anchor.ownerDocument;
  const generation = index.generation;
  const dialog = doc.createElement("section");
  dialog.className = "resource-link-popover";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-label", t("Resource link"));
  const title = doc.createElement("strong");
  title.textContent = occurrence.displayText || occurrence.identifier;
  const close = doc.createElement("button");
  close.type = "button";
  close.className = "resource-link-popover-close";
  close.textContent = t("Close");
  const content = doc.createElement("div");
  const actions = doc.createElement("div");
  actions.className = "resource-link-popover-actions";
  dialog.append(title, close, content, actions);
  doc.body.append(dialog);
  const bounds = anchor.getBoundingClientRect();
  const position = () => {
    dialog.style.left = `${Math.max(8,Math.min(bounds.left,doc.documentElement.clientWidth-dialog.offsetWidth-8))}px`;
    dialog.style.top = `${Math.max(8,Math.min(bounds.bottom+4,doc.documentElement.clientHeight-dialog.offsetHeight-8))}px`;
  };
  doc.defaultView.addEventListener("resize",position);
  let disposed = false, saving = false, querying = false, loadingAccount = false;
  const syncVisibility = () => {
    // Give the existing permission panel the screen while it owns this user
    // decision. Keep the lookup alive and restore its result after approval.
    dialog.hidden = querying && permissionPending;
    if (!dialog.hidden) position();
  };
  syncActiveVisibility = syncVisibility;
  const accountContexts = new Map();
  let unsubscribeSource = () => {};
  const dismiss = () => {
    if (disposed) return;
    disposed = true; dialog.remove(); unsubscribe(); unsubscribeSource();
    doc.removeEventListener("keydown", keydown);
    doc.defaultView.removeEventListener("resize",position);
    if (closeActive === dismiss) closeActive = null;
    if (syncActiveVisibility === syncVisibility) syncActiveVisibility = null;
    restoreFocus();
  };
  const keydown = (event) => { if (event.key === "Escape") { event.stopPropagation(); dismiss(); } };
  const locator = JSON.stringify([occurrence.identifierKind, occurrence.identifier]);
  const unsubscribe = index.subscribe((change) => {
    if (index.generation !== generation || (!saving && !querying && !loadingAccount && change.changed.has(locator))) dismiss();
  });
  unsubscribeSource = onInvalidate(dismiss);
  closeActive = dismiss;
  close.addEventListener("click", dismiss);
  doc.addEventListener("keydown", keydown);
  function valid() {
    if (disposed || generation !== index.generation || !isCurrent()) { dismiss(); return false; }
    return true;
  }

  function candidateLabel(candidate) {
    return [candidate.resourceType, candidate.accountScope, candidate.regionId,
      ...Object.values(candidate.attributes || {}), candidate.status].filter(Boolean).join(" · ");
  }

  function show(reference, queryResult = null) {
    content.replaceChildren(); actions.replaceChildren();
    const spec = resourceSpec(reference.resourceType || occurrence.resourceType);
    const defaults = currentChoice && (!currentChoice.referenceId || currentChoice.referenceId === reference.referenceId)
      ? currentChoice : {};
    const base = {...occurrence, ...defaults, resourceType: spec?.resourceType, ...reference,
      identifier: reference.identifier || occurrence.identifier, attributes: {...defaults.attributes, ...reference.attributes}};
    const declared = isDeclaredResource(reference);
    const observed = index.references.get(reference.referenceId);
    // Editing user-provided context creates/reuses a declaration; it does not
    // rewrite the identity of an observed API object under its reference ID.
    if (declared) delete base.referenceId;
    const description = doc.createElement("p");
    description.textContent = initial.reason === "deleted_name" || reference.status === "DELETE_COMPLETE"
      ? t("This name belonged to a deleted resource. Confirm the current object before opening it.")
      : t("Complete resource context");
    content.append(description);
    if (reference.status === "DELETE_COMPLETE") {
      const resolution = resolveResourceChoice(occurrence, reference, index, rules);
      if (resolution?.replacements?.length) {
        const label = doc.createElement("p"); label.textContent = t("Choose a resource"); content.append(label);
        for (const candidate of resolution.replacements) {
          const button = doc.createElement("button"); button.type = "button";
          button.dataset.resourceReplacement = candidate.referenceId;
          button.textContent = candidateLabel(candidate);
          button.addEventListener("click", () => { if (valid()) { show(candidate); close.focus({preventScroll:true}); } });
          content.append(button);
        }
      }
    }
    const inputs = new Map();
    const fieldNames = ["accountScope", "consolePartition", ...(spec?.scopeKind === "regional" ? ["regionId"] : []), ...(spec?.parentFields || [])];
    for (const field of fieldNames) {
      const label = doc.createElement("label");
      const caption = doc.createElement("span");
      caption.textContent = field === "regionId" ? t("Region") : field === "accountScope" ? t("Account") : field;
      const input = doc.createElement(field === "consolePartition" || field === "accountScope" ? "select" : "input");
      if (input.tagName === "SELECT") {
        const values = field === "consolePartition" ? ["", "aliyun", "alibabacloud"]
          : ["", ...new Set([...index.references.values()].map((item) => item.accountScope).filter(Boolean).concat([...accountContexts.keys()]))];
        for (const value of values) {
          const option = doc.createElement("option");
          option.value = value; option.textContent = accountContexts.has(value)
            ? t("Configured credentials (account not verified)") : value || t("Select");
          input.append(option);
        }
      }
      input.value = base[field] || base.attributes?.[field] || "";
      // Existing fact identity fields are not editable under the same reference
      // ID. Choose another candidate to change account/region/parent identity.
      input.disabled = Boolean(!declared && (observed?.[field] || observed?.attributes?.[field]));
      input.name = field;
      inputs.set(field, input);
      label.append(caption, input); content.append(label);
    }
    const status = doc.createElement("p");
    status.setAttribute("role", "status");
    content.append(status);
    const accountLoad = doc.createElement("button");
    accountLoad.type = "button"; accountLoad.dataset.resourceAccount = "true";
    accountLoad.textContent = t("Use configured cloud credentials");
    accountLoad.hidden = !loadAccountContext || inputs.get("accountScope").disabled;
    actions.append(accountLoad);
    const target = doc.createElement("a");
    target.target = "_blank"; target.rel = "noopener noreferrer";
    target.textContent = t("Open console"); target.hidden = true;
    actions.append(target);
    const confirm = doc.createElement("button");
    confirm.type = "button"; confirm.textContent = t("Select"); confirm.hidden = !onChoose;
    actions.append(confirm);
    const lookup = doc.createElement("button");
    lookup.type = "button"; lookup.textContent = t("Load missing resource attributes");
    lookup.dataset.resourceQuery = "true";
    actions.append(lookup);
    const copy = doc.createElement("button");
    copy.type = "button";
    copy.textContent = occurrence.identifierKind === "name" ? t("Copy name") : t("Copy ID");
    copy.addEventListener("click", async () => {
      try { await doc.defaultView.navigator.clipboard.writeText(occurrence.displayText || occurrence.identifier); }
      catch (_error) { status.textContent = t("Copy failed"); }
    });
    actions.append(copy);
    const resolveSelection = () => {
      if (!valid()) return null;
      const selected = {...base, attributes: {...base.attributes}};
      for (const [key, input] of inputs) {
        const value = input.value.trim();
        if (literalIdentifier(value)) {
          if (["accountScope", "consolePartition", "regionId"].includes(key)) selected[key] = value;
          else selected.attributes[key] = value;
        } else if (["accountScope", "consolePartition", "regionId"].includes(key)) delete selected[key];
        else delete selected.attributes[key];
      }
      selected.accountContextId = accountContexts.get(selected.accountScope)?.accountContextId;
      const specific = [occurrence, ...(occurrence.alternatives || [])]
        .find((item) => item.resourceType === spec?.resourceType) || occurrence;
      return {selected, resolution: resolveResourceChoice(specific, selected, index, rules)};
    };
    const refresh = () => {
      const result = resolveSelection();
      if (!result) return;
      const {resolution} = result;
      target.hidden = resolution?.status !== "resolved";
      confirm.disabled = target.hidden || saving || querying || loadingAccount;
      accountLoad.disabled = saving || querying || loadingAccount;
      target.removeAttribute("href");
      if (resolution?.url) target.href = resolution.url;
      status.textContent = resolution?.status === "resolved" ? t("This choice is not saved yet.")
        : resolution?.status === "unsupported" ? t("This console route has not been verified.")
        : (resolution?.missing || []).join(", ");
      const known = index.references.get(reference.referenceId);
      // Account/region selections are not authority for cloud reads. A query
      // is offered only for missing locator attributes on an observed fact;
      // the backend independently proves the current source and actual target.
      lookup.hidden = !queryAttributes || !known || isDeclaredResource(known) ||
        !known.accountScope || !known.consolePartition || known.status === "DELETE_COMPLETE" ||
        !resolution?.missing?.some((field) => !["accountScope","consolePartition","regionId"].includes(field));
      lookup.disabled = saving || querying || loadingAccount;
      if (loadingAccount) status.textContent = t("Loading credential context...");
      else if (querying) status.textContent = t("Loading resource attributes...");
      else if (queryResult?.status === "attributes_loaded" && resolution?.status === "resolved")
        status.textContent = t("Resource attributes loaded. Choose a link to open the console.");
      syncVisibility();
    };
    accountLoad.addEventListener("click", async () => {
      if (!valid() || saving || querying || loadingAccount || accountLoad.hidden) return;
      const disabled = new Map([...inputs.values()].map((input) => [input,input.disabled]));
      for (const input of inputs.values()) input.disabled = true;
      loadingAccount = true; refresh();
      let result;
      try { result = await loadAccountContext(valid); }
      catch (_error) { result = {status:"unavailable"}; }
      finally { loadingAccount = false; }
      if (!valid()) return;
      for (const [input,wasDisabled] of disabled) input.disabled = wasDisabled;
      const choice = result?.status === "available" && result.choice;
      if (!choice) {
        refresh(); status.textContent = t("No cloud credential context is available. Configure credentials, then retry.");
        position(); return;
      }
      accountContexts.clear(); accountContexts.set(choice.accountScope,choice);
      const input = inputs.get("accountScope");
      // Select only in response to this explicit action. Never infer that the
      // configured credential belongs to a historical account or namespace.
      input.replaceChildren();
      const values = new Set(["",...[...index.references.values()].map((item) => item.accountScope).filter(Boolean),choice.accountScope]);
      for (const value of values) {
        const option = doc.createElement("option"); option.value = value;
        option.textContent = value === choice.accountScope ? t("Configured credentials (account not verified)") : value || t("Select");
        input.append(option);
      }
      input.value = choice.accountScope;
      if (choice.consolePartition && !inputs.get("consolePartition").disabled)
        inputs.get("consolePartition").value = choice.consolePartition;
      refresh(); input.focus({preventScroll:true});
    });
    lookup.addEventListener("click", async () => {
      if (!valid() || saving || querying || loadingAccount || lookup.hidden) return;
      querying = true;
      const disabled = new Map([...inputs.values()].map((input) => [input,input.disabled]));
      for (const input of inputs.values()) input.disabled = true;
      refresh();
      let result;
      try { result = await queryAttributes(reference.referenceId,valid); }
      catch (_error) { result = {status:"needs_context",reason:"resource_query_failed"}; }
      finally { querying = false; }
      if (!valid()) return;
      const updated = result?.status === "attributes_loaded" && index.references.get(result.referenceId);
      if (updated) {
        show(updated,result);
        const nextTarget = actions.querySelector('a:not([hidden])');
        (nextTarget || close).focus({preventScroll:true});
        return;
      }
      for (const [input,wasDisabled] of disabled) input.disabled = wasDisabled;
      refresh();
      status.textContent = queryMessage(result?.reason);
      position();
      lookup.focus({preventScroll:true});
    });
    const checkTarget = (event) => {
      const result = resolveSelection();
      if (!result || result.resolution?.url !== target.href || result.resolution?.status !== "resolved") event.preventDefault();
    };
    target.addEventListener("click", checkTarget);
    target.addEventListener("auxclick", checkTarget);
    target.addEventListener("contextmenu", checkTarget);
    confirm.addEventListener("click", async () => {
      if (saving || querying || loadingAccount) return;
      const result = resolveSelection();
      if (result?.resolution?.status !== "resolved" || onChoose?.(result.selected, result.resolution) !== true) return;
      if (!persistChoice) { dismiss(); return; }
      saving = true;
      confirm.disabled = true;
      const disabled = new Map([...inputs.values()].map((input) => [input,input.disabled]));
      for (const input of inputs.values()) input.disabled = true;
      let saved;
      try { saved = await persistChoice(result.selected); }
      catch (_error) { saved = {status:"not_saved"}; }
      finally { saving = false; }
      if (!valid()) return;
      if (saved?.status === "saved" && resolveSelection()?.resolution?.status === "resolved") { dismiss(); return; }
      for (const [input,wasDisabled] of disabled) input.disabled = wasDisabled;
      refresh();
      // Keep the local choice usable, but never acknowledge a failed write as
      // durable. The same explicit action can retry after a revision refresh.
      if (resolveSelection()?.resolution?.status === "resolved") status.textContent = t("This choice is not saved yet.");
    });
    for (const input of inputs.values()) input.addEventListener("input", refresh);
    refresh();
  }
  const candidates = initial.candidates || [];
  if (initial.status === "ambiguous" && candidates.length) {
    const label = doc.createElement("p"); label.textContent = t("Choose a resource"); content.append(label);
    for (const candidate of candidates) {
      const button = doc.createElement("button"); button.type = "button";
      button.textContent = candidateLabel(candidate);
      button.addEventListener("click", () => show(candidate)); content.append(button);
    }
  } else show(initial.reference || occurrence);
  position();
  close.focus({preventScroll: true});
  return dismiss;
}
