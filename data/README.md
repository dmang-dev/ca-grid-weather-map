# data/

Cached datasets fetched by `../fetch_data.py`. All files here are
regenerated on demand — **safe to delete the whole directory; `fetch_data.py`
will repopulate it.** In the GitHub Pages deploy, `deploy-pages.yml`
regenerates these every 2 hours and publishes them with the site.

## Files

| File | Source | Refreshed by |
|---|---|---|
| `wind.json` | NOAA HRRR 10m U/V via AWS S3 (forecast) | `fetch_wind()` |
| `raws.geojson` | RAWS station observations — wind, gusts, RH, temp (observed) | `fetch_raws()` |
| `outage_points.geojson` | California OES point layer (PG&E/SCE/SDG&E/SMUD) | `fetch_outages()` |
| `outage_areas.geojson` | California OES polygon layer (same query; upstream only publishes PG&E here) | `fetch_outages()` |
| `psps.geojson` | CPUC PSPS event history (365 days) | `fetch_psps()` |
| `nws_alerts.geojson` | api.weather.gov, clipped to CA | `fetch_alerts()` |
| `wildfires.geojson` | NIFC WFIGS perimeters (Living Atlas mirror when throttled), unioned with CAL FIRE/FIRIS IR heat perimeters; `_perimeter_source` says which | `fetch_fires()` |
| `fire_points.geojson` | WFIGS incident points, CA wildfires, overlaid with CAL FIRE acreage; `_has_perimeter` flags which ones the perimeter layer already draws | `fetch_fire_points()` |
| `calfire.geojson` | CAL FIRE incidents (`incidents.fire.ca.gov`) — fresher acreage/containment than IRWIN | `fetch_calfire()` |
| `evacuations.geojson` | Active evacuation zones (Genasys/Zonehaven via CAL FIRE) — orders, warnings, advisories | `fetch_evacuations()` |
| `inciweb.geojson` | InciWeb incident origins | `fetch_inciweb()` |
| `pge_territory.geojson` | CEC utility service territories (53 features) | `fetch_territory()` |
| `ca_counties.geojson` | CA county subdivisions (58 features) | `fetch_counties()` |
| `ca_boundary.geojson` | CA state polygon (fetched once, then reused) | `_get_ca_boundary()` |
| `manifest.json` | Per-fetch run summary used by the UI banner | every fetcher writes |

## Rules

- **Not in git.** `deploy-pages.yml` regenerates this directory on the
  runner and ships it inside the GitHub Pages artifact, so the hosted
  demo refreshes every 2 hours without the output entering history.
  (It used to be committed; 576 of those commits had taken the repo to
  459 MB.) Everything here except this README is gitignored — run
  `python fetch_data.py` to populate it locally.
- **Failure-isolated.** Each fetcher writes its file independently; one
  broken upstream doesn't prevent the others from updating. A fetcher
  whose upstream is down leaves the previous file untouched, and
  `manifest.json` marks that layer `stale` (with an `as_of` stamp)
  rather than `error` — the map keeps drawing the last-good snapshot.
- **`ca_boundary.geojson` is the one write-once file.** It's only
  fetched if missing, so the response is validated before it lands —
  caching an upstream error body there would poison NWS alerts on every
  later run. Delete it to force a re-fetch.
- **The refresh button in the UI** kicks `POST /refresh` on `serve.py`,
  which subprocess-shells `fetch_data.py`. Don't break that contract.
