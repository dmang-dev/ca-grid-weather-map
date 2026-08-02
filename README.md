# California Grid Weather Map

**Live demo (stale snapshot):** [dmang.com/ca-grid-weather-map](https://dmang.com/ca-grid-weather-map/)

Interactive map overlaying live NOAA HRRR 10m wind on California utility
service territories, active power outages, PSPS event footprints, NWS
fire/wind alerts, and active wildfire perimeters.

> The hosted demo's `data/` is auto-refreshed every 2 hours by a GitHub
> Actions workflow (`.github/workflows/refresh-data.yml`). It runs
> `fetch_data.py` on an Ubuntu runner, then commits the diff back to
> `main` if anything changed — GitHub Pages redeploys automatically.
> The refresh button in the UI is disabled on the hosted demo — it needs
> `serve.py` behind it, so it only enables when the page is served from
> localhost, a private-range LAN address, or a `.local` name. Clone and
> run `python serve.py` for live on-demand refreshes. To run a refresh
> manually right now, hit "Run workflow" in the Actions tab.

Built to answer the question: *what's blowing where, who's in the dark,
and is anything on fire?*

```
[ wind ] x [ outages ] x [ PSPS history ] x [ NWS alerts ] x [ wildfires ]
        all clipped to and colored by California's 53 electric utilities
```

## Features

- **Animated 10m wind** from NOAA HRRR (3 km, hourly) rendered as flowing
  particles via `leaflet-velocity`, color-mapped with Viridis (colorblind-safe)
- **Active power outages** across PG&E (polygon footprints), SCE / SDG&E / SMUD
  (point markers) — sourced from California OES
- **PSPS event footprints** from CPUC's official IOU PSPS layer, marked active
  vs. historical
- **NWS active alerts** — Red Flag Warning, Fire Weather Watch, Wind Advisory,
  High Wind Warning, etc. Polygons are reconstructed from UGC forecast-zone
  unions and clipped to the California state boundary
- **Active wildfire perimeters** from NIFC's WFIGS interagency aggregate, plus **InciWeb incident origin points** with links to the full incident pages (news releases, evacuation orders, photos, social media)
- **All 53 CA utility service territories** from CA Energy Commission, with
  the 8 major utilities (PG&E, SCE, SDG&E, LADWP, SMUD, IID, PacifiCorp,
  Liberty) styled distinctly in a Paul Tol bright (CVD-safe) palette
- **Cursor hover panel** reports county, utility (with annual GWh), sampled
  wind speed/direction, active alerts, outages affecting the point, PSPS
  status, and wildfire status
- **Refresh button** kicks off a server-side re-fetch of all datasets;
  page auto-reloads when done
- **WCAG 2.1 AA effort** — landmarks, ARIA labels, skip link, visible focus
  rings, `prefers-reduced-motion` support, no opacity-based dimming, contrast
  meets 4.5:1 against the dark UI

## Stack

| Layer       | Tech                                                 |
|-------------|------------------------------------------------------|
| Data fetch  | Python 3.11, [Herbie](https://herbie.readthedocs.io) (HRRR via AWS S3), `requests`, `shapely`, `xarray`/`cfgrib` |
| Server      | `http.server` subclass with a `POST /refresh` endpoint |
| Frontend    | Leaflet 1.9 + leaflet-velocity 2.1 + Turf.js 7 (CDN), no build step |
| Tiles       | Carto Dark Matter (OpenStreetMap)                    |

No build system. No npm. Open `index.html` directly behind the bundled HTTP
server and you're done.

## Quick start

Prereqs: **Python 3.11** (3.10+ probably works), Windows / macOS / Linux.

```bash
# 1. Clone
git clone https://github.com/dmang-dev/ca-grid-weather-map.git
cd ca-grid-weather-map

# 2. Create venv + install (one-time, ~5 minutes — eccodes is the heavy bit)
python3.11 -m venv .venv

# Pick the line matching your OS:
.venv\Scripts\python -m pip install -r requirements.txt   # Windows (PowerShell or cmd)
source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

# 3. Populate ./data with the first dataset fetch (~25 seconds)
python fetch_data.py        # works after `activate` on macOS/Linux
.venv\Scripts\python fetch_data.py   # Windows without activating

# 4. Start the local server (port 8000) — also handles POST /refresh
python serve.py
.venv\Scripts\python serve.py        # Windows alternative

# 5. Open the map
# http://localhost:8000/
```

Click the **↻ Refresh data** button at any time to re-pull every layer.

## Desktop app

If you'd rather have a native window than a browser tab:

```bash
pip install -r requirements-desktop.txt
python app.py
```

`app.py` runs the data fetcher once if `data/` is empty, starts the same
HTTP server on a free port, and opens a native window pointed at it via
[pywebview](https://pywebview.flowrl.com/). No Chromium bundle, no
Electron — uses the OS's own webview:

| OS      | Backend           | Install footprint |
|---------|-------------------|--------------------|
| Windows | Edge WebView2 (preinstalled on Win 10/11 22H2+) | ~10 MB (pythonnet) |
| macOS   | System WebKit                                    | ~5 MB              |
| Linux   | PyQt5 + QtWebEngine (via `pywebview[qt]`)        | ~70 MB             |

If pywebview can't load for some reason, `app.py` falls back to opening
the URL in your default browser.

## Mobile

The site is a **PWA** — mobile Safari and Chrome both support "Add to Home
Screen" / "Install app" against the live demo. That's the zero-friction
mobile install path: no app store, no native build, no signing.

For native shells, the `mobile/` directory has a [Capacitor](https://capacitorjs.com)
wrapper that points to the GitHub Pages URL. Two CI workflows build it:

| Workflow | Artifact | Use |
|---|---|---|
| `build-android.yml` | Unsigned debug APK | Side-load via `adb install`. **Not** Play Store — that needs a signing keystore and a separate release flow. |
| `build-ios.yml` | Unsigned simulator `.app` | Runs only in the iOS Simulator on a Mac. **Cannot** install on a real iPhone or ship through TestFlight / App Store without an Apple Developer Program account. |

Both download as workflow artifacts from the [Actions tab](https://github.com/dmang-dev/ca-grid-weather-map/actions).
The iOS job exists primarily as a compile-time regression test until/unless
real device distribution becomes a goal.

## File layout

```
fetch_data.py         All data fetchers (one function per source).
                      Each writes a file under data/ and is independently
                      failure-isolated. Re-run any time.
serve.py              Static-file server + POST /refresh subprocess shim.
index.html            The entire frontend. Single file, no build step.
requirements.txt      Python deps (Herbie + cfgrib + eccodes + xarray + shapely + requests).
data/                 Fetched datasets (gitignored, regenerated by fetch_data.py):
  wind.json             HRRR 10m U/V in leaflet-velocity grid format
  outage_points.geojson Active outages (points) — PGE/SCE/SDGE/SMUD
  outage_areas.geojson  Active outages (polygons) — PGE only
  psps.geojson          PSPS events, last 365 days, all CA IOUs
  nws_alerts.geojson    Active fire/wind NWS alerts, clipped to CA
  wildfires.geojson     NIFC active wildfire perimeters in CA
  inciweb.geojson       InciWeb incident origin points (CA bbox)
  pge_territory.geojson CEC utility service territories (53 features)
  ca_counties.geojson   California county polygons (58 features)
  ca_boundary.geojson   CA state polygon (used for alert clipping)
  manifest.json         Run summary for the banner
```

## Data sources

| Layer            | Source                                                 |
|------------------|--------------------------------------------------------|
| Wind (HRRR)      | NOAA via AWS Open Data (`noaa-hrrr-bdp-pds`)           |
| Outages          | California OES, *Power Outages (Public View)* ArcGIS feature service |
| PSPS events      | CPUC, *Consolidated PSPS Map* feature service          |
| NWS alerts       | api.weather.gov                                        |
| NWS zone polys   | api.weather.gov (`/zones/forecast/<UGC>`)              |
| Wildfires        | NIFC WFIGS, *Interagency Perimeters Current* (perimeter polygons) |
| Fire incidents   | InciWeb via the *Wildfire Aware Inciweb* community mirror (origin points + narrative metadata, links to inciweb.wildfire.gov) |
| Utility territories | California Energy Commission, *Electric Load Serving Entities (IOU & POU)* |
| Counties         | CA OES (county subdivision of outage layer)            |
| CA boundary      | Public ArcGIS service (cached on first fetch)          |

All data is from public US/CA government open-data feeds. Attributions
shown on the map and respected per source terms. Tiles &copy; OpenStreetMap
contributors &copy; CARTO.

## Limitations (honest list)

- **LADWP, IID, and PacifiCorp outages aren't visible.** They don't publish
  to the state-aggregated OES feed and instead use vendor-specific outage
  maps (mostly Kubra) with undocumented, fragile endpoints. Their territory
  outlines are shown, but outage events from them are not. PG&E participates
  in OES with polygon footprints; SCE/SDG&E/SMUD with points only.
- **Wind data is forecast, not observation.** HRRR is a numerical weather
  prediction model. For *observed* current wind, RTMA would be the better
  source — at the cost of slightly more complex GRIB processing.
- **Alert footprints are forecast zones, not "as drawn."** NWS forecasters
  may have a tighter mental polygon than the UGC zones we union — but the
  zone polygons are what's actually published.
- **PSPS feed is post-event filings.** The CPUC layer reflects events
  reported by IOUs after the fact. For real-time PSPS notification (e.g.
  during an active event), check the utility's own PSPS portal.
- **HRRR bbox edges are visible at very low zoom.** Particles "escape" the
  wind data bounding box and shoot in straight lines outside it. The bbox
  is set generously around CA so this happens off the visible map at
  normal zoom — but pan far enough out and you'll see it.

## Accessibility

Built to WCAG 2.1 AA targets:

- Semantic landmarks (`<main>`, `<header>`, `<aside>`, `role="region"`)
- Skip link, visible focus rings (`:focus-visible`)
- `aria-live="polite"` on the refresh status; deliberately *not* live on the
  hover panel (would spam screen readers on every mousemove)
- `prefers-reduced-motion` disables the animated wind particles; the hover
  panel still reports wind speed/direction in text
- All color encodings backed by either a non-color signal (dash patterns,
  text labels) or use a CVD-safe palette (Viridis for sequential wind speed,
  Paul Tol bright for qualitative utility colors)
- Tested mentally against NVDA's tab traversal; not yet user-tested with
  screen reader users — feedback welcome

## Adding a new data source

1. Add a `fetch_<thing>()` function in `fetch_data.py` that writes a JSON or
   GeoJSON to `data/<thing>.geojson`
2. Append it to `results` in `main()`
3. In `index.html`, add a `fetch('data/<thing>.geojson')` block that builds
   a Leaflet layer and registers it in `overlays`
4. If the layer should appear in the cursor hover panel, stash the GeoJSON
   in `HOVER.<thing>` and add a row plus a PIP block in `updateHover()`

Each fetcher is failure-isolated — if a new source breaks, the others still
populate.

## License

[MIT](LICENSE). Have fun.
