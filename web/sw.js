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
 * VERSION is stamped by the server from the SHELL FILES' OWN CONTENT (see
 * main.py:service_worker) - it is not maintained by hand, because by hand it
 * was forgotten. app.js changed in three separate PRs while VERSION sat still,
 * and cache-first + ignoreSearch meant every browser that had already
 * installed this worker kept serving the OLD app.js indefinitely. That shipped
 * as a card showing a MITIGATE badge with no explanation under it: the backend
 * was sending the threat rows, and a months-old script was rendering the page.
 * The "sometimes it works" was just which devices had the worker installed.
 *
 * Old caches are purged on activate, and skipWaiting + clients.claim make a new
 * deploy take over on the next load so users never get stuck on a stale shell.
 */
const VERSION = "__SHELL_VERSION__";
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
  /* The three type roles, latin subset. These are in the SHELL and not fetched
     from a font CDN precisely because of the line above this list: a
     cross-origin request is bypassed by this worker, so an installed app
     opened offline would lose its type and re-flow every number column. */
  "/fonts/plex-sans-400.woff2",
  "/fonts/plex-sans-600.woff2",
  "/fonts/plex-sans-condensed-600.woff2",
  "/fonts/plex-sans-condensed-700.woff2",
  "/fonts/plex-mono-400.woff2",
  "/fonts/plex-mono-500.woff2",
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
