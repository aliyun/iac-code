import { t } from "./i18n.js?v=web-repl-ui-277";
import {
  base64UrlDecode,
  base64UrlEncode,
  chacha20poly1305Decrypt,
  chacha20poly1305Encrypt,
  concatBytes,
  hkdfSha256,
  toBytes,
} from "./vendor/token-crypto.js?v=1.0.0";

const VERSION = "v1";
const HKDF_INFO = new TextEncoder().encode("iac-code-web-token-v1");
const TOKEN_STORAGE_KEY = "iac-code:web-access-token";
const REPLAY_WINDOW_SIZE = 1024;
const decoder = new TextDecoder();
const encoder = new TextEncoder();

let currentSession = null;
let sessionPromise = null;

class TokenAuthenticationError extends Error {}

export function isTokenMode() {
  return typeof document !== "undefined" && document.body?.dataset?.tokenMode === "true";
}

function storedToken() {
  try {
    return window.sessionStorage?.getItem(TOKEN_STORAGE_KEY) || "";
  } catch (_error) {
    return "";
  }
}

function saveToken(token) {
  try {
    window.sessionStorage?.setItem(TOKEN_STORAGE_KEY, token);
  } catch (_error) {
    // The in-memory session remains usable when storage is unavailable.
  }
}

function clearToken() {
  try {
    window.sessionStorage?.removeItem(TOKEN_STORAGE_KEY);
  } catch (_error) {
    // Best effort only.
  }
}

function validTokenFormat(value) {
  if (!/^[A-Za-z0-9_-]{43,}$/.test(value)) return false;
  try {
    const decoded = base64UrlDecode(value);
    return decoded.length >= 32 && base64UrlEncode(decoded) === value;
  } catch (_error) {
    return false;
  }
}

function tokenPrompt(message = "") {
  return new Promise((resolve) => {
    const gate = document.createElement("div");
    gate.className = "token-access-gate";
    gate.innerHTML = `
      <section class="token-access-dialog" role="dialog" aria-modal="true" aria-labelledby="token-access-title">
        <h1 id="token-access-title"></h1>
        <p class="token-access-description"></p>
        <form class="token-access-form">
          <input class="token-access-input" type="password" autocomplete="off" spellcheck="false" />
          <output class="token-access-error" role="status"></output>
          <button class="token-access-submit" type="submit"></button>
        </form>
      </section>`;
    gate.querySelector("h1").textContent = t("Enter Web access token");
    gate.querySelector(".token-access-description").textContent = t(
      "Enter the Web access token shown on the deployment result page.",
    );
    gate.querySelector(".token-access-submit").textContent = t("Unlock");
    const input = gate.querySelector(".token-access-input");
    input.placeholder = t("Enter Web access token");
    const error = gate.querySelector(".token-access-error");
    error.textContent = message;
    gate.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (!validTokenFormat(value)) {
        error.textContent = t("Enter a valid access token.");
        input.focus();
        return;
      }
      gate.remove();
      resolve(value);
    });
    document.body.append(gate);
    input.focus();
  });
}

function nonce(prefix, sequence) {
  const output = new Uint8Array(12);
  output.set(prefix, 0);
  let value = BigInt(sequence);
  for (let index = 11; index >= 4; index -= 1) {
    output[index] = Number(value & 0xffn);
    value >>= 8n;
  }
  return output;
}

function aad(sessionId, direction, messageType, sequence) {
  return encoder.encode([VERSION, sessionId, direction, messageType, String(sequence)].join("\n"));
}

function nextRequestSequence(session) {
  session.requestSequence += 1;
  if (!Number.isSafeInteger(session.requestSequence)) throw new Error("request sequence exhausted");
  return session.requestSequence;
}

function acceptResponseSequence(session, sequence) {
  if (!Number.isSafeInteger(sequence) || sequence <= 0) throw new Error("invalid response sequence");
  const floor = Math.max(0, session.responseMaximum - REPLAY_WINDOW_SIZE + 1);
  if (sequence < floor || session.responseSeen.has(sequence)) throw new Error("replayed response");
  session.responseSeen.add(sequence);
  if (sequence > session.responseMaximum) {
    session.responseMaximum = sequence;
    const nextFloor = Math.max(0, sequence - REPLAY_WINDOW_SIZE + 1);
    session.responseSeen = new Set([...session.responseSeen].filter((value) => value >= nextFloor));
  }
}

function encryptEnvelope(session, messageType, plaintext) {
  const sequence = nextRequestSequence(session);
  const ciphertext = chacha20poly1305Encrypt(
    session.requestKey,
    nonce(session.requestNoncePrefix, sequence),
    toBytes(plaintext),
    aad(session.sessionId, "request", messageType, sequence),
  );
  return {
    sessionId: session.sessionId,
    sequence,
    type: messageType,
    ciphertext: base64UrlEncode(ciphertext),
  };
}

