/* Minima service worker.
 *
 * Strategy is deliberately split because this app's value is LIVE data:
 *   - /api/*        -> network-only, NEVER cached. Weather/NOTAM/SIGMET results
 *                     must always be fresh ("forecasts are not observations").
 *   - navigations   -> network-first, fall back to the cached shell when offline
 *                     so the app still opens (and then shows its own errors).
 *   - other GETs    -> cache-first (the static shell: HTML/CSS/JS/icons).
 *   - cross-origin  -> bypassed entirely (Leaflet CDN, GeoMet radar tiles, etc.).
 *
 * Bump VERSION on any shell change; old caches are purged on activate, and
 * skipWaiting + clients.claim make a new deploy take over on the next load so
 * users never get stuck on a stale shell.
 */
const VERSION = "minima-v1-20260630";
const SHELL_CACHE = `shell-${VERSION}`;

const SHELL = [
  "/",
  "/index.html",
  "/app.js",
  "/style.css",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
  "/apple-touch-icon.png",
  "/favicon-32.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Only manage our own origin; let the CDN / tile servers do their thing.
  if (url.origin !== self.location.origin) return;

  // Live data is never cached.
  if (url.pathname.startsWith("/api/")) return;

  // App navigations: try the network, fall back to the cached shell offline.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/index.html", { ignoreSearch: true }))
    );
    return;
  }

  // Static assets: serve from cache, then fall back to (and warm) the network.
  // ignoreSearch so the ?v=... cache-busting query still hits the cache.
  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => {
      if (hit) return hit;
      return fetch(req).then((res) => {
        if (res && res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      });
    })
  );
});
