// Offline support: app shell is cached; data.json is network-first so you always get
// the latest predictions when online and the last ones when not.
// Renamed from pl-model-vNN with the app (8 Oct); activate deletes every other cache,
// old names included.
const CACHE = "soccer-model-v76";
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;
  // Network first for everything (so updates show up), falling back to cache offline.
  e.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("index.html")))
  );
});

// ---------- Bet alerts (owner, 5 Nov; push/README.md) ----------
// The Worker sends {title, body, kickoff (ISO), url ("./?trade=<id>"), tag (trade id)}; the
// kickoff is shown in this phone's own time. A tap focuses the app on the trade's sheet.
function kickoffText(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "";
  return `kicks off ${d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}`;
}
self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch { p = { body: e.data ? e.data.text() : "" }; }
  const body = [p.body, kickoffText(p.kickoff)].filter(Boolean).join(" · ");
  e.waitUntil(self.registration.showNotification(p.title || "Soccer Model", {
    body,
    tag: p.tag || undefined,
    icon: "icons/icon-192.png",
    badge: "icons/icon-192.png",
    data: { url: p.url || "./", trade: p.tag || null },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url = "./", trade = null } = e.notification.data || {};
  const target = new URL(url, self.registration.scope).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (win) {
      await win.focus();
      if (trade) win.postMessage({ openTrade: trade });
      return;
    }
    await self.clients.openWindow(target);
  })());
});
