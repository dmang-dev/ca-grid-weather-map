# data/

Cached datasets fetched by `../fetch_data.py`. All files here are
regenerated on demand — **safe to delete the whole directory; `fetch_data.py`
will repopulate it.** In the GitHub Pages deploy, a scheduled Actions
workflow regenerates these every 2 hours.

## Files

| File | Source | Refreshed by |
|---|---|---|
| `wind.json` | NOAA HRRR 10m U/V via AWS S3 | `fetch_wind()` |
| `outage_points.geojson` | California OES point layer (PG&E/SCE/SDG&E/SMUD) | `fetch_outages()` |
| `outage_areas.geojson` | California OES polygon layer (same query; upstream only publishes PG&E here) | `fetch_outages()` |
| `psps.geojson` | CPUC PSPS event history (365 days) | `fetch_psps()` |
| `nws_alerts.geojson` | api.weather.gov, clipped to CA | `fetch_alerts()` |
| `wildfires.geojson` | NIFC WFIGS perimeters, or the Living Atlas mirror when WFIGS is throttled | `fetch_fires()` |
| `inciweb.geojson` | InciWeb incident origins | `fetch_inciweb()` |
| `pge_territory.geojson` | CEC utility service territories (53 features) | `fetch_territory()` |
| `ca_counties.geojson` | CA county subdivisions (58 features) | `fetch_counties()` |
| `ca_boundary.geojson` | CA state polygon (fetched once, then reused) | `_get_ca_boundary()` |
| `manifest.json` | Per-fetch run summary used by the UI banner | every fetcher writes |

## Rules

- **In git, on purpose.** Regenerable, but not gitignored: GitHub Pages
  has no runtime, so the committed snapshot *is* what the hosted demo
  serves. `refresh-data.yml` commits a fresh one every 2 hours. A dirty
  `data/` diff after a local run is expected — commit it to update the
  demo, or `git restore data/` to drop it.
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