function decryptEnvelope(session, envelope, expectedType) {
  if (
    !envelope ||
    envelope.sessionId !== session.sessionId ||
    envelope.type !== expectedType ||
    !Number.isSafeInteger(envelope.sequence)
  ) {
    throw new Error("invalid encrypted response");
  }
  const plaintext = chacha20poly1305Decrypt(
    session.responseKey,
    nonce(session.responseNoncePrefix, envelope.sequence),
    base64UrlDecode(envelope.ciphertext),
    aad(session.sessionId, "response", expectedType, envelope.sequence),
  );
  acceptResponseSequence(session, envelope.sequence);
  return plaintext;
}

async function createSession(token) {
  const challengeResponse = await fetch("/api/token/challenge", {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: "{}",
  });
  if (!challengeResponse.ok) throw new Error(t("Unable to start an encrypted session."));
  const challenge = await challengeResponse.json();
  if (challenge.version !== VERSION) throw new Error(t("Unsupported encrypted transport version."));
  const keyMaterial = hkdfSha256(
    encoder.encode(token),
    base64UrlDecode(challenge.salt),
    HKDF_INFO,
    64,
  );
  const session = {
    sessionId: challenge.sessionId,
    requestKey: keyMaterial.slice(0, 32),
    responseKey: keyMaterial.slice(32),
    requestNoncePrefix: base64UrlDecode(challenge.requestNoncePrefix),
    responseNoncePrefix: base64UrlDecode(challenge.responseNoncePrefix),
    expiresAt: Number(challenge.expiresAt) * 1000,
    requestSequence: 0,
    responseMaximum: 0,
    responseSeen: new Set(),
    token,
  };
  const ping = encryptEnvelope(session, "ping", encoder.encode("ping"));
  const pingResponse = await fetch("/api/token/ping", {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(ping),
  });
  if (pingResponse.status === 401) throw new TokenAuthenticationError(t("The Web access token is incorrect."));
  if (!pingResponse.ok) throw new Error(t("Unable to start an encrypted session."));
  let pong = "";
  try {
    pong = decoder.decode(decryptEnvelope(session, await pingResponse.json(), "pong"));
  } catch (_error) {
    throw new TokenAuthenticationError(t("The Web access token is incorrect."));
  }
  if (pong !== "pong") throw new TokenAuthenticationError(t("The Web access token is incorrect."));
  return session;
}

async function establishSession() {
  let token = storedToken();
  let message = "";
  for (;;) {
    if (!validTokenFormat(token)) token = await tokenPrompt(message);
    try {
      const session = await createSession(token);
      saveToken(token);
      return session;
    } catch (error) {
      if (!(error instanceof TokenAuthenticationError)) throw error;
      clearToken();
      token = "";
      message = error instanceof Error ? error.message : t("The Web access token is incorrect.");
    }
  }
}

async function ensureSession() {
  if (currentSession && currentSession.expiresAt > Date.now()) return currentSession;
  currentSession = null;
  if (!sessionPromise) {
    sessionPromise = establishSession().finally(() => {
      sessionPromise = null;
    });
  }
  currentSession = await sessionPromise;
  return currentSession;
}

function requestTarget(url) {
  const target = new URL(url, window.location.href);
  if (target.origin !== window.location.origin || !target.pathname.startsWith("/api/")) {
    throw new Error("encrypted transport only supports same-origin API requests");
  }
  return `${target.pathname}${target.search}`;
}

function requestHeaders(options) {
  const headers = new Headers(options.headers || {});
  const result = {};
  for (const name of ["accept", "content-type", "last-event-id"]) {
    if (headers.has(name)) result[name] = headers.get(name);
  }
  return result;
}

async function requestBody(options) {
  if (options.body === undefined || options.body === null) return new Uint8Array();
  if (typeof options.body === "string") return encoder.encode(options.body);
  if (options.body instanceof Uint8Array) return options.body;
  if (options.body instanceof ArrayBuffer) return new Uint8Array(options.body);
  if (typeof Blob !== "undefined" && options.body instanceof Blob) return new Uint8Array(await options.body.arrayBuffer());
  throw new Error("unsupported encrypted request body");
}

