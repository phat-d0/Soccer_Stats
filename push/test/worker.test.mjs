// Bet-alert Worker tests: no Cloudflare account, no network. Node 20+ (node --test test/).
// The push service is a stub; the phone's side of the encryption is played here, so a
// message the Worker encrypts must decrypt to what was sent (RFC 8291).
import assert from "node:assert/strict";
import { test } from "node:test";
import { handle } from "../src/index.js";
import { b64urlDecode, b64urlEncode, concat, encrypt, vapidAuthorization, vapidKey } from "../src/webpush.js";

const enc = new TextEncoder();
const dec = new TextDecoder();

async function hkdf(salt, ikm, info, n) {
  const k = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, k, n * 8));
}

// A browser's subscription: its ECDH key pair and auth secret.
async function phone(endpoint = "https://push.example.net/send/abc") {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const pub = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  const auth = crypto.getRandomValues(new Uint8Array(16));
  return { pair, sub: { endpoint, keys: { p256dh: b64urlEncode(pub), auth: b64urlEncode(auth) } } };
}

// The receiving side of aes128gcm, written from RFC 8291 / 8188.
async function decrypt(body, ph) {
  const salt = body.slice(0, 16);
  const rs = new DataView(body.buffer, body.byteOffset + 16, 4).getUint32(0);
  const idlen = body[20];
  const asPub = body.slice(21, 21 + idlen);
  const sealed = body.slice(21 + idlen);
  assert.equal(rs, 4096);
  assert.equal(idlen, 65);
  const asKey = await crypto.subtle.importKey("raw", asPub, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: asKey }, ph.pair.privateKey, 256));
  const uaPub = b64urlDecode(ph.sub.keys.p256dh);
  const ikm = await hkdf(b64urlDecode(ph.sub.keys.auth), shared, concat(enc.encode("WebPush: info\0"), uaPub, asPub), 32);
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["decrypt"]);
  const plain = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce }, key, sealed));
  assert.equal(plain[plain.length - 1], 2, "last record ends with the 0x02 delimiter");
  return dec.decode(plain.slice(0, -1));
}

async function vapidPair() {
  const p = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const pub = new Uint8Array(await crypto.subtle.exportKey("raw", p.publicKey));
  const jwk = await crypto.subtle.exportKey("jwk", p.privateKey);
  return { publicKey: b64urlEncode(pub), privateKey: jwk.d, verifyKey: p.publicKey };
}

async function verifyVapid(header, endpoint, vp) {
  const m = header.match(/^vapid t=([^,]+), k=(.+)$/);
  assert.ok(m, header);
  assert.equal(m[2], vp.publicKey);
  const [h, c, s] = m[1].split(".");
  assert.deepEqual(JSON.parse(dec.decode(b64urlDecode(h))), { typ: "JWT", alg: "ES256" });
  const claims = JSON.parse(dec.decode(b64urlDecode(c)));
  assert.equal(claims.aud, new URL(endpoint).origin);
  assert.ok(claims.exp > Date.now() / 1000 && claims.exp <= Date.now() / 1000 + 24 * 3600);
  assert.ok(await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, vp.verifyKey, b64urlDecode(s), enc.encode(`${h}.${c}`)));
  return claims;
}

class FakeKV {
  constructor() { this.m = new Map(); }
  async get(k) { return this.m.has(k) ? this.m.get(k) : null; }
  async put(k, v) { this.m.set(k, v); }
  async delete(k) { this.m.delete(k); }
  async list({ prefix = "" } = {}) {
    return { keys: [...this.m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true };
  }
}

async function env() {
  const vp = await vapidPair();
  return {
    vp,
    env: {
      SUBS: new FakeKV(), VAPID_PUBLIC_KEY: vp.publicKey, VAPID_PRIVATE_KEY: vp.privateKey,
      ALERT_TOKEN: "tok-123", PAIR_CODE: "24681357", VAPID_SUBJECT: "https://phat-d0.github.io/Soccer_Stats/",
      ALLOW_ORIGIN: "https://phat-d0.github.io",
    },
  };
}

const post = (path, body, headers = {}) =>
  new Request(`https://alerts.example.workers.dev${path}`, { method: "POST", headers: { "Content-Type": "application/json", Origin: "https://phat-d0.github.io", ...headers }, body: JSON.stringify(body) });

// A push service stub: records each call and answers with the status given per endpoint.
function pushService(statusFor = () => 201) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: statusFor(url) });
  };
  return { calls, fetchFn };
}

test("an encrypted message decrypts to the payload on the phone's side", async () => {
  const ph = await phone();
  const msg = JSON.stringify({ title: "Blend Lean: Arsenal v Leeds", body: "Arsenal to win · +150 (2.50) · $10" });
  const body = await encrypt(msg, ph.sub.keys);
  assert.equal(await decrypt(body, ph), msg);
  // A fresh salt and sender key every time: the same message never encrypts the same way.
  assert.notDeepEqual(await encrypt(msg, ph.sub.keys), body);
});

test("the VAPID header is an ES256 JWT for the push service's origin", async () => {
  const vp = await vapidPair();
  const key = await vapidKey(vp.publicKey, vp.privateKey);
  const endpoint = "https://web.push.apple.com/QGuQyavXutnMPAUY";
  const h = await vapidAuthorization(endpoint, vp.publicKey, key, "https://phat-d0.github.io/Soccer_Stats/");
  const claims = await verifyVapid(h, endpoint, vp);
  assert.equal(claims.sub, "https://phat-d0.github.io/Soccer_Stats/");
});

