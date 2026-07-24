// RtcView service worker — network-first for shell so updates land immediately.
const CACHE = "rtcview-v3";
const SHELL = [
  "/",
  "/static/css/style.css",
  "/static/js/app.js",
  "/manifest.webmanifest",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/go2rtc/")) return;
  if (e.request.method !== "GET") return;

  // Icons rarely change — cache-first
  if (url.pathname.startsWith("/static/icons/")) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => { try { c.put(e.request, copy); } catch {} });
      return resp;
    })));
    return;
  }

  // Everything else (HTML, CSS, JS): network-first with cache fallback for offline.
  e.respondWith(
    fetch(e.request).then((resp) => {
      if (resp && resp.status === 200) {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => { try { c.put(e.request, copy); } catch {} });
      }
      return resp;
    }).catch(() => caches.match(e.request).then(r => r || caches.match("/")))
  );
});
