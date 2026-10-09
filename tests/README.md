# tests/

Offline unit tests for `fetch_data.py` and the deploy's seed step. Stdlib
`unittest` — no pytest, no new dependency, in keeping with the project's
no-build-step rule.

## Running

```bash
python -m unittest discover -s tests -t tests          # quiet
python -m unittest discover -s tests -t tests -v       # per-test names
python -m unittest discover -s tests -t tests -k gust  # one topic
```

140 tests, well under a second. Every test stubs the network, so this runs
with no upstream access and gives the same answer offline.

`fetch_data.py` only needs `numpy` and `requests` at import time — herbie,
cfgrib and shapely are imported lazily inside the fetchers that use them — so
the tests run without the heavy `eccodes` install. `.github/workflows/tests.yml`
relies on that; the full install is covered by `cross-platform-check.yml`.
The alert-zone and FIRIS-merge tests need `shapely` (`helpers.needs_shapely`):
`tests.yml` installs it, and anywhere it is missing those 27 skip rather than
fail. Because CI has shapely, `test_lazy_imports.py` blocks it (and herbie,
cfgrib and friends) in a fresh interpreter and imports `fetch_data` there, so
a module-scope import still fails the job. `test_wind.py` drives
`fetch_wind()` through a fake Herbie module, so it needs neither herbie nor a
GRIB file.

## The rule that matters

**Tests never write into the project's `data/`.** An earlier throwaway version
of this suite wrote its mock stubs into the real directory, and a 1-feature
`wildfires.geojson` got committed and served to the live site before anyone
noticed. `helpers.FetchDataTest` points `DATA_DIR` at a temp directory and
restores it afterwards; `tearDown` asserts nothing reassigned it.

Anything new that can write goes through that base class.

## What's covered, and why

| File | Covers |
|---|---|
| `test_arcgis_query.py` | Retry/backoff, in-body errors served as HTTP 200, and pagination |
| `test_fire_naming.py` | Cross-source fire identity: keys, complex aliases, revision suffixes, IRWIN ids, geometry |
| `test_fetchers.py` | Mirror failover, gust QC, and the empty/duplicate/short-result guards |
| `test_alerts.py` | Drawing NWS alerts from their `affectedZones`, counting the ones that can't be, and never fetching a URL off api.weather.gov |
| `test_wind.py` | HRRR grid-relative U/V rotated to earth-relative, and a run time that carries its UTC offset |
| `test_fire_points.py` | `_has_perimeter` name matches confirmed against the perimeter's extent, and the cold start with no perimeter file |
| `test_firis_union.py` | FIRIS perimeters that duplicate a WFIGS fire under another name: alias, overlap, umbrella, the cases that must stay separate, and the names and draw order a merge leaves behind |
| `test_lazy_imports.py` | `fetch_data` still imports with only numpy and requests installed |
| `test_sw_lists.py` | `sw.js` precaches exactly the unpkg libraries and `data/` files `index.html` loads, so offline launch can't silently lose one |
| `test_stale_fallback.py` | `run_one()`'s carry-forward, with and without a seeded snapshot |
| `test_seed_data.py` | The deploy's seed step — that it can never fail a build or keep a broken file |

Each case is here because the unguarded version shipped bad data at least
once. A few worth knowing:

- **Errors arrive as HTTP 200.** ArcGIS reports rate limiting in the response
  body, so `raise_for_status()` sails past it. That took the wildfire layer
  down and started this whole safety net.
- **Truncation is silent.** RAWS holds 4,208 records and returns exactly 2,000
  with `exceededTransferLimit` *absent* — the flag only shows up on page 2.
  `test_full_page_with_the_flag_absent_still_pages` pins that.
- **The aliasing rules pull against each other.** `CINDERCOMPLEX-5-3` must
  alias to `5-3`, while `RUMSEY-UPDATED` must fold to `RUMSEY` and
  `CINDERCOMPLEX-5-4` must *not* fold to `CINDERCOMPLEX-5`. Every rule has a
  negative case; it is easy to fix one and break the other.
- **A dropped upstream field must not be retried.** CEC removing
  `Sales_GWh_2023` made ArcGIS reject the whole query with a 400. Retrying a
  400 just burns the window — `test_bad_field_fails_fast_without_retrying`.
- **A failing fetcher should degrade, not disappear.** `test_stale_fallback.py`
  contrasts the two runners directly: without a seeded snapshot the layer 404s
  off the site; with one it goes amber and keeps serving.

## Adding a test

Subclass `helpers.FetchDataTest`, then use `self.stub_http(...)` for the
requests session, `mock.patch.object(fd, "arcgis_query", ...)` for a whole
query, and `self.write_data(...)` / `self.read_data(...)` for the isolated
`data/`. `self.slept` records backoff without ever waiting.
