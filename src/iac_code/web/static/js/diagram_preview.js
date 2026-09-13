import { renderArchitectureDiagram } from "./architecture_diagram.js?v=eraser-browser-v14";
import {
  indexTemplateFiles,
  shuffledOtherTemplatePaths,
} from "./diagram_preview_library.js?v=template-library-v1";
import { applyDomI18n, t } from "./i18n.js?v=web-repl-ui-277";
import { tokenFetch } from "./token_transport.js?v=token-transport-v4";

const MAX_TEMPLATE_BYTES = 2 * 1024 * 1024;
const CORPUS_URL = "/static/diagram-preview-corpus.json?v=visual-corpus-v1";
const input = document.querySelector('[data-diagram-preview="input"]');
const repositoryInput = document.querySelector('[data-diagram-preview="repository-input"]');
const repositoryStatus = document.querySelector('[data-diagram-preview="repository-status"]');
const corpusSelect = document.querySelector('[data-diagram-preview="corpus-select"]');
const randomButton = document.querySelector('[data-diagram-preview="random"]');
const dropzone = document.querySelector('[data-diagram-preview="dropzone"]');
const status = document.querySelector('[data-diagram-preview="status"]');
const result = document.querySelector('[data-diagram-preview="result"]');
const filename = document.querySelector('[data-diagram-preview="filename"]');
const summary = document.querySelector('[data-diagram-preview="summary"]');
const renderer = document.querySelector('[data-diagram-preview="renderer"]');
const canvas = document.querySelector('[data-diagram-preview="canvas"]');
const renderTarget = document.querySelector('[data-diagram-preview="render"]');
let intrinsicWidth = 0;
let corpusPaths = [];
let templateIndex = new Map();
let randomQueue = [];
let pendingCorpusPath = "";

applyDomI18n(document);

function rendererLabel() {
  const value = document.body.dataset.architectureDiagramRenderer === "mermaid" ? "Mermaid" : "Eraser";
  return t("Renderer: {renderer}", { renderer: value });
}

function syncRendererLabel() {
  renderer.textContent = rendererLabel();
}

function syncRenderWidth() {
  if (!intrinsicWidth || result.hidden) return;
  const available = Math.max(0, canvas.clientWidth - 2);
  renderTarget.style.width = `${Math.max(available, intrinsicWidth + 48)}px`;
}

function setStatus(message, { error = false } = {}) {
  status.textContent = message;
  status.classList.toggle("is-error", error);
}

function setRepositoryStatus(message, { error = false } = {}) {
  repositoryStatus.textContent = message;
  repositoryStatus.classList.toggle("is-error", error);
}

function populateCorpusSelect() {
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = t("Select a template");
  placeholder.selected = true;
  corpusSelect.replaceChildren(placeholder);
  const hasRepository = templateIndex.size > 0;
  let available = 0;
  corpusPaths.forEach((path, index) => {
    const option = document.createElement("option");
    option.value = path;
    option.textContent = `${String(index + 1).padStart(2, "0")} · ${path}`;
    option.disabled = hasRepository && !templateIndex.has(path);
    if (hasRepository && !option.disabled) available += 1;
    corpusSelect.append(option);
  });
  corpusSelect.disabled = !corpusPaths.length;
  randomButton.disabled = !shuffledOtherTemplatePaths(templateIndex, corpusPaths, () => 0).length;
  return available;
}

async function loadCorpus() {
  try {
    const response = await fetch(CORPUS_URL, { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("corpus request failed");
    const payload = await response.json();
    corpusPaths = Array.isArray(payload?.templates) ? payload.templates.filter((path) => typeof path === "string") : [];
    if (!corpusPaths.length) throw new Error("corpus is empty");
    populateCorpusSelect();
  } catch {
    corpusPaths = [];
    corpusSelect.disabled = true;
    setRepositoryStatus(t("The fixed template corpus could not be loaded."), { error: true });
  }
}

async function loadRepository(files) {
  templateIndex = indexTemplateFiles(files);
  randomQueue = [];
  if (!templateIndex.size) {
    populateCorpusSelect();
    setRepositoryStatus(t("No supported ROS templates were found in that folder."), { error: true });
    return;
  }
  await corpusPromise;
  const available = populateCorpusSelect();
  const templateCount = templateIndex.size;
  const summaryParams = { count: templateCount, available };
  setRepositoryStatus(
    templateCount === 1
      ? t("{count} template loaded; {available} of 30 corpus templates available.", summaryParams)
      : t("{count} templates loaded; {available} of 30 corpus templates available.", summaryParams),
  );
  if (pendingCorpusPath && templateIndex.has(pendingCorpusPath)) {
    const path = pendingCorpusPath;
    pendingCorpusPath = "";
    corpusSelect.value = path;
    previewFile(templateIndex.get(path), path);
  } else {
    pendingCorpusPath = "";
  }
}

async function responsePayload(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return { error: { message: t("The template could not be rendered.") } };
}

async function previewFile(file, displayName = file?.name) {
  if (!file) return;
  if (file.size > MAX_TEMPLATE_BYTES) {
    setStatus(t("The template must be 2 MB or smaller."), { error: true });
    return;
  }
  setStatus(t("Rendering {filename}…", { filename: displayName }));
  try {
    const content = await file.text();
    const response = await tokenFetch("/api/diagram-preview", {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content }),
    });
    const payload = await responsePayload(response);
    if (!response.ok) throw new Error(payload?.error?.message || t("The template could not be rendered."));
    filename.textContent = displayName || payload.filename || file.name;
    const viewCount = payload.views?.length || 1;
    summary.textContent = viewCount === 1
      ? t("{count} architecture view", { count: viewCount })
      : t("{count} architecture views", { count: viewCount });
    syncRendererLabel();
    result.hidden = false;
    intrinsicWidth = 0;
    renderTarget.style.width = "";
    await renderArchitectureDiagram(renderTarget, payload, {
      maxHeight: 2400,
      onSize(size) {
        intrinsicWidth = Math.max(0, Number(size?.width) || 0);
        syncRenderWidth();
      },
    });
    setStatus(t("Rendered {filename}", { filename: displayName || payload.filename || file.name }));
    result.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus(error instanceof Error ? error.message : t("The template could not be rendered."), { error: true });
  } finally {
    input.value = "";
  }
}

const corpusPromise = loadCorpus();
input.addEventListener("change", () => previewFile(input.files?.[0]));
repositoryInput.addEventListener("change", () => loadRepository(repositoryInput.files));
corpusSelect.addEventListener("change", () => {
  const path = corpusSelect.value;
  if (!path) return;
  const file = templateIndex.get(path);
  if (file) {
    previewFile(file, path);
    return;
  }
  pendingCorpusPath = path;
  setRepositoryStatus(t("Select ros-templates folder"));
  repositoryInput.click();
});
randomButton.addEventListener("click", () => {
  if (!randomQueue.length) randomQueue = shuffledOtherTemplatePaths(templateIndex, corpusPaths);
  const path = randomQueue.shift();
  if (!path) {
    setRepositoryStatus(t("No templates remain outside the fixed corpus."), { error: true });
    return;
  }
  corpusSelect.value = "";
  previewFile(templateIndex.get(path), path);
});
dropzone.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  input.click();
});
dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  dropzone.classList.add("is-dragging");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragging"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("is-dragging");
  previewFile(event.dataTransfer.files?.[0]);
});
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
window.addEventListener("focus", syncRendererLabel);
new ResizeObserver(syncRenderWidth).observe(canvas);
syncRendererLabel();