async function encryptedRequest(url, options, stream, allowRetry) {
  const session = await ensureSession();
  const inner = {
    method: String(options.method || "GET").toUpperCase(),
    path: requestTarget(url),
    headers: requestHeaders(options),
    body: base64UrlEncode(await requestBody(options)),
  };
  const messageType = stream ? "stream" : "request";
  const envelope = encryptEnvelope(session, messageType, encoder.encode(JSON.stringify(inner)));
  const outerResponse = await fetch(stream ? "/api/token/stream" : "/api/token/request", {
    method: "POST",
    cache: "no-store",
    signal: options.signal,
    headers: {
      Accept: stream ? "application/x-ndjson" : "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(envelope),
  });
  if (outerResponse.status === 401 && allowRetry) {
    currentSession = null;
    return encryptedRequest(url, options, stream, false);
  }
  if (!outerResponse.ok) throw new Error(`Encrypted transport failed with ${outerResponse.status}`);
  return stream ? decodeStreamResponse(session, outerResponse) : decodeResponse(session, outerResponse);
}

async function decodeResponse(session, outerResponse) {
  const plaintext = decryptEnvelope(session, await outerResponse.json(), "response");
  const payload = JSON.parse(decoder.decode(plaintext));
  const body = [204, 205, 304].includes(payload.status) ? null : base64UrlDecode(payload.body);
  return new Response(body, {
    status: payload.status,
    headers: new Headers(payload.headers || []),
  });
}

async function* responseLines(body) {
  if (!body) return;
  const reader = body.getReader();
  const textDecoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += textDecoder.decode(value || new Uint8Array(), { stream: !done });
      let newline = buffer.indexOf("\n");
      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) yield JSON.parse(line);
        newline = buffer.indexOf("\n");
      }
      if (done) break;
    }
    if (buffer.trim()) yield JSON.parse(buffer.trim());
  } finally {
    try {
      await reader.cancel();
    } catch (_error) {
      // The response may already be complete.
    }
    reader.releaseLock();
  }
}

async function decodeStreamResponse(session, outerResponse) {
  const iterator = responseLines(outerResponse.body)[Symbol.asyncIterator]();
  const first = await iterator.next();
  if (first.done) throw new Error("encrypted stream ended before response metadata");
  const start = JSON.parse(decoder.decode(decryptEnvelope(session, first.value, "stream-start")));
  let ended = false;
  const body = new ReadableStream({
    async pull(controller) {
      if (ended) return;
      try {
        const item = await iterator.next();
        if (item.done) throw new Error("encrypted stream ended unexpectedly");
        if (item.value.type === "stream-end") {
          decryptEnvelope(session, item.value, "stream-end");
          ended = true;
          await iterator.return?.();
          controller.close();
          return;
        }
        const frame = JSON.parse(decoder.decode(decryptEnvelope(session, item.value, "stream-body")));
        controller.enqueue(base64UrlDecode(frame.body));
      } catch (error) {
        ended = true;
        await iterator.return?.();
        controller.error(error);
      }
    },
    async cancel() {
      ended = true;
      await iterator.return?.();
    },
  });
  return new Response(body, { status: start.status, headers: new Headers(start.headers || []) });
}

export function tokenFetch(url, options = {}, { stream = false } = {}) {
  if (!isTokenMode()) return fetch(url, options);
  return encryptedRequest(url, options, stream, true);
}

export function requestAuthorizationCode({ signal } = {}) {
  return new Promise((resolve, reject) => {
    const gate = document.createElement("div");
    gate.className = "token-access-gate oauth-code-gate";
    gate.innerHTML = `
      <section class="token-access-dialog oauth-code-dialog" role="dialog" aria-modal="true" aria-labelledby="oauth-code-title">
        <h1 id="oauth-code-title"></h1>
        <p class="token-access-description oauth-code-description"></p>
        <form class="token-access-form oauth-code-form">
          <input class="token-access-input oauth-code-input" type="text" autocomplete="off" spellcheck="false" />
          <output class="token-access-error oauth-code-error" role="status"></output>
          <div class="oauth-code-actions">
            <button class="oauth-code-cancel" type="button"></button>
            <button class="token-access-submit oauth-code-submit" type="submit"></button>
          </div>
        </form>
      </section>`;
    gate.querySelector("h1").textContent = t("Complete OAuth login");
    gate.querySelector(".oauth-code-description").textContent = t(
      "Paste the complete OAuth callback URL (recommended), or paste only the authorization code.",
    );
    const input = gate.querySelector(".oauth-code-input");
    const error = gate.querySelector(".oauth-code-error");
    const cancelButton = gate.querySelector(".oauth-code-cancel");
    input.placeholder = t("Paste callback URL or authorization code");
    cancelButton.textContent = t("Cancel");
    gate.querySelector(".oauth-code-submit").textContent = t("Complete login");

    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", cancel);
      gate.remove();
      callback();
    };
    const cancel = () => finish(() => reject(new DOMException(t("OAuth login cancelled."), "AbortError")));
    gate.querySelector("form").addEventListener("submit", (event) => {
      event.preventDefault();
      const submitted = input.value.trim();
      if (!submitted) {
        error.textContent = t("Authorization code is required.");
        input.focus();
        return;
      }
      finish(() => resolve(submitted));
    });
    cancelButton.addEventListener("click", cancel);
    if (signal?.aborted) {
      cancel();
      return;
    }
    signal?.addEventListener("abort", cancel, { once: true });
    document.body.append(gate);
    input.focus();
  });
}

export function bytesToObjectUrl(data, mediaType) {
  return URL.createObjectURL(new Blob([data], { type: mediaType || "application/octet-stream" }));
}

export { concatBytes };
