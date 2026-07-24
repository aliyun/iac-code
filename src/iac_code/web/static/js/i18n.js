// Runtime i18n for the web session UI.
// English is the base: t() returns the msgid when no translation exists.
// The active-language catalog is injected as window.__IAC_I18N__ by the server.

const store = (typeof window !== "undefined" && window.__IAC_I18N__) || { lang: "en", messages: {} };

export function currentLang() {
  return store.lang || "en";
}

export function t(msgid, params) {
  let out = (store.messages && store.messages[msgid]) || msgid;
  if (params) {
    for (const key of Object.keys(params)) {
      out = out.split("{" + key + "}").join(String(params[key]));
    }
  }
  return out;
}

export function applyDomI18n(root) {
  const scope = root || (typeof document !== "undefined" ? document : null);
  if (!scope || !scope.querySelectorAll) return;
  for (const el of scope.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.getAttribute("data-i18n"));
  }
  for (const el of scope.querySelectorAll("[data-i18n-attr]")) {
    for (const part of el.getAttribute("data-i18n-attr").split(";")) {
      const idx = part.indexOf(":");
      if (idx > 0) {
        const attr = part.slice(0, idx).trim();
        const key = part.slice(idx + 1).trim();
        if (attr && key) el.setAttribute(attr, t(key));
      }
    }
  }
}
