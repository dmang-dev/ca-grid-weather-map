// Service worker for offline support of the PWA shell.
//
// Strategy:
//   - App shell (HTML, manifest, icons, CDN libs): cache-first.
//   - /data/* (the auto-refreshed JSON snapshots): network-first, fall back
//     to cache so the app still opens when offline.
//
// Bump CACHE_NAME any time the shell URLs change so old caches get evicted.

const CACHE_NAME = 'cagrid-shell-v11';
const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon.svg',
  './icon-maskable.svg',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.allSettled(SHELL_ASSETS.map((u) => cache.add(u)))
    )
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Same-origin /data/* — network-first so refreshed snapshots win, but
  // fall through to the last cached copy if offline.
  if (url.origin === self.location.origin && url.pathname.includes('/data/')) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // App shell + everything else: cache-first.
  event.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req).then((resp) => {
        // Opportunistically cache successful same-origin GETs.
        if (resp.ok && url.origin === self.location.origin) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
        }
        return resp;
      })
    )
  );
});
