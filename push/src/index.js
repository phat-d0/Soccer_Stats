// Soccer Model bet alerts (owner, 5 Nov): a Cloudflare Worker that keeps the phone's Web
// Push subscription and forwards each new lead-strategy paper trade to it.
//
// Routes:
//   GET  /health              {ok: true}
//   GET  /vapid-public-key    {key}: the app subscribes with it
//   POST /subscribe           {code, subscription}: stored when code = PAIR_CODE
//   POST /unsubscribe         {endpoint}
//   POST /test                {code, endpoint}: one test alert to that subscription
//   POST /alert               Bearer ALERT_TOKEN, {title, body, url?, tag?}: to every
//                             subscription; a 404 or 410 answer removes it
// Bindings: KV namespace SUBS. Secrets: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, ALERT_TOKEN,
// PAIR_CODE. Vars: VAPID_SUBJECT, ALLOW_ORIGIN.

import { sendPush, vapidKey } from "./webpush.js";

const enc = new TextEncoder();

async function sha256hex(s) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
  return [...d].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Equal-length comparison that doesn't stop at the first difference (hashes first, so the
// lengths always match).
async function sameSecret(given, want) {
  if (!want || typeof given !== "string") return false;
  const [a, b] = await Promise.all([sha256hex(given), sha256hex(want)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function cors(env, req) {
  const allowed = String(env.ALLOW_ORIGIN || "").split(",").map((s) => s.trim()).filter(Boolean);
  const origin = req.headers.get("Origin") || "";
  return {
    "Access-Control-Allow-Origin": allowed.includes(origin) ? origin : allowed[0] || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

const json = (data, status, headers) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...headers } });

const subKey = async (endpoint) => `sub:${await sha256hex(endpoint)}`;

function validSubscription(s) {
  try {
    return (
      s && typeof s.endpoint === "string" && new URL(s.endpoint).protocol === "https:" &&
      typeof s.keys?.p256dh === "string" && typeof s.keys?.auth === "string" && s.endpoint.length < 2048
    );
  } catch {
    return false;
  }
}

async function readJson(req) {
  try {
    return await req.json();
  } catch {
    return null;
  }
}

async function vapid(env) {
  return {
    publicKey: env.VAPID_PUBLIC_KEY,
    signingKey: await vapidKey(env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY),
    subject: env.VAPID_SUBJECT || "https://phat-d0.github.io/Soccer_Stats/",
  };
}

async function allSubscriptions(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.SUBS.list({ prefix: "sub:", cursor });
    for (const k of page.keys) {
      const v = await env.SUBS.get(k.name);
      if (v) out.push({ key: k.name, sub: JSON.parse(v) });
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

function cleanPayload(p) {
  const s = (v, n) => (typeof v === "string" ? v.slice(0, n) : undefined);
  return { title: s(p.title, 120) || "Soccer Model", body: s(p.body, 400) || "", kickoff: s(p.kickoff, 40), url: s(p.url, 500), tag: s(p.tag, 200) };
}

export async function handle(req, env, fetchFn = fetch) {
  const h = cors(env, req);
  const { pathname } = new URL(req.url);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: h });
  if (req.method === "GET" && pathname === "/health") return json({ ok: true }, 200, h);
  if (req.method === "GET" && pathname === "/vapid-public-key") {
    return env.VAPID_PUBLIC_KEY ? json({ key: env.VAPID_PUBLIC_KEY }, 200, h) : json({ error: "not set up" }, 503, h);
  }
  if (req.method !== "POST") return json({ error: "not found" }, 404, h);

  if (pathname === "/subscribe") {
    const b = await readJson(req);
    if (!(await sameSecret(b?.code, env.PAIR_CODE))) return json({ error: "wrong pairing code" }, 403, h);
    if (!validSubscription(b.subscription)) return json({ error: "bad subscription" }, 400, h);
    const { endpoint, keys } = b.subscription;
    await env.SUBS.put(await subKey(endpoint), JSON.stringify({ endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth }, at: new Date().toISOString() }));
    return json({ ok: true }, 200, h);
  }
  if (pathname === "/unsubscribe") {
    const b = await readJson(req);
    if (typeof b?.endpoint !== "string") return json({ error: "bad request" }, 400, h);
    await env.SUBS.delete(await subKey(b.endpoint));
    return json({ ok: true }, 200, h);
  }
  if (pathname === "/test") {
    const b = await readJson(req);
    if (!(await sameSecret(b?.code, env.PAIR_CODE))) return json({ error: "wrong pairing code" }, 403, h);
    const raw = typeof b?.endpoint === "string" ? await env.SUBS.get(await subKey(b.endpoint)) : null;
    if (!raw) return json({ error: "not subscribed" }, 404, h);
    const status = await sendPush(JSON.parse(raw), { title: "Bet alerts are on", body: "You'll get one alert when Blend Lean or DraftKings corners (under 60%) opens a paper trade.", tag: "test" }, await vapid(env), { fetchFn });
    return json({ ok: status >= 200 && status < 300, status }, 200, h);
  }
  if (pathname === "/alert") {
    const auth = req.headers.get("Authorization") || "";
    if (!(await sameSecret(auth.replace(/^Bearer\s+/i, ""), env.ALERT_TOKEN))) return json({ error: "unauthorised" }, 401, h);
    const b = await readJson(req);
    if (!b || typeof b.title !== "string") return json({ error: "bad request" }, 400, h);
    const payload = cleanPayload(b);
    const v = await vapid(env);
    let sent = 0, removed = 0, failed = 0;
    for (const { key, sub } of await allSubscriptions(env)) {
      let status = 0;
      try {
        status = await sendPush(sub, payload, v, { fetchFn });
      } catch {
        status = 0;
      }
      if (status >= 200 && status < 300) sent++;
      else if (status === 404 || status === 410) {
        await env.SUBS.delete(key);
        removed++;
      } else failed++;
    }
    return json({ ok: true, sent, removed, failed }, 200, h);
  }
  return json({ error: "not found" }, 404, h);
}

export default {
  fetch: (req, env) => handle(req, env),
};
