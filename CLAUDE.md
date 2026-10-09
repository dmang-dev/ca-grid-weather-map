# CLAUDE.md — ca-grid-weather-map

California electricity-grid + weather PWA. Python Flask-style server +
single-file frontend; no build step.

## Build / install

```bash
python3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt        # Windows
source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

`eccodes` is the heavy install (HRRR GRIB parsing); ~5 minutes one-time.

## Run

```bash
python fetch_data.py    # populate data/ (~25 s; the deploy re-runs it every 2 h)
python serve.py         # port 8000, also serves POST /refresh
# open http://localhost:8000/
```

For a native window instead of a browser tab: `pip install -r
requirements-desktop.txt && python app.py` (pywebview shell).

## Toolchain

- Python 3.11 (3.10+ probably works)
- Frontend is Leaflet 1.9 + leaflet-velocity 2.1 + Turf.js 7 (CDN, no npm)
- HRRR fetcher uses [Herbie](https://herbie.readthedocs.io) + cfgrib + xarray

## File layout

```
app.py                  pywebview native-window launcher
serve.py                Static + POST /refresh subprocess shim
fetch_data.py           All data fetchers (one function per upstream)
probe_endpoints.py      Diagnostic helper for testing upstream APIs
index.html              Entire frontend, single file
sw.js                   Service worker — PWA offline cache
manifest.webmanifest    PWA install manifest
icon.svg / icon-maskable.svg   PWA icons
data/                   Cached upstream responses (gitignored; built at deploy)
mobile/                 Capacitor wrapper for Android/iOS
pinger/                 Cloudflare Worker — rebuilds when GH's cron misses
```

## Conventions

- **It's a PWA.** Service worker lives in `sw.js`. It precaches the
  shell, the unpkg libraries and every `data/` file the page loads, so
  one visit is enough for an offline launch; `tests/test_sw_lists.py`
  fails if those lists drift from `index.html`. Bump `CACHE_NAME` when a
  shell file changes. **Do not break offline mode** — if you change the
  cached file list, smoke-test offline in Lighthouse before pushing.
- **Desktop vs mobile.** Project root = desktop/PWA. `mobile/` =
  Capacitor app-store wrapper that points at the hosted PWA.
- **`data/` is regenerable and never committed.** Anything in there is
  `fetch_data.py`'s output. `deploy-pages.yml` regenerates it on the
  runner and ships it inside the Pages artifact, so the hosted demo
  stays fresh without the output entering git history — committing it
  every 2 hours had taken the repo to 459 MB. Run `python fetch_data.py`
  to populate `data/` locally; only `data/README.md` is tracked.
- **The deploy seeds `data/` before fetching.** Because nothing is
  committed, a runner starts empty — which silently disabled
  `run_one()`'s stale carry-forward: with no previous file and no
  previous manifest, a failing fetcher returned `error` and its layer
  **vanished from the site** instead of being served as `cached`. That
  is how one removed CEC field took utility territories off the map for
  four days in Sept 2026, as a 404. `.github/scripts/seed_data.py`
  downloads the published snapshot first, so the safety net works again.
  It is best-effort by design: malformed or missing files are skipped
  and it always exits 0, so a cold start behaves exactly as before.
- **Fetchers are failure-isolated.** Each fetches one source and writes
  one file. A broken upstream must not prevent the others from updating.
- **Frontend is single-file.** `index.html` carries all the JS/CSS. No
  build step, no bundler, no npm at the root. Keep it that way.

## Pitfalls

- **HRRR is forecast wind, not observed.** `fetch_raws()` adds the
  observed side: ~4,200 RAWS stations from the same CAL FIRE ArcGIS org
  as FIRIS, refreshed within the hour, with gusts, RH and temperature.
  It's point data, not a field — the hover panel reports the nearest
  station within 25 km beside the HRRR value, and the layer is **off by
  default** because 4,000 markers bury everything else. A gridded
  observed field would still mean RTMA and more GRIB processing.
- **HRRR's U/V are grid-relative.** Its 10 m winds point along the
  Lambert conformal grid (`uvRelativeToGrid=1`), and neither Herbie nor
  cfgrib rotates them. Written straight out as eastward/northward, every
  arrow over California leaned 10–17° clockwise and disagreed with the
  RAWS station beside it. `_earth_relative_winds()` rotates on the native
  grid before resampling, and raises (→ stale) rather than publish winds
  whose orientation the GRIB attributes don't state.
- **ArcGIS can truncate without saying so.** RAWS holds 4,208 records
  and returns exactly 2,000 with `exceededTransferLimit` *absent* —
  the flag only shows up on page 2. Anything that might exceed
  `maxRecordCount` must go through `arcgis_query_all()`, which pages
  with `resultOffset`. Don't trust the flag to warn you.
- **RAWS empties and refills.** The upstream layer is periodically
  reloaded, and a query caught mid-reload returns a few hundred stations
  as a complete answer — no time filter is involved, so a short count is
  never real weather. 12 of 100 deploys in Sept–Oct 2026 published
  532–1,864 of ~4,200 stations as `ok`. `fetch_raws()` raises below
  `RAWS_MIN_STATIONS` (3,000) so the last full file is served as stale.
- **WFIGS perimeters lag; FIRIS doesn't.** WFIGS only has a polygon
  once someone ground-maps the fire and a GIS specialist uploads it — a
  day or more, worst on CAL FIRE state incidents. California's own
  answer is FIRIS (Fire Integrated Real-Time Intelligence System):
  state aircraft flying IR sensors that publish "heat perimeters"
  within hours. That's what fire.ca.gov's incident maps actually draw,
  which is why a fire can have a shape there and none here.
  `_fires_from_firis()` unions that layer into `wildfires.geojson`.
  Neither source contains the other — take the union, not one or the
  other. FIRIS rows carry **no IRWIN id**, so cross-referencing them
  falls back to normalized incident name; that's why
  `fetch_fire_points()` checks both.
- **FIRIS gives geometry, almost nothing else.** No containment, cause,
  county or discovery date. `_backfill_perimeter_attrs()` fills those
  from the incident point for the same fire, and only where the
  perimeter is blank — WFIGS's own values stay authoritative. It lives
  in `fetch_fire_points()` because the dependency runs both ways:
  points need the perimeter file to set `_has_perimeter`, so it must
  already be written. Expect partial coverage — perimeters older than
  the WFIGS "Current" points feed have no point left to join to.
- **Fire names don't agree across sources.** `_fire_keys()` returns
  several candidates per name: CAL FIRE's "Gann Fire" and WFIGS's
  "GANN" normalize together, and FIRIS's complex naming
  ("CINDERCOMPLEX-5-3") also answers to its sub-designation ("5-3"),
  which is what WFIGS calls it. That alias can be as short as `53`, so
  **alias hits must be confirmed geometrically** (`ALIAS_CONFIRM_KM`, 40
  km from the perimeter's extent) before they're trusted — the primary
  key match doesn't need it, the alias does. That covers every name match
  that isn't a perimeter's own primary key, `_has_perimeter` included.
  FIRIS also re-uploads a fire as "<NAME>-UPDATED" now and then, which
  drew the same fire twice (RUMSEY vs RUMSEY-UPDATED, 0.01 km apart);
  `_FIRIS_REVISION_SUFFIX` folds that away before the latest-flight
  dedup. Only `-UPDATED` — a trailing *number* is a real sub-fire.
- **Names alone still draw fires twice.** FIRIS calls WFIGS's "3-6
  WAGON" just "WAGON", cross-wires Cinder's sub-fires ("5-3" carried
  5-4's polygon), and flies an umbrella CINDER COMPLEX over both — 4 of 37
  fires drawn twice on Aug 4 2026. `fetch_fires()` therefore also merges
  a FIRIS perimeter sharing ≥ 50% of *each* polygon with one already
  drawn, and drops one ≥ 90% covered by its sub-fires (`firis_merged`,
  `firis_covered`). It stays conservative — a wrong merge hides a real
  fire — and never treats a *fresher* flight inside an older perimeter
  as covered: that's a new fire (a reburn), not the old one.
- **Evacuation zones are Zonehaven, not a CAL FIRE original.** Genasys's
  product is Zonehaven; CAL FIRE republishes it (plus zones some counties
  publish themselves — one current record comes from Tuolumne County's
  own ArcGIS, so don't filter on `state_abbr`, which is null there). The
  layer carries *only* non-Normal zones, so everything in it is live.
  `est_population` and `structure_count` are null on every record —
  don't build on them. `last_updated` is unreliable: some timestamps
  land hours in the future, so the UI never prints an age for a zone.
- **Red Flag Warnings live on fire weather zones.** Zone-based NWS
  alerts come without a polygon; their shape is the zones they name. A
  bare UGC code doesn't say which kind of zone it is, and
  `/zones/forecast/CAZ270` 404s for a fire zone — that silently dropped 3
  of 5 matched alerts in Oct 2026 while the layer said `ok`. Take shapes
  from `affectedZones` (full URLs, type included; only api.weather.gov
  ones are fetched). An alert still left without geometry counts as
  `dropped_no_geometry` and adds to the manifest entry's `warnings` list,
  which the deploy step prints. Expired alerts are filtered in the page,
  not the fetcher: deploys are hours apart, and an end time passes
  whether or not one runs.
- **A fire can still have no polygon anywhere.** Until the first IR
  flight, a fire exists only as a point. `fetch_fire_points()` covers
  that; `_has_perimeter: false` marks the ones no perimeter layer can
  show, and the map draws those boldly.
- **IRWIN acreage goes stale; CAL FIRE's doesn't.** WFIGS reports
  whatever IRWIN was last told, which on state incidents can be wildly
  behind — Grade sat at 0.1 acres against CAL FIRE's 689.
  `fetch_calfire()` pulls `incidents.fire.ca.gov` (plain JSON, not
  ArcGIS) and `fetch_fire_points()` overlays it by normalized name +
  county, confirmed by position. `_calfire_acres` wins over
  `IncidentSize` everywhere in the UI. CAL FIRE incidents WFIGS lacks
  get appended, unless a WFIGS point sits within 5 km — Cinder Complex
  is 1.2 km from WFIGS's "5-4", the same fire under lightning-complex
  numbering, and appending it would draw the fire twice.
- Three CA utilities (LADWP, IID, PacifiCorp) don't publish to OES;
  their outage events will never appear no matter how robust the
  fetcher is.
- **GitHub's `schedule` trigger is best-effort, and it shows.** Since
  late Aug 2026 it has fired only ~30–40% of `deploy-pages.yml`'s
  2-hourly ticks: gaps average 5–7 h and reach 9 h. Don't treat the cron
  as a cadence guarantee — `pinger/` (a Cloudflare Worker) watches the
  published manifest and dispatches a rebuild when it goes stale; while
  it worked, data never passed the UI's 4.5 h stale threshold. The
  workflow cron stays armed as a backstop; the worker checks the manifest
  age first so the two schedulers don't queue duplicate deploys.
- **The pinger runs on a token that expires.** Its first PAT took
  GitHub's default 30-day expiry and lapsed on 2026-09-06. Failures were
  only `console.log`ged, so it dispatched nothing for 33 days and nobody
  noticed. It now checks the token every tick, warns 7 days ahead, and
  throws on failure (Workers Logs record it). The telltale from GitHub's
  side: no `workflow_dispatch` runs of `deploy-pages.yml` for hours while
  the snapshot is over 2 h old. Also, GitHub disables a public repo's
  scheduled workflows after 60 days without a commit, and a disabled
  workflow refuses dispatches too.
- **eckitlib 2.3 crashes Python at exit on Linux.** It bundles its own
  PROJ, findlibs loads it `RTLD_GLOBAL`, and a pyproj imported after
  eccodes binds to it: `free(): invalid pointer`, exit 134, after all
  the work is done. It kept the canary's ubuntu leg red Sep 21–Oct 5
  2026; the deploy survived only on import order. `requirements.txt`
  pins `eckitlib<2.3` on Linux — lift it once `python -c "import eccodes,
  pyproj"` exits 0 on a fresh install.
