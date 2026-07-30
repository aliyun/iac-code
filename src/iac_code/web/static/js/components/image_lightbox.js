import { t } from "../i18n.js?v=web-repl-ui-277";

// 全屏图片灯箱:单例浮层,挂在 document.body。composer 缩略图与消息内图片共用同一套。
// 点遮罩空白/×/Esc 关闭(点图片本身不关闭);自持 DOM,不进 render 扇出。
let overlay = null;
let imageEl = null;
let keyHandler = null;

function ensureOverlay() {
  if (overlay) {
    return overlay;
  }
  overlay = document.createElement("div");
  overlay.className = "image-lightbox";
  overlay.hidden = true;

  const figure = document.createElement("figure");
  figure.className = "image-lightbox-figure";
  imageEl = document.createElement("img");
  imageEl.className = "image-lightbox-img";
  imageEl.draggable = false;
  figure.append(imageEl);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "image-lightbox-close";
  close.setAttribute("aria-label", t("Close preview"));
  close.textContent = "×";

  overlay.append(figure, close);

  // 点遮罩空白处关闭(点图片本身冒泡到 overlay 前已被 figure 截断,故不误关)。
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      closeImageLightbox();
    }
  });
  close.addEventListener("click", closeImageLightbox);

  document.body.append(overlay);
  return overlay;
}

export function openImageLightbox({ src, alt } = {}) {
  const source = src === undefined || src === null ? "" : String(src);
  if (!source) {
    return;
  }
  if (typeof document === "undefined" || !document.body) {
    return;
  }
  ensureOverlay();
  imageEl.src = source;
  imageEl.alt = alt === undefined || alt === null ? "" : String(alt);
  overlay.hidden = false;
  if (!keyHandler) {
    keyHandler = (event) => {
      if (event.key === "Escape") {
        closeImageLightbox();
      }
    };
    document.addEventListener("keydown", keyHandler);
  }
}

export function closeImageLightbox() {
  if (overlay) {
    overlay.hidden = true;
  }
  if (imageEl) {
    imageEl.removeAttribute("src");
  }
  if (keyHandler) {
    document.removeEventListener("keydown", keyHandler);
    keyHandler = null;
  }
}
