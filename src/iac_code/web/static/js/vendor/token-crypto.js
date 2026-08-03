// Fixed protocol implementation for iac-code Web token transport.
// Algorithms and byte layout follow RFC 2104, RFC 5869, RFC 8439 and FIPS 180-4.

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const utf8 = new TextEncoder();

function bytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  return utf8.encode(String(value));
}

function concat(...parts) {
  const values = parts.map(bytes);
  const output = new Uint8Array(values.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of values) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function rotateRight(value, shift) {
  return ((value >>> shift) | (value << (32 - shift))) >>> 0;
}

export function sha256(input) {
  const message = bytes(input);
  const paddedLength = Math.ceil((message.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(message);
  padded[message.length] = 0x80;
  let bitLength = BigInt(message.length) * 8n;
  for (let index = 0; index < 8; index += 1) {
    padded[paddedLength - 1 - index] = Number(bitLength & 0xffn);
    bitLength >>= 8n;
  }

  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const words = new Uint32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      const start = offset + index * 4;
      words[index] = (
        (padded[start] << 24) |
        (padded[start + 1] << 16) |
        (padded[start + 2] << 8) |
        padded[start + 3]
      ) >>> 0;
    }
    for (let index = 16; index < 64; index += 1) {
      const left = words[index - 15];
      const right = words[index - 2];
      const sigma0 = rotateRight(left, 7) ^ rotateRight(left, 18) ^ (left >>> 3);
      const sigma1 = rotateRight(right, 17) ^ rotateRight(right, 19) ^ (right >>> 10);
      words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + sum1 + choice + SHA256_K[index] + words[index]) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    state[0] = (state[0] + a) >>> 0;
    state[1] = (state[1] + b) >>> 0;
    state[2] = (state[2] + c) >>> 0;
    state[3] = (state[3] + d) >>> 0;
    state[4] = (state[4] + e) >>> 0;
    state[5] = (state[5] + f) >>> 0;
    state[6] = (state[6] + g) >>> 0;
    state[7] = (state[7] + h) >>> 0;
  }
  const output = new Uint8Array(32);
  state.forEach((value, index) => {
    output[index * 4] = value >>> 24;
    output[index * 4 + 1] = value >>> 16;
    output[index * 4 + 2] = value >>> 8;
    output[index * 4 + 3] = value;
  });
  return output;
}

export function hmacSha256(keyInput, messageInput) {
  let key = bytes(keyInput);
  if (key.length > 64) key = sha256(key);
  const innerPad = new Uint8Array(64).fill(0x36);
  const outerPad = new Uint8Array(64).fill(0x5c);
  for (let index = 0; index < key.length; index += 1) {
    innerPad[index] ^= key[index];
    outerPad[index] ^= key[index];
  }
  return sha256(concat(outerPad, sha256(concat(innerPad, bytes(messageInput)))));
}

export function hkdfSha256(ikm, salt, info, length) {
  if (!Number.isInteger(length) || length < 1 || length > 8160) throw new Error("invalid HKDF length");
  const prk = hmacSha256(bytes(salt), bytes(ikm));
  let previous = new Uint8Array();
  let output = new Uint8Array();
  for (let counter = 1; output.length < length; counter += 1) {
    previous = hmacSha256(prk, concat(previous, bytes(info), new Uint8Array([counter])));
    output = concat(output, previous);
  }
  return output.slice(0, length);
}

function read32le(input, offset) {
  return (
    input[offset] |
    (input[offset + 1] << 8) |
    (input[offset + 2] << 16) |
    (input[offset + 3] << 24)
  ) >>> 0;
}

function write32le(output, offset, value) {
  output[offset] = value;
  output[offset + 1] = value >>> 8;
  output[offset + 2] = value >>> 16;
  output[offset + 3] = value >>> 24;
}

function rotateLeft(value, shift) {
  return ((value << shift) | (value >>> (32 - shift))) >>> 0;
}

function quarterRound(state, a, b, c, d) {
  state[a] = (state[a] + state[b]) >>> 0;
  state[d] = rotateLeft(state[d] ^ state[a], 16);
  state[c] = (state[c] + state[d]) >>> 0;
  state[b] = rotateLeft(state[b] ^ state[c], 12);
  state[a] = (state[a] + state[b]) >>> 0;
  state[d] = rotateLeft(state[d] ^ state[a], 8);
  state[c] = (state[c] + state[d]) >>> 0;
  state[b] = rotateLeft(state[b] ^ state[c], 7);
}

function chachaBlock(keyInput, counter, nonceInput) {
  const key = bytes(keyInput);
  const nonce = bytes(nonceInput);
  if (key.length !== 32 || nonce.length !== 12) throw new Error("invalid ChaCha20 key or nonce");
  const initial = new Uint32Array(16);
  initial.set([0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]);
  for (let index = 0; index < 8; index += 1) initial[index + 4] = read32le(key, index * 4);
  initial[12] = counter >>> 0;
  initial[13] = read32le(nonce, 0);
  initial[14] = read32le(nonce, 4);
  initial[15] = read32le(nonce, 8);
  const state = new Uint32Array(initial);
  for (let round = 0; round < 10; round += 1) {
    quarterRound(state, 0, 4, 8, 12);
    quarterRound(state, 1, 5, 9, 13);
    quarterRound(state, 2, 6, 10, 14);
    quarterRound(state, 3, 7, 11, 15);
    quarterRound(state, 0, 5, 10, 15);
    quarterRound(state, 1, 6, 11, 12);
    quarterRound(state, 2, 7, 8, 13);
    quarterRound(state, 3, 4, 9, 14);
  }
  const output = new Uint8Array(64);
  for (let index = 0; index < 16; index += 1) write32le(output, index * 4, (state[index] + initial[index]) >>> 0);
  return output;
}

function chachaXor(key, nonce, input, initialCounter = 1) {
  const source = bytes(input);
  const output = new Uint8Array(source.length);
  for (let offset = 0, counter = initialCounter; offset < source.length; offset += 64, counter += 1) {
    if (counter > 0xffffffff) throw new Error("ChaCha20 counter exhausted");
    const block = chachaBlock(key, counter, nonce);
    const size = Math.min(64, source.length - offset);
    for (let index = 0; index < size; index += 1) output[offset + index] = source[offset + index] ^ block[index];
  }
  return output;
}

function littleEndianBigInt(input) {
  let value = 0n;
  const source = bytes(input);
  for (let index = source.length - 1; index >= 0; index -= 1) value = (value << 8n) | BigInt(source[index]);
  return value;
}

function bigIntLittleEndian(value, length) {
  const output = new Uint8Array(length);
  let remaining = value;
  for (let index = 0; index < length; index += 1) {
    output[index] = Number(remaining & 0xffn);
    remaining >>= 8n;
  }
  return output;
}

function poly1305(messageInput, oneTimeKeyInput) {
  const message = bytes(messageInput);
  const key = bytes(oneTimeKeyInput);
  if (key.length !== 32) throw new Error("invalid Poly1305 key");
  const r = littleEndianBigInt(key.slice(0, 16)) & 0x0ffffffc0ffffffc0ffffffc0fffffffn;
  const s = littleEndianBigInt(key.slice(16));
  const modulus = (1n << 130n) - 5n;
  let accumulator = 0n;
  for (let offset = 0; offset < message.length; offset += 16) {
    const block = message.slice(offset, Math.min(offset + 16, message.length));
    const value = littleEndianBigInt(block) + (1n << BigInt(block.length * 8));
    accumulator = ((accumulator + value) * r) % modulus;
  }
  return bigIntLittleEndian((accumulator + s) & ((1n << 128n) - 1n), 16);
}

function pad16(input) {
  const source = bytes(input);
  const remainder = source.length % 16;
  return remainder === 0 ? new Uint8Array() : new Uint8Array(16 - remainder);
}

function uint64le(value) {
  return bigIntLittleEndian(BigInt(value), 8);
}

function authenticationData(aad, ciphertext) {
  const associated = bytes(aad);
  const encrypted = bytes(ciphertext);
  return concat(
    associated,
    pad16(associated),
    encrypted,
    pad16(encrypted),
    uint64le(associated.length),
    uint64le(encrypted.length),
  );
}

function equalBytes(leftInput, rightInput) {
  const left = bytes(leftInput);
  const right = bytes(rightInput);
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) difference |= (left[index] || 0) ^ (right[index] || 0);
  return difference === 0;
}