- **A bad basemap key doesn't error.** CARTO started requiring a key for
  raster basemaps in Sept 2026. Keyless requests return real tiles
  stamped "API KEY REQUIRED", and an invalid key returns the
  *byte-identical* watermarked tile — both as 200s, so Leaflet's
  `tileerror` never fires. A valid key from an unlisted referrer gets a
  403 instead, but with **no CORS headers**, so a page only ever sees a
  rejected fetch, indistinguishable from being offline. `index.html`
  therefore verifies the key by fetching one tile with and without it:
  keyless-loads-but-keyed-fails means refused here, identical bytes mean
  an invalid key, different bytes mean it works. Esri's dark gray basemap
  stands in otherwise, and on genuine tile errors at runtime.
  `CARTO_KEY` ships in page source by design and is restricted to
  `dmang.com`; CARTO's form rejects `localhost`, so local dev is refused
  and falls back to Esri automatically (spending no quota). The Esri
  fallback has the same trap one level down: data only to zoom 16, then a
  light-gray "Map data not yet available" tile as a 200 — hence
  `maxNativeZoom: 16`.
- The hosted demo's refresh button is disabled because GitHub Pages
  can't subprocess-spawn. Clone-and-run-locally is the live-refresh path.
- The HRRR bbox is set generously around CA; particles "escape" the box
  at very low zoom levels.
