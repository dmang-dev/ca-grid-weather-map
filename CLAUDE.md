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
```

## Conventions

- **It's a PWA.** Service worker lives in `sw.js`. **Do not break
  offline mode** — if you change the cached file list, smoke-test
  offline in Lighthouse before pushing.
- **Desktop vs mobile.** Project root = desktop/PWA. `mobile/` =
  Capacitor app-store wrapper that points at the hosted PWA.
- **`data/` is regenerable and never committed.** Anything in there is
  `fetch_data.py`'s output. `deploy-pages.yml` regenerates it on the
  runner and ships it inside the Pages artifact, so the hosted demo
  stays fresh without the output entering git history — committing it
  every 2 hours had taken the repo to 459 MB. Run `python fetch_data.py`
  to populate `data/` locally; only `data/README.md` is tracked.
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
- **ArcGIS can truncate without saying so.** RAWS holds 4,208 records
  and returns exactly 2,000 with `exceededTransferLimit` *absent* —
  the flag only shows up on page 2. Anything that might exceed
  `maxRecordCount` must go through `arcgis_query_all()`, which pages
  with `resultOffset`. Don't trust the flag to warn you.
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
  **alias hits must be confirmed geometrically** (40 km) before they're
  trusted — the primary key match doesn't need it, the alias does.
  FIRIS also re-uploads a fire as "<NAME>-UPDATED" now and then, which
  drew the same fire twice (RUMSEY vs RUMSEY-UPDATED, 0.01 km apart);
  `_FIRIS_REVISION_SUFFIX` folds that away before the latest-flight
  dedup. Only `-UPDATED` — a trailing *number* is a real sub-fire.
- **Evacuation zones are Zonehaven, not a CAL FIRE original.** Genasys's
  product is Zonehaven; CAL FIRE republishes it (plus zones some counties
  publish themselves — one current record comes from Tuolumne County's
  own ArcGIS, so don't filter on `state_abbr`, which is null there). The
  layer carries *only* non-Normal zones, so everything in it is live.
  `est_population` and `structure_count` are null on every record —
  don't build on them. `last_updated` is unreliable: some timestamps
  land hours in the future, so the UI never prints an age for a zone.
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
- The hosted demo's refresh button is disabled because GitHub Pages
  can't subprocess-spawn. Clone-and-run-locally is the live-refresh path.
- The HRRR bbox is set generously around CA; particles "escape" the box
  at very low zoom levels.
