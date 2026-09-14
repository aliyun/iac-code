import { stockLibrary, buildRenderPageSetup } from "@eraserlabs/diagrams/library";
import { stockNormalizers } from "@eraserlabs/diagrams/normalizers";
import "@eraserlabs/render/browser";
import { createResolver, prepareLibrary } from "@eraserlabs/resolve";

window.createIacEraserRenderer = async function createIacEraserRenderer(iconMap) {
  const resolver = await createResolver({
    library: stockLibrary,
    normalizers: stockNormalizers,
    iconLoader: async (name) => {
      const icon = iconMap[name];
      if (!icon) throw new Error(`Unknown local icon: ${name}`);
      return icon;
    },
  });
  window.__eraser.setup(buildRenderPageSetup(prepareLibrary(stockLibrary)));
  await window.__eraser.registerFonts({
    css:
      ':root{--font-clean:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif;--font-rough:var(--font-clean);--font-mono:ui-monospace,SFMono-Regular,Consolas,monospace}',
    faces: [],
  });
  return async (input) => {
    const resolved = await resolver.resolve(input);
    if (!resolved.ok) {
      const reason = resolved.errors?.map((item) => item.message || String(item)).join("; ") || "resolve failed";
      throw new Error(reason);
    }
    const layout = await window.__eraser.run(resolved);
    return { layout, warnings: resolved.warnings || [] };
  };
};
