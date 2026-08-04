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
data/                   Cached upstream responses (committed — see below)
mobile/                 Capacitor wrapper for Android/iOS
```

## Conventions

- **It's a PWA.** Service worker lives in `sw.js`. **Do not break
  offline mode** — if you change the cached file list, smoke-test
  offline in Lighthouse before pushing.
- **Desktop vs mobile.** Project root = desktop/PWA. `mobile/` =
  Capacitor app-store wrapper that points at the hosted PWA.
- **`data/` is regenerable but committed.** Anything in there is
  `fetch_data.py`'s output, so it's safe to delete and repopulate — but
  it is deliberately *not* gitignored: GitHub Pages has no runtime, so
  the committed snapshot *is* what the hosted demo serves.
  `refresh-data.yml` regenerates and commits it every 2 hours. A dirty
  `data/` diff after a local run is expected — commit it to update the
  demo, or `git restore data/` to drop it.
- **Fetchers are failure-isolated.** Each fetches one source and writes
  one file. A broken upstream must not prevent the others from updating.
- **Frontend is single-file.** `index.html` carries all the JS/CSS. No
  build step, no bundler, no npm at the root. Keep it that way.

## Pitfalls

- HRRR is forecast wind, not observed wind. RTMA would be observed (at
  the cost of more complex GRIB processing).
- **Perimeters lag the fire.** WFIGS only has a polygon once someone
  flies or walks the fire and a GIS specialist uploads it — a day or
  more, worst on CAL FIRE state-responsibility incidents, which don't
  feed the national pipeline as promptly as federal ones. A
  perimeter-only map shows *nothing* for those fires, which skews
  against exactly the new, fast-moving ones that matter most.
  `fetch_fire_points()` covers the gap with WFIGS incident points;
  `_has_perimeter: false` marks the ones the perimeter layer can't
  show. Acreage on those points is IRWIN's and runs stale — CAL FIRE's
  own API (`incidents.fire.ca.gov`) is fresher for state incidents but
  isn't wired up.
- Three CA utilities (LADWP, IID, PacifiCorp) don't publish to OES;
  their outage events will never appear no matter how robust the
  fetcher is.
- The hosted demo's refresh button is disabled because GitHub Pages
  can't subprocess-spawn. Clone-and-run-locally is the live-refresh path.
- The HRRR bbox is set generously around CA; particles "escape" the box
  at very low zoom levels.
