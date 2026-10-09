// Service worker for offline support of the PWA shell.
//
// Strategy:
//   - App shell (HTML, manifest, icons) and the CDN libraries: precached at
//     install, served cache-first.
//   - /data/* (the auto-refreshed JSON snapshots): precached at install too,
//     then network-first, falling back to cache so the app still opens when
//     offline. Only good responses are cached, and an error response is
//     answered with the last good copy.
//
// Bump CACHE_NAME whenever a shell file or any URL list changes: the shell
// is served cache-first, so an edited index.html reaches installed clients
// only when a new worker re-precaches it (and the bump evicts the old cache).

const CACHE_NAME = 'cagrid-shell-v15';
const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon.svg',
  './icon-maskable.svg',
];

// The exact <link>/<script> URLs in index.html; keep the two in sync.
//
// Precached at install rather than cached on first use: the page registers
// this worker on 'load', after it has already fetched them, so a runtime
// cache would miss them on the first visit and leave an offline launch to
// whatever the browser's HTTP cache still holds. Fetched with CORS rather
// than as the opaque no-cors responses the page's own tags get: unpkg sends
// Access-Control-Allow-Origin: *, and only a CORS response has a status to
// check. An opaque one is cached blind, and since these are served
// cache-first, a CDN 5xx caught at install would stand in for Leaflet until
// the next CACHE_NAME bump, online too.
const CDN_ASSETS = [
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet-velocity@2.1.4/dist/leaflet-velocity.min.css',
  'https://unpkg.com/@turf/turf@7.1.0/turf.min.js',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://unpkg.com/leaflet-velocity@2.1.4/dist/leaflet-velocity.min.js',
];

// Every data/ file index.html fetches; keep the two in sync. Precached for
// the same first-visit reason: the page has already loaded its layers by the
// time this worker exists, so without this a PWA installed on its first visit
// opened offline to an empty map and "No manifest found". The page's own
// loads usually left these in the HTTP cache moments ago, so this is rarely
// a second download.
const DATA_ASSETS = [
  './data/manifest.json',
  './data/wind.json',
  './data/ca_counties.geojson',
  './data/pge_territory.geojson',
  './data/raws.geojson',
  './data/outage_points.geojson',
  './data/outage_areas.geojson',
  './data/nws_alerts.geojson',
  './data/wildfires.geojson',
  './data/fire_points.geojson',
  './data/evacuations.geojson',
  './data/inciweb.geojson',
  './data/psps.geojson',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  // allSettled: an unreachable CDN (or one missing file) must not stop the
  // shell from installing. The fetch handler fills any gap on a later visit.
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      Promise.allSettled([
        // reload: past the HTTP cache. GitHub Pages serves index.html with
        // max-age=600, so a client that loaded the page in the ten minutes
        // before this worker installed stored the OLD index.html under the
        // new CACHE_NAME, and ran it until the next bump.
        ...SHELL_ASSETS.map((u) => cache.add(new Request(u, { cache: 'reload' }))),
        ...CDN_ASSETS.map((u) => cache.add(new Request(u, { mode: 'cors' }))),
        ...DATA_ASSETS.map((u) => cache.add(u)),
      ])
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
      // no-cache: ask the server (a 304 when nothing changed) rather than take
      // the HTTP cache's copy. GitHub Pages serves max-age=600, so for ten
      // minutes after a load this was cache-first in practice, and the
      // banner's "newer data — reload" link brought back the snapshot already
      // on screen, or the new manifest over the old layers.
      fetch(req, { cache: 'no-cache' })
        .then((resp) => {
          // Only a good response replaces the cached copy. A 404/5xx used to
          // overwrite it and be served instead, so a layer missing from one
          // deploy (pge_territory.geojson was a 404 for four days in Sept
          // 2026) vanished even for clients holding a good copy. Serve that
          // copy; pass the error through only when there is none.
          if (!resp.ok) return caches.match(req).then((cached) => cached || resp);
          // The banner's newer-deploy probe asks for no-store. Keeping its
          // manifest would pair it with the previous deploy's cached layers
          // on the next offline launch: counts the map doesn't draw, and no
          // stale warning however old those layers are.
          if (req.cache !== 'no-store') {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          }
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // CDN libraries — cache-first from the install-time precache. A miss means
  // that precache failed, or index.html asks for a URL CDN_ASSETS lacks (a
  // version bump that missed this file): cache it on first use, on the same
  // CORS terms, so offline still works from the next visit on. If the CORS
  // fetch itself fails, fall back to the page's own request, uncached, rather
  // than fail a library that would load without this worker.
  if (url.origin === 'https://unpkg.com') {
    event.respondWith(
      caches.match(req.url, { ignoreVary: true }).then((cached) =>
        cached ||
        fetch(req.url, { mode: 'cors' }).then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req.url, copy));
          }
          return resp;
        }, () => fetch(req))
      )
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