test("subscribing needs the pairing code", async () => {
  const { env: e } = await env();
  const ph = await phone();
  let r = await handle(post("/subscribe", { code: "wrong", subscription: ph.sub }), e);
  assert.equal(r.status, 403);
  assert.equal(e.SUBS.m.size, 0);
  r = await handle(post("/subscribe", { code: "24681357", subscription: { endpoint: "http://insecure", keys: {} } }), e);
  assert.equal(r.status, 400);
  r = await handle(post("/subscribe", { code: "24681357", subscription: ph.sub }), e);
  assert.equal(r.status, 200);
  assert.equal(e.SUBS.m.size, 1);
  assert.equal(r.headers.get("Access-Control-Allow-Origin"), "https://phat-d0.github.io");
  // Subscribing again replaces, never duplicates.
  await handle(post("/subscribe", { code: "24681357", subscription: ph.sub }), e);
  assert.equal(e.SUBS.m.size, 1);
  r = await handle(post("/unsubscribe", { endpoint: ph.sub.endpoint }), e);
  assert.equal(r.status, 200);
  assert.equal(e.SUBS.m.size, 0);
});

test("an alert needs the token and reaches every subscription, dropping gone ones", async () => {
  const { env: e, vp } = await env();
  const a = await phone("https://push.example.net/send/a");
  const gone = await phone("https://push.example.net/send/gone");
  const broken = await phone("https://push.example.net/send/broken");
  for (const ph of [a, gone, broken]) await handle(post("/subscribe", { code: "24681357", subscription: ph.sub }), e);
  const svc = pushService((url) => (url.endsWith("/gone") ? 410 : url.endsWith("/broken") ? 500 : 201));
  const alert = { title: "Corners (DraftKings): Arsenal over 5.5", body: "Over 5.5 · −110 (1.91) · $10 stake", kickoff: "2026-11-07T15:00:00+00:00", url: "./?trade=x", tag: "x" };
  let r = await handle(post("/alert", alert), e, svc.fetchFn);
  assert.equal(r.status, 401);
  r = await handle(post("/alert", alert, { Authorization: "Bearer nope" }), e, svc.fetchFn);
  assert.equal(r.status, 401);
  assert.equal(svc.calls.length, 0);
  r = await handle(post("/alert", alert, { Authorization: "Bearer tok-123" }), e, svc.fetchFn);
  assert.deepEqual(await r.json(), { ok: true, sent: 1, removed: 1, failed: 1 });
  assert.equal(svc.calls.length, 3);
  assert.equal(e.SUBS.m.size, 2, "the 410 subscription is dropped, the 500 one kept");
  const call = svc.calls.find((c) => c.url.endsWith("/a"));
  assert.equal(call.init.headers["Content-Encoding"], "aes128gcm");
  assert.equal(call.init.headers.TTL, String(6 * 3600));
  await verifyVapid(call.init.headers.Authorization, call.url, vp);
  assert.deepEqual(JSON.parse(await decrypt(call.init.body, a)), alert);
});

test("health, the public key, a test alert and unknown routes", async () => {
  const { env: e } = await env();
  assert.deepEqual(await (await handle(new Request("https://x.dev/health"), e)).json(), { ok: true });
  assert.equal((await (await handle(new Request("https://x.dev/vapid-public-key"), e)).json()).key, e.VAPID_PUBLIC_KEY);
  assert.equal((await handle(new Request("https://x.dev/nothing"), e)).status, 404);
  assert.equal((await handle(new Request("https://x.dev/subscribe", { method: "OPTIONS" }), e)).status, 204);
  const ph = await phone();
  await handle(post("/subscribe", { code: "24681357", subscription: ph.sub }), e);
  const svc = pushService();
  let r = await handle(post("/test", { code: "nope", endpoint: ph.sub.endpoint }), e, svc.fetchFn);
  assert.equal(r.status, 403);
  r = await handle(post("/test", { code: "24681357", endpoint: ph.sub.endpoint }), e, svc.fetchFn);
  assert.deepEqual(await r.json(), { ok: true, status: 201 });
  assert.equal(JSON.parse(await decrypt(svc.calls[0].init.body, ph)).title, "Bet alerts are on");
});

// RFC 8291 Appendix A: the published example, byte for byte.
test("encryption matches RFC 8291's worked example", async () => {
  const asPub = b64urlDecode("BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8");
  const jwk = { kty: "EC", crv: "P-256", x: b64urlEncode(asPub.slice(1, 33)), y: b64urlEncode(asPub.slice(33)), d: "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw" };
  const senderKeys = {
    privateKey: await crypto.subtle.importKey("jwk", jwk, { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]),
    publicKey: await crypto.subtle.importKey("raw", asPub, { name: "ECDH", namedCurve: "P-256" }, true, []),
  };
  const keys = { p256dh: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", auth: "BTBZMqHH6r4Tts7J_aSIgg" };
  const body = await encrypt("When I grow up, I want to be a watermelon", keys, { salt: b64urlDecode("DGv6ra1nlYgDCS1FRnbzlw"), senderKeys });
  assert.equal(
    b64urlEncode(body),
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
  );
});
