const TEMPLATE_EXTENSION = /\.(?:ya?ml|json)$/i;

export function repositoryRelativePath(file) {
  const rawPath = String(file?.webkitRelativePath || file?.name || "").replaceAll("\\", "/");
  const parts = rawPath.split("/").filter((part) => part && part !== ".");
  if (!parts.length || parts.includes("..")) return "";
  return parts.length > 1 ? parts.slice(1).join("/") : parts[0];
}

export function indexTemplateFiles(files) {
  const templates = new Map();
  for (const file of Array.from(files || [])) {
    const path = repositoryRelativePath(file);
    if (!path || !TEMPLATE_EXTENSION.test(path) || path.split("/").some((part) => part.startsWith("."))) {
      continue;
    }
    templates.set(path, file);
  }
  return templates;
}

export function shuffledOtherTemplatePaths(templateIndex, corpusPaths, random = Math.random) {
  const corpus = new Set(corpusPaths || []);
  const paths = Array.from(templateIndex?.keys?.() || [])
    .filter((path) => !corpus.has(path))
    .sort((left, right) => left.localeCompare(right));
  for (let index = paths.length - 1; index > 0; index -= 1) {
    const sample = Number(random());
    const swapIndex = Math.min(index, Math.max(0, Math.floor((Number.isFinite(sample) ? sample : 0) * (index + 1))));
    [paths[index], paths[swapIndex]] = [paths[swapIndex], paths[index]];
  }
  return paths;
}
