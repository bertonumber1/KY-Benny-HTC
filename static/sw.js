/* VR-N7600 Remote — minimal service worker.
   Network-first for everything so updates always win; falls back to the
   last cached copy of the shell + static assets when offline. Never touches
   /api or the websockets. */
const CACHE = "vrn7600-v1";

self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) return;
  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.ok && (url.pathname === "/" ||
                      url.pathname.startsWith("/static/") ||
                      url.pathname === "/sw.js")) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match(e.request))
  );
});