export function chacha20poly1305Encrypt(key, nonce, plaintext, aad = new Uint8Array()) {
  const oneTimeKey = chachaBlock(bytes(key), 0, bytes(nonce)).slice(0, 32);
  const ciphertext = chachaXor(bytes(key), bytes(nonce), bytes(plaintext), 1);
  const tag = poly1305(authenticationData(bytes(aad), ciphertext), oneTimeKey);
  return concat(ciphertext, tag);
}

export function chacha20poly1305Decrypt(key, nonce, encrypted, aad = new Uint8Array()) {
  const input = bytes(encrypted);
  if (input.length < 16) throw new Error("authentication failed");
  const ciphertext = input.slice(0, -16);
  const suppliedTag = input.slice(-16);
  const oneTimeKey = chachaBlock(bytes(key), 0, bytes(nonce)).slice(0, 32);
  const expectedTag = poly1305(authenticationData(bytes(aad), ciphertext), oneTimeKey);
  if (!equalBytes(suppliedTag, expectedTag)) throw new Error("authentication failed");
  return chachaXor(bytes(key), bytes(nonce), ciphertext, 1);
}

export function base64UrlEncode(input) {
  const source = bytes(input);
  let binary = "";
  for (let offset = 0; offset < source.length; offset += 8192) {
    binary += String.fromCharCode(...source.slice(offset, offset + 8192));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function base64UrlDecode(value) {
  if (value === "") return new Uint8Array();
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url");
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(value.replaceAll("-", "+").replaceAll("_", "/") + padding);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

export { concat as concatBytes, bytes as toBytes };
