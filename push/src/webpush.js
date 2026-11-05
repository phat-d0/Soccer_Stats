// Web Push with WebCrypto only (runs in Cloudflare Workers and in Node 20+): VAPID
// (RFC 8292, an ES256 JWT) and the aes128gcm payload encryption (RFC 8291 / RFC 8188).
// No libraries: everything here is crypto.subtle.

const enc = new TextEncoder();

export function b64urlEncode(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlDecode(str) {
  const s = String(str).trim().replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

// HKDF-SHA-256 (extract with `salt`, expand with `info`) to `bytes` bytes.
async function hkdf(salt, ikm, info, bytes) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, bytes * 8);
  return new Uint8Array(bits);
}

// The VAPID signing key from the two strings `npx web-push generate-vapid-keys` prints (or
// scripts/vapid-keys.mjs): the public key, 65 raw bytes (0x04 || x || y), and the private
// scalar d, 32 bytes, both base64url.
export async function vapidKey(publicKey, privateKey) {
  const pub = b64urlDecode(publicKey);
  if (pub.length !== 65 || pub[0] !== 4) throw new Error("VAPID public key must be 65 bytes, uncompressed");
  const jwk = {
    kty: "EC",
    crv: "P-256",
    x: b64urlEncode(pub.slice(1, 33)),
    y: b64urlEncode(pub.slice(33, 65)),
    d: String(privateKey).trim(),
    ext: true,
  };
  return crypto.subtle.importKey("jwk", jwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
}

// "vapid t=<JWT>, k=<public key>" for one push service (aud = its origin), valid 12 hours.
export async function vapidAuthorization(endpoint, publicKey, signingKey, subject, now = Date.now()) {
  const header = b64urlEncode(enc.encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const claims = { aud: new URL(endpoint).origin, exp: Math.floor(now / 1000) + 12 * 3600, sub: subject };
  const body = b64urlEncode(enc.encode(JSON.stringify(claims)));
  const unsigned = `${header}.${body}`;
  // WebCrypto's ECDSA signature is already the raw r || s that JWS wants.
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, signingKey, enc.encode(unsigned));
  return `vapid t=${unsigned}.${b64urlEncode(sig)}, k=${publicKey}`;
}

const RECORD_SIZE = 4096;

// Encrypt `plaintext` (bytes or string) for one subscription's keys {p256dh, auth}
// (base64url): one aes128gcm record, header salt || rs || idlen || the sender's public key.
// `salt` and `senderKeys` can be given for tests; normally both are fresh each message.
export async function encrypt(plaintext, keys, { salt, senderKeys } = {}) {
  const uaPublic = b64urlDecode(keys.p256dh);
  const authSecret = b64urlDecode(keys.auth);
  const data = typeof plaintext === "string" ? enc.encode(plaintext) : plaintext;
  if (data.length > RECORD_SIZE - 17 - 86) throw new Error("payload too large for one record");
  salt = salt || crypto.getRandomValues(new Uint8Array(16));
  const as = senderKeys || (await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]));
  const asPublic = new Uint8Array(await crypto.subtle.exportKey("raw", as.publicKey));
  const uaKey = await crypto.subtle.importKey("raw", uaPublic, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: uaKey }, as.privateKey, 256));
  // RFC 8291 §3.4: IKM from the shared secret, keyed by the auth secret.
  const keyInfo = concat(enc.encode("WebPush: info\0"), uaPublic, asPublic);
  const ikm = await hkdf(authSecret, shared, keyInfo, 32);
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  // The last (only) record ends with the padding delimiter 0x02.
  const sealed = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key, concat(data, new Uint8Array([2]))));
  const rs = new Uint8Array(4);
  new DataView(rs.buffer).setUint32(0, RECORD_SIZE);
  return concat(salt, rs, new Uint8Array([asPublic.length]), asPublic, sealed);
}

// Send one push message. Returns the push service's status (201 = accepted; 404 or 410 =
// the subscription is gone).
export async function sendPush(subscription, payload, vapid, { ttl = 6 * 3600, urgency = "high", fetchFn = fetch } = {}) {
  const body = await encrypt(JSON.stringify(payload), subscription.keys);
  const res = await fetchFn(subscription.endpoint, {
    method: "POST",
    headers: {
      Authorization: await vapidAuthorization(subscription.endpoint, vapid.publicKey, vapid.signingKey, vapid.subject),
      "Content-Encoding": "aes128gcm",
      "Content-Type": "application/octet-stream",
      TTL: String(ttl),
      Urgency: urgency,
    },
    body,
  });
  return res.status;
}
