// RtcView SW — minimal. Only icons are cached so the UI is never stale.
// HTML/CSS/JS always fetched fresh from network.
const CACHE = "rtcview-icons-v4";
const ICONS = [
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ICONS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Only intercept icon requests — everything else bypasses SW entirely
  if (url.pathname.startsWith("/static/icons/") && e.request.method === "GET") {
    e.respondWith(
      caches.match(e.request).then((r) => r || fetch(e.request).then((resp) => {
        if (resp && resp.status === 200) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => { try { c.put(e.request, copy); } catch {} });
        }
        return resp;
      }))
    );
  }
  // No else → default browser fetch handling for all other requests
});
