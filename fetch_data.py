"""
Fetch three datasets and write them into ./data/ for the Leaflet map:

  1. HRRR 10m wind (U, V)  -> data/wind.json       (leaflet-velocity format)
  2. CA OES power outages  -> data/outages.geojson (filtered to PGE)
  3. CPUC PSPS event map   -> data/psps.geojson    (filtered to PGE, recent)

Re-run any time. Each fetcher fails independently so a flaky upstream
does not kill the others. A run summary is written to data/manifest.json.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Force UTF-8 stdout — Herbie prints box-drawing characters on first import
# which crash on Windows' default cp1252 console encoding.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
import requests

# -----------------------------------------------------------------------------
# Config — PG&E service territory bounding box (roughly N/Central California).
# Used to clip HRRR. Outage and PSPS data are filtered server-side by utility.
# -----------------------------------------------------------------------------
PGE_BBOX = {
    # Wider than CA so wind-particle edges fall offscreen at typical zoom.
    # CA spans ~32.5-42N and ~-124.4 to -114.1W; buffer on every side.
    "lat_min": 31.5,
    "lat_max": 44.5,
    "lon_min": -128.5,
    "lon_max": -113.5,
}
PSPS_LOOKBACK_DAYS = 365  # show PG&E PSPS events from the last year
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "pge-weather-map/1.0 (+local research)"})

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


# =============================================================================
# ArcGIS query helper — the 200-with-an-error-body trap
# =============================================================================
# Every public ArcGIS Online layer we read (NIFC, CA OES, CPUC, CEC) reports
# rate limiting as HTTP 200 with an {"error": {"code": 429, ...}} body, so
# raise_for_status() sails straight past it. Those quotas are org-wide and
# shared with every other consumer on the internet, so a 429 says nothing about
# our request being wrong — it is transient, and worth retrying.
ARCGIS_TRANSIENT_CODES = {429, 500, 502, 503, 504}


class ArcGISError(RuntimeError):
    """An ArcGIS REST error, whether it arrived as an HTTP status or a JSON body."""

    def __init__(self, message: str, code=None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


def arcgis_query(url: str, params: dict, *, what: str, timeout: int = 60,
                 attempts: int = 3, max_sleep: float = 20.0) -> dict:
    """GET an ArcGIS FeatureServer query and return the parsed GeoJSON.

    Retries transient failures (connection errors, 5xx, quota 429s) with capped
    backoff. The cap is deliberately well under the 60 s ArcGIS quota window:
    a caller with a mirror is better off failing over than waiting it out.
    """
    exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = HTTP.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            gj = r.json()
            err = gj.get("error") if isinstance(gj, dict) else None
            if err:
                code = err.get("code")
                detail = "; ".join(err.get("details") or []) or err.get("message") or ""
                raise ArcGISError(f"{what}: ArcGIS error {code}: {detail}",
                                  code=code, transient=code in ARCGIS_TRANSIENT_CODES)
            if not isinstance(gj, dict) or "features" not in gj:
                raise ArcGISError(f"{what}: no 'features' in response: {str(gj)[:200]}")
            if gj.get("exceededTransferLimit"):
                # Not fatal — but the counts we report would be short, so say so
                # rather than letting a truncated layer look complete.
                print(f"  [{what}] WARNING: hit the server's max record count; "
                      f"result truncated at {len(gj['features'])} features")
            return gj
        except ArcGISError as e:
            transient, exc = e.transient, e
        except (requests.RequestException, ValueError) as e:
            # ValueError covers JSONDecodeError — usually an HTML error page.
            transient, exc = True, e
        if not transient or attempt == attempts:
            raise exc
        delay = min(max_sleep, 5.0 * 2 ** (attempt - 1))
        print(f"  [{what}] transient failure, retrying in {delay:.0f}s: {exc}")
        time.sleep(delay)
    raise exc  # unreachable; keeps type checkers happy


def arcgis_query_all(url: str, params: dict, *, what: str, page_size: int = 2000,
                     max_pages: int = 20, **kwargs) -> dict:
    """Page through a query whose result exceeds the server's maxRecordCount.

    Do not trust exceededTransferLimit to tell you when this is needed: the
    RAWS layer returns exactly 2000 features with the flag *absent* while
    holding 4208, so a single query silently drops half the stations.
    """
    collected: list[dict] = []
    gj: dict = {"type": "FeatureCollection", "features": []}
    for page in range(max_pages):
        gj = arcgis_query(url, {**params, "resultOffset": page * page_size,
                                "resultRecordCount": page_size},
                          what=f"{what} p{page + 1}", **kwargs)
        batch = gj["features"]
        collected.extend(batch)
        if len(batch) < page_size:
            break
    else:
        print(f"  [{what}] WARNING: stopped at {max_pages} pages "
              f"({len(collected)} features); results may be incomplete")
    gj["features"] = collected
    return gj


# =============================================================================
# 1. HRRR wind
# =============================================================================
def _grid_to_earth_winds(u, v, lon, lov: float, latin1: float, latin2: float):
    """Rotate grid-relative U/V on a Lambert conformal grid to eastward/northward.

    The grid's y axis points at the cone's apex above LoV, not at the pole, so
    away from LoV it leans off true north by n * (lon - LoV): the meridian
    convergence, with n the cone constant. Rotation preserves speed.
    """
    phi1, phi2 = math.radians(latin1), math.radians(latin2)
    if math.isclose(phi1, phi2):
        n = math.sin(phi1)  # tangent cone, as HRRR's is
    else:
        n = (math.log(math.cos(phi1) / math.cos(phi2))
             / math.log(math.tan(math.pi / 4 + phi2 / 2) / math.tan(math.pi / 4 + phi1 / 2)))
    ang = np.radians(n * ((np.asarray(lon) - lov + 180.0) % 360.0 - 180.0))
    cos, sin = np.cos(ang), np.sin(ang)
    return cos * u + sin * v, -sin * u + cos * v


def _earth_relative_winds(u, v, lon, attrs):
    """U/V as eastward/northward components, rotated if the GRIB says they're not.

    HRRR's 10 m winds are relative to its Lambert grid (uvRelativeToGrid=1),
    and neither Herbie nor cfgrib rotates them. Written out as-is, every arrow
    over California leaned 10-17 degrees clockwise (SF 15.5, LA 12.9, Needles
    10.6, Crescent City 16.6) and the hover panel's HRRR direction disagreed
    with the RAWS station beside it. If the attributes can't say which way the
    vectors point, raise: run_one then keeps the last good wind.json and the
    deploy flags it, which beats publishing a field that is silently rotated.
    """
    flag = attrs.get("GRIB_uvRelativeToGrid")
    if flag == 0:
        return u, v
    params = [attrs.get(k) for k in ("GRIB_LoVInDegrees", "GRIB_Latin1InDegrees",
                                     "GRIB_Latin2InDegrees")]
    if flag != 1 or attrs.get("GRIB_gridType") != "lambert" or None in params:
        raise RuntimeError(
            f"HRRR U/V orientation unknown (uvRelativeToGrid={flag!r}, "
            f"gridType={attrs.get('GRIB_gridType')!r}, LoV/Latin1/Latin2={params}); "
            f"refusing to publish winds that may be rotated")
    return _grid_to_earth_winds(u, v, lon, *(float(x) for x in params))


def fetch_wind() -> dict:
    """Pull latest HRRR sfc 10m U/V wind, clip to PG&E bbox, write velocity JSON."""
    from herbie import Herbie  # imported lazily so other fetchers still run if missing

    # Walk back hour-by-hour until we find a HRRR run with the f00 sfc file
    # already present on AWS (typically 1-2 hours of latency).
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    H = None
    last_err = None
    for hours_back in range(1, 8):
        run = now - timedelta(hours=hours_back)
        try:
            candidate = Herbie(run.strftime("%Y-%m-%d %H:%M"), model="hrrr",
                               product="sfc", fxx=0, priority=["aws"], verbose=False)
            if candidate.grib is not None:
                H = candidate
                print(f"  HRRR run found: {run.isoformat()} ({hours_back}h back)")
                break
        except Exception as e:
            last_err = e
    if H is None:
        raise RuntimeError(f"No HRRR run available in last 8h: {last_err}")

    # xarray dataset for U/V at 10m above ground
    ds = H.xarray(":[UV]GRD:10 m above ground", remove_grib=False)
    if isinstance(ds, list):
        # Herbie sometimes returns a list when multiple grib messages match;
        # merge by taking the first U and first V.
        u_ds = next(d for d in ds if "u10" in d.data_vars)
        v_ds = next(d for d in ds if "v10" in d.data_vars)
    else:
        u_ds = ds
        v_ds = ds

    u = u_ds["u10"]
    v = v_ds["v10"]

    # HRRR is on a Lambert grid; xarray exposes lat/lon as 2D coords.
    # leaflet-velocity wants a regular lat/lon grid, so we resample to one.
    lat2d = u["latitude"].values  # shape (ny, nx)
    lon2d = u["longitude"].values
    # HRRR longitudes are 0-360; normalize to -180..180
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
    # Earth-relative before anything else: the rotation depends on each native
    # point's longitude, which the resample below throws away.
    u_vals, v_vals = _earth_relative_winds(u.values, v.values, lon2d, u.attrs)

    bbox = PGE_BBOX
    # Build a regular target grid over PG&E bbox at ~0.05° (~5km, close to HRRR native)
    step = 0.05
    target_lats = np.arange(bbox["lat_max"], bbox["lat_min"] - 1e-9, -step)
    target_lons = np.arange(bbox["lon_min"], bbox["lon_max"] + 1e-9, step)

    # Nearest-neighbor resample from HRRR native to regular grid.
    # For each target cell, find the nearest HRRR grid point. We do this with
    # a flat KD-style lookup approximated by indexing into a coarse mask first,
    # then taking argmin over the small subset — fast enough for ~10k cells.
    out_u = np.full((len(target_lats), len(target_lons)), np.nan, dtype=np.float32)
    out_v = np.full_like(out_u, np.nan)

    # Subset HRRR points to the bbox first (huge speedup)
    in_bbox = (
        (lat2d >= bbox["lat_min"] - 0.5) & (lat2d <= bbox["lat_max"] + 0.5) &
        (lon2d >= bbox["lon_min"] - 0.5) & (lon2d <= bbox["lon_max"] + 0.5)
    )
    src_lats = lat2d[in_bbox]
    src_lons = lon2d[in_bbox]
    src_u = u_vals[in_bbox]
    src_v = v_vals[in_bbox]
    print(f"  HRRR points in PG&E bbox: {src_lats.size}")
    if src_lats.size == 0:
        raise RuntimeError("HRRR bbox intersection empty — check coordinates")

    for i, la in enumerate(target_lats):
        # Coarse latitude filter (~0.1°) so the argmin runs over a thin band
        band = np.abs(src_lats - la) < 0.1
        if not band.any():
            band = np.abs(src_lats - la) < 0.5
        b_lats = src_lats[band]
        b_lons = src_lons[band]
        b_u = src_u[band]
        b_v = src_v[band]
        for j, lo in enumerate(target_lons):
            d2 = (b_lats - la) ** 2 + (b_lons - lo) ** 2
            k = int(d2.argmin())
            out_u[i, j] = b_u[k]
            out_v[i, j] = b_v[k]

    # leaflet-velocity expects two records (U then V) with a shared header.
    # Data order is rows from north to south, west to east, flattened.
    def velocity_record(component_short: str, name: str, data: np.ndarray) -> dict:
        return {
            "header": {
                "parameterUnit": "m.s-1",
                "parameterNumber": 2 if component_short == "U" else 3,
                "parameterNumberName": name,
                "parameterCategory": 2,
                "discipline": 0,
                "nx": int(data.shape[1]),
                "ny": int(data.shape[0]),
                "lo1": float(target_lons[0]),
                "la1": float(target_lats[0]),
                "lo2": float(target_lons[-1]),
                "la2": float(target_lats[-1]),
                "dx": float(step),
                "dy": float(step),
                "refTime": H.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "forecastTime": 0,
            },
            "data": np.nan_to_num(data, nan=0.0).round(2).flatten().tolist(),
        }

    velocity = [
        velocity_record("U", "eastward_wind", out_u),
        velocity_record("V", "northward_wind", out_v),
    ]

    (DATA_DIR / "wind.json").write_text(json.dumps(velocity))
    return {
        # H.date is naive UTC. isoformat() left the offset off, and browsers
        # read an offset-less date-time as local, so the banner put the run
        # 7-8 h late in California. Say Z, as refTime above does.
        "run": H.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grid": [int(out_u.shape[0]), int(out_u.shape[1])],
        "bbox": bbox,
    }


# =============================================================================
# 2. CA OES power outages (point + polygon layers, filtered to PG&E)
# =============================================================================
OES_BASE = ("https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/"
            "Power_Outages_(View)/FeatureServer")


def fetch_outages() -> dict:
    summary = {}
    for layer_id, name in [(0, "outage_points"), (1, "outage_areas")]:
        url = f"{OES_BASE}/{layer_id}/query"
        params = {
            # OES carries PGE/SCE/SDGE/SMUD. LADWP, IID, and PacifiCorp do
            # not publish to the state aggregator — their outages won't show.
            "where": "UtilityCompany IN ('PGE','SCE','SDGE','SMUD')",
            "outFields": "*",
            "outSR": 4326,
            "f": "geojson",
        }
        gj = arcgis_query(url, params, what=f"OES layer {layer_id}")
        (DATA_DIR / f"{name}.geojson").write_text(json.dumps(gj))
        summary[name] = len(gj["features"])
    return summary


# =============================================================================
# 3. CPUC PSPS event polygons (filtered to PG&E, recent)
# =============================================================================
PSPS_BASE = ("https://services2.arcgis.com/VofPZYDe2pLxSP5G/arcgis/rest/services/"
             "Consolidated_PSPS_Map20221231_gdb/FeatureServer/0")


def fetch_psps() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=PSPS_LOOKBACK_DAYS)
    where = (
        f"IOU IN ('PGE','SCE','SDGE') AND "
        f"FirstDateofPOC >= TIMESTAMP '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'"
    )
    params = {
        "where": where,
        "outFields": "*",
        "outSR": 4326,
        "f": "geojson",
    }
    gj = arcgis_query(f"{PSPS_BASE}/query", params, what="PSPS")

    # Tag each feature with active/historical based on FullRestorationDate.
    now_ms = int(time.time() * 1000)
    active = 0
    for f in gj["features"]:
        p = f["properties"]
        end = p.get("FullRestorationDate")
        is_active = end is None or (isinstance(end, (int, float)) and end > now_ms)
        p["_is_active"] = bool(is_active)
        if is_active:
            active += 1

    (DATA_DIR / "psps.geojson").write_text(json.dumps(gj))
    return {
        "events_total": len(gj["features"]),
        "events_active": active,
        "lookback_days": PSPS_LOOKBACK_DAYS,
    }


# =============================================================================
# 4. NWS active alerts — Red Flag, Fire Weather Watch, Wind Advisories
# =============================================================================
NWS_HEADERS = {
    "User-Agent": "pge-weather-map (research; contact: local user)",
    "Accept": "application/geo+json",
}
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
# Fallback only, for an alert that carries no affectedZones. A UGC code doesn't
# say which kind of zone it is, and Red Flag Warnings and Fire Weather Watches
# are issued on *fire* weather zones (CAZ270-285, NVZ4xx) that 404 here.
NWS_ZONE_URL = "https://api.weather.gov/zones/forecast/{zone}"
# affectedZones arrives in the feed, so it is never fetched as given: only an
# api.weather.gov zone path passes, rebuilt from its parts.
_NWS_ZONE_PATH = re.compile(r"/zones/([a-z]+)/([A-Za-z0-9]+)/?")
CA_BOUNDARY_URL = (
    "https://services3.arcgis.com/fdvHcZVgB2QSRNkL/arcgis/rest/services/"
    "State_Boundary/FeatureServer/0/query"
)
CA_BOUNDARY_FILE = DATA_DIR / "ca_boundary.geojson"


def _get_ca_boundary():
    """Return shapely geometry of California, fetched once and cached on disk."""
    from shapely.geometry import shape
    if not CA_BOUNDARY_FILE.exists():
        print("  fetching CA state boundary (one-time)...")
        # Validate before writing: caching a rate-limit error body here would
        # poison this one-time file permanently.
        gj = arcgis_query(CA_BOUNDARY_URL, {
            "where": "1=1", "outFields": "State", "outSR": 4326, "f": "geojson",
        }, what="CA boundary")
        if not gj["features"]:
            raise RuntimeError("CA boundary query returned no features")
        CA_BOUNDARY_FILE.write_text(json.dumps(gj))
    gj = json.loads(CA_BOUNDARY_FILE.read_text())
    return shape(gj["features"][0]["geometry"])
ALERT_EVENTS = {
    "Red Flag Warning",
    "Fire Weather Watch",
    "Wind Advisory",
    "Lake Wind Advisory",
    "High Wind Warning",
    "High Wind Watch",
    "Extreme Wind Warning",
}
# Zone geometry by URL, so a zone shared by several alerts is fetched once.
_zone_cache: dict[str, dict | None] = {}


def _nws_zone_url(url) -> str | None:
    """Canonical URL for an api.weather.gov zone, or None for anything else."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        # "https://[api.weather.gov/..." — one malformed entry must not fail
        # the whole layer.
        return None
    m = _NWS_ZONE_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc.lower() != "api.weather.gov"
            or parts.query or parts.fragment or not m):
        return None
    return f"https://api.weather.gov/zones/{m[1]}/{m[2]}"


def _alert_zone_urls(props: dict) -> list[str]:
    """Zone URLs to draw an alert that came without a polygon.

    affectedZones names each zone with its type (/zones/fire/CAZ270), which the
    bare UGC code can't; the UGC fallback assumes a forecast zone.
    """
    listed = props.get("affectedZones")
    if not listed:
        listed = [NWS_ZONE_URL.format(zone=z)
                  for z in (props.get("geocode") or {}).get("UGC", [])]
    urls: list[str] = []
    for raw in listed:
        url = _nws_zone_url(raw)
        if url is None:
            print(f"    ignoring zone reference that isn't an NWS zone: {str(raw)[:120]}")
        elif url not in urls:
            urls.append(url)
    return urls


def _fetch_zone(url: str) -> dict | None:
    """Return GeoJSON geometry for an NWS zone URL, or None on failure."""
    if url in _zone_cache:
        return _zone_cache[url]
    try:
        r = HTTP.get(url, headers=NWS_HEADERS, timeout=20)
        r.raise_for_status()
        geom = r.json().get("geometry")
    except Exception as e:
        print(f"    zone {url}: {e}")
        geom = None
    _zone_cache[url] = geom
    return geom


def fetch_alerts() -> dict:
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union

    ca = _get_ca_boundary()

    r = HTTP.get(NWS_ALERTS_URL, params={"area": "CA"}, headers=NWS_HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.json().get("features", [])

    # Filter to event types of interest, deduplicating by alert id.
    seen = set()
    alerts = []
    for f in raw:
        p = f.get("properties", {})
        if p.get("event") not in ALERT_EVENTS:
            continue
        aid = p.get("id")
        if aid in seen:
            continue
        seen.add(aid)
        alerts.append(f)
    print(f"  matched {len(alerts)} relevant alerts of {len(raw)} active CA alerts")

    # Collect zones from the alerts that need them.
    zone_urls = [[] if f.get("geometry") else _alert_zone_urls(f["properties"])
                 for f in alerts]
    needed = {u for urls in zone_urls for u in urls}
    print(f"  fetching {len(needed)} zone polygons")
    for url in sorted(needed):
        _fetch_zone(url)

    # Build per-alert features with merged geometry clipped to California.
    out_features = []
    dropped_out_of_state = 0
    no_geometry: list[str] = []
    for f, urls in zip(alerts, zone_urls):
        p = f["properties"]
        geom_dict = f.get("geometry")
        if geom_dict:
            merged = shape(geom_dict)
        else:
            zone_geoms = []
            for url in urls:
                g = _zone_cache.get(url)
                if g:
                    try:
                        zone_geoms.append(shape(g))
                    except Exception:
                        pass
            if not zone_geoms:
                # Nothing to draw. This used to drop the alert without a trace:
                # the Oct 8-9 2026 deploys lost 3 of 5 matched alerts this way,
                # all issued on fire weather zones, and still reported ok.
                no_geometry.append(p.get("event") or "unknown event")
                continue
            merged = unary_union(zone_geoms)

        # Clip to CA — drops zone slivers that spill into NV/OR/AZ.
        clipped = merged.intersection(ca)
        if clipped.is_empty or clipped.area < 1e-6:
            dropped_out_of_state += 1
            continue
        geom = mapping(clipped)

        # Trim properties — full NWS payload is bulky and most isn't useful on a map.
        out_features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "event": p.get("event"),
                "headline": p.get("headline"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "certainty": p.get("certainty"),
                "areaDesc": p.get("areaDesc"),
                "senderName": p.get("senderName"),
                "effective": p.get("effective"),
                "onset": p.get("onset"),
                "expires": p.get("expires"),
                "ends": p.get("ends"),
                "description": (p.get("description") or "")[:1500],
                "instruction": (p.get("instruction") or "")[:600],
            },
        })

    out = {"type": "FeatureCollection", "features": out_features}
    (DATA_DIR / "nws_alerts.geojson").write_text(json.dumps(out))

    counts = {}
    for f in out_features:
        e = f["properties"]["event"]
        counts[e] = counts.get(e, 0) + 1
    summary = {
        "alerts_total": len(out_features),
        "by_event": counts,
        "dropped_out_of_state": dropped_out_of_state,
        "dropped_no_geometry": len(no_geometry),
    }
    if no_geometry:
        # The layer is still ok, so the deploy step reads this list to say so.
        summary["warnings"] = [
            f"{len(no_geometry)} of {len(alerts)} matched alerts not drawn: no zone "
            f"geometry could be resolved ({', '.join(sorted(set(no_geometry)))})"]
    return summary


# =============================================================================
# 6. Active wildfire perimeters in California (NIFC WFIGS aggregate)
# =============================================================================
FIRES_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
             "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")

# NIFC's own service is the busiest wildfire layer on ArcGIS Online and spends
# real stretches of fire season over its per-minute quota (HTTP 200, body says
# 429). Esri's Living Atlas republishes the same NIFC feed from a different
# org — different quota — so it stays up when the origin is throttled.
# Layer 1 = perimeters (geometry), layer 0 = incident points (the containment,
# cause, and county attributes that WFIGS bundles into one layer).
FIRES_MIRROR_BASE = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
                     "USA_Wildfires_v1/FeatureServer")
# The mirror's perimeter layer has no state field, so California is selected by
# envelope. Tight to the state — not PGE_BBOX, which is padded far out to sea
# and into Nevada for the wind grid.
CA_ENVELOPE = "-124.6,32.4,-114.0,42.1"
# ONCC/OSCC are the two California geographic area coordination centers, used
# to keep CA perimeters whose IRWIN id doesn't join to an incident point.
CA_GACCS = {"ONCC", "OSCC"}


def _irwin(value) -> str:
    """Normalize an IRWIN id — the mirror's two layers disagree on braces/case."""
    return (value or "").strip("{}").lower()


def _fire_key(name) -> str:
    """Normalize a fire name for cross-source matching.

    CAL FIRE writes "Gann Fire", WFIGS writes "GANN". Strip the trailing noun
    and punctuation so the two line up.
    """
    n = re.sub(r"\s+(FIRE|INCIDENT|COMPLEX)$", "", (name or "").upper().strip())
    return re.sub(r"[^A-Z0-9 ]", "", n).strip()


def _iso_to_ms(value):
    """CAL FIRE ISO timestamp -> epoch ms, matching ArcGIS's date encoding."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _fire_keys(name) -> list[str]:
    """Match keys for a fire name, most specific first.

    FIRIS names a complex's sub-fires "CINDERCOMPLEX-5-3" where WFIGS calls the
    same fire "5-3", so a complex-prefixed name also answers to its sub-
    designation. That alias can be as short as "53", which is far too generic
    to trust on its own — callers must confirm alias hits geometrically.
    """
    base = _fire_key(name)
    keys = [base] if base else []
    head, sep, tail = (name or "").upper().strip().partition("-")
    if sep and head.replace(" ", "").endswith("COMPLEX"):
        alias = _fire_key(tail)
        if alias and alias not in keys:
            keys.append(alias)
    return keys


def _geom_bbox(geometry) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon) of any GeoJSON geometry."""
    if not geometry:
        return None
    lons: list[float] = []
    lats: list[float] = []

    def walk(c):
        if isinstance(c, (int, float)):
            return
        if len(c) >= 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
            lons.append(c[0])
            lats.append(c[1])
            return
        for part in c:
            walk(part)

    walk(geometry.get("coordinates") or [])
    if not lons:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def _geom_center(geometry) -> tuple[float, float] | None:
    """Rough (lat, lon) centre of any GeoJSON geometry, for sanity checks."""
    box = _geom_bbox(geometry)
    if not box:
        return None
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _km_apart(lat1, lon1, lat2, lon2) -> float:
    """Rough great-circle distance in km — fine at the scale we compare at."""
    return math.hypot((lat1 - lat2) * 111.0,
                      (lon1 - lon2) * 111.0 * math.cos(math.radians(lat1)))


def _km_outside(lat, lon, box) -> float:
    """Rough km from (lat, lon) to a _geom_bbox() box; 0 anywhere inside it."""
    return _km_apart(lat, lon, min(max(lat, box[0]), box[2]), min(max(lon, box[1]), box[3]))


# How close a name match that needs confirming — a complex-name alias like
# "53" — has to land before it is trusted. Generous: an origin point can sit
# well outside a grown perimeter. A point is measured to a perimeter's extent
# (_km_outside), not its centre: a Dixie-sized fire's origin sits ~50 km from
# the middle of its own box.
ALIAS_CONFIRM_KM = 40


def _fires_from_wfigs() -> dict:
    return arcgis_query(FIRES_URL, {
        "where": "attr_POOState='US-CA'",
        "outFields": ("poly_IncidentName,poly_GISAcres,poly_DateCurrent,"
                      "attr_IncidentName,attr_PercentContained,attr_FireDiscoveryDateTime,"
                      "attr_FireCause,attr_IncidentTypeCategory,attr_POOCounty,attr_IrwinID"),
        "outSR": 4326,
        "f": "geojson",
    }, what="fires (WFIGS)")


def _fires_from_mirror() -> dict:
    """Rebuild the WFIGS feature schema from the Living Atlas republication.

    Emits the same poly_*/attr_* property names the frontend already reads, so
    a failover is invisible to the map.
    """
    perims = arcgis_query(f"{FIRES_MIRROR_BASE}/1/query", {
        "where": "1=1",
        "geometry": CA_ENVELOPE,
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "IncidentName,GISAcres,DateCurrent,IncidentTypeCategory,IRWINID,GACC",
        "outSR": 4326,
        "f": "geojson",
    }, what="fires mirror (perimeters)")

    # Best-effort attribute join: perimeters with thin popups still beat no
    # wildfire layer at all.
    attrs: dict[str, dict] = {}
    try:
        pts = arcgis_query(f"{FIRES_MIRROR_BASE}/0/query", {
            "where": "POOState='US-CA'",
            "outFields": ("IrwinID,IncidentName,PercentContained,FireCause,"
                          "FireDiscoveryDateTime,POOCounty,IncidentTypeCategory"),
            "outSR": 4326,
            "f": "geojson",
        }, what="fires mirror (incidents)")
        attrs = {_irwin(f["properties"].get("IrwinID")): f["properties"]
                 for f in pts["features"] if _irwin(f["properties"].get("IrwinID"))}
    except Exception as e:
        print(f"  [fires] mirror incident points unavailable, perimeters only: {e}")

    out = []
    for f in perims["features"]:
        p = f["properties"]
        a = attrs.get(_irwin(p.get("IRWINID"))) or {}
        # The envelope is a rectangle, so it also catches Nevada and Arizona.
        # An incident-point match means POOState='US-CA'; without one, fall back
        # to the California coordination centers.
        if not a and p.get("GACC") not in CA_GACCS:
            continue
        out.append({
            "type": "Feature",
            "geometry": f.get("geometry"),
            "properties": {
                "poly_IncidentName": p.get("IncidentName"),
                "poly_GISAcres": p.get("GISAcres"),
                "poly_DateCurrent": p.get("DateCurrent"),
                "attr_IncidentName": a.get("IncidentName") or p.get("IncidentName"),
                "attr_PercentContained": a.get("PercentContained"),
                "attr_FireDiscoveryDateTime": a.get("FireDiscoveryDateTime"),
                "attr_FireCause": a.get("FireCause"),
                "attr_IncidentTypeCategory": (a.get("IncidentTypeCategory")
                                              or p.get("IncidentTypeCategory")),
                "attr_POOCounty": a.get("POOCounty"),
                # Normalized so the incident-point layer can tell which fires
                # already have a perimeter drawn, whichever source we used.
                "attr_IrwinID": _irwin(p.get("IRWINID")) or None,
            },
        })
    return {"type": "FeatureCollection", "features": out}


# CAL FIRE's own incident maps don't draw WFIGS perimeters — they draw this,
# which is why a fire can show a polygon on fire.ca.gov and nothing here.
# FIRIS is California's Fire Integrated Real-Time Intelligence System: state
# aircraft flying IR sensors that produce "heat perimeters" within hours,
# instead of waiting on the ground-mapping-and-upload cycle WFIGS depends on.
# The layer blends FIRIS, CAL FIRE intel flights, NIFC/WFIGS, USFS, and county
# sources, and carries several missions per fire — one row per flight.
FIRIS_URL = ("https://bz1uwwpkuinzbk94.svcs5.arcgis.com/bz1uwWPKUInZBK94/arcgis/rest/"
             "services/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0/query")
# Long enough to keep a fire whose last IR flight was a while ago, short enough
# not to redraw the whole season.
FIRIS_MAX_AGE_DAYS = 21
_FIRIS_REVISION_SUFFIX = re.compile(r"\s*-\s*UPDATED\s*$", re.I)


def _fires_from_firis() -> list[dict]:
    """Latest heat perimeter per incident, in the WFIGS feature schema."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=FIRIS_MAX_AGE_DAYS)
    gj = arcgis_query(FIRIS_URL, {
        "where": f"poly_DateCurrent >= TIMESTAMP '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'",
        "outFields": ("incident_name,source,area_acres,NIFC_GISAcres,poly_DateCurrent,"
                      "Percent_Contained,FireDiscoveryDate"),
        "outSR": 4326,
        "f": "geojson",
    }, what="fires (FIRIS)")

    latest: dict[str, dict] = {}
    for f in gj["features"]:
        p = f["properties"]
        # FIRIS sometimes re-uploads a fire under a "-UPDATED" name, which then
        # draws twice: RUMSEY and RUMSEY-UPDATED sat 0.01 km apart at 396 and
        # 380 acres. Fold the suffix away so the latest-flight rule below keeps
        # only the fresher one. Strictly -UPDATED — a trailing number is a real
        # sub-fire (CINDERCOMPLEX-5-3), not a revision marker.
        p["incident_name"] = _FIRIS_REVISION_SUFFIX.sub(
            "", (p.get("incident_name") or "").strip())
        key = _fire_key(p.get("incident_name"))
        # The layer carries unnamed rows literally called "NONE".
        if not key or key == "NONE" or not f.get("geometry"):
            continue
        prev = latest.get(key)
        if prev is None or (p.get("poly_DateCurrent") or 0) > (prev["properties"].get("poly_DateCurrent") or 0):
            latest[key] = f

    out = []
    for key, f in latest.items():
        p = f["properties"]
        out.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "poly_IncidentName": p.get("incident_name"),
                "poly_GISAcres": p.get("area_acres") if p.get("area_acres") is not None
                                 else p.get("NIFC_GISAcres"),
                "poly_DateCurrent": p.get("poly_DateCurrent"),
                "attr_IncidentName": p.get("incident_name"),
                "attr_PercentContained": p.get("Percent_Contained"),
                "attr_FireDiscoveryDateTime": p.get("FireDiscoveryDate"),
                "attr_FireCause": None,
                "attr_IncidentTypeCategory": "WF",
                "attr_POOCounty": None,
                "attr_IrwinID": None,
                # Provenance, so the popup can say where the shape came from.
                "_perimeter_source": p.get("source") or "FIRIS",
            },
        })
    return out


def fetch_fires() -> dict:
    try:
        gj = _fires_from_wfigs()
        source = "wfigs"
    except Exception as e:
        print(f"  [fires] WFIGS unavailable ({e}) — failing over to Living Atlas mirror")
        gj = _fires_from_mirror()
        source = "living_atlas"
    for f in gj["features"]:
        f["properties"].setdefault("_perimeter_source", "WFIGS")

    # Union in FIRIS. Neither source contains the other: FIRIS has fires WFIGS
    # has no polygon for (Gann, Grade, Mines), and WFIGS has a few FIRIS lacks.
    # Best-effort — a FIRIS outage must not cost us the WFIGS perimeters.
    firis_added = firis_replaced = firis_merged = firis_covered = 0
    try:
        try:
            import shapely  # noqa: F401  (only the geometric checks need it)
            shape_of = _shape_cache()
        except ImportError:
            print("  [fires] shapely unavailable — FIRIS merges by exact name only")
            shape_of = None
        by_key = {_fire_key(f["properties"].get("attr_IncidentName")
                            or f["properties"].get("poly_IncidentName")): f
                  for f in gj["features"]}

        def area(f) -> float:
            s = shape_of(f) if shape_of else None
            return s.area if s is not None else 0.0

        cands = _fires_from_firis()
        first_added = len(gj["features"])
        if shape_of:
            # Smallest first, so a complex's sub-fires are on the map before its
            # umbrella is tested against them. FIRIS lists CINDER COMPLEX ahead
            # of CINDERCOMPLEX-5-4 and -5-3, and taken as listed the outcome hung
            # on row order: one drew 5-4's shape under the complex's name,
            # another the umbrella's shape under 5-3's.
            cands.sort(key=area)
        for cand in cands:
            name = cand["properties"]["attr_IncidentName"]
            key = _fire_key(name)
            try:
                how, existing = _match_firis(cand, gj["features"], by_key, shape_of)
            except Exception as e:
                # One bad polygon must not cost the layer; match it by name as before.
                print(f"  [fires] FIRIS {name}: geometric match failed ({e})")
                how, existing = ("name", by_key[key]) if key in by_key else ("new", None)
            if how == "covered":
                print(f"  [fires] FIRIS {name}: dropped, already >= "
                      f"{FIRIS_COVERED_SHARE:.0%} inside perimeters on the map")
                firis_covered += 1
                for holder in existing:  # the perimeters that draw it
                    _note_aka(holder, name)
                continue
            if existing is None:
                gj["features"].append(cand)
                by_key[key] = cand
                firis_added += 1
                continue
            fresher = _fresher(cand, existing)
            if how != "name":
                firis_merged += 1
                ename = (existing["properties"].get("attr_IncidentName")
                         or existing["properties"].get("poly_IncidentName"))
                print(f"  [fires] FIRIS {name} is {ename} (by {how}); "
                      f"{'fresher, geometry swapped in' if fresher else 'older, dropped'}")
                _note_aka(existing, name)
            if fresher:
                # Same fire, fresher flight — swap the geometry in place.
                _swap_in_firis(existing, cand)
                if shape_of:
                    shape_of.forget(existing)
                firis_replaced += 1
        if shape_of:
            # Then draw what was added largest first. index.html stacks the
            # file in order, so the last perimeter is on top and takes the
            # clicks; left smallest-first, an umbrella its sub-fires cover
            # under 90% of would sit over them and swallow their popups.
            gj["features"][first_added:] = sorted(gj["features"][first_added:],
                                                  key=area, reverse=True)
    except Exception as e:
        print(f"  [fires] FIRIS perimeters unavailable: {e}")

    (DATA_DIR / "wildfires.geojson").write_text(json.dumps(gj))
    total_acres = sum(f["properties"].get("poly_GISAcres") or 0 for f in gj["features"])
    return {
        "fires": len(gj["features"]),
        "total_acres": round(total_acres, 0),
        "source": source,
        "firis_added": firis_added,
        "firis_fresher": firis_replaced,
        # FIRIS perimeters found to be a fire already drawn under another name
        # (alias or overlap), and ones the existing perimeters already cover.
        "firis_merged": firis_merged,
        "firis_covered": firis_covered,
    }


# A FIRIS perimeter can be a fire we already have under another name. The Aug
# 4 2026 snapshot drew 4 of its 37 fires twice: WFIGS "3-6 WAGON" again as
# FIRIS "WAGON" (sharing 88% of one shape and 99% of the other), WFIGS "5-3"
# and "5-4" again as CINDERCOMPLEX-5-4 and -5-3 (95-99% — cross-named, so the
# names alone would pair them wrong), and FIRIS's umbrella CINDER COMPLEX over
# both. ~5.7k acres counted twice.
# Kept conservative on purpose: a wrong merge hides a real fire, which is worse
# than drawing one twice. Areas are compared in plain degrees, which is fine for
# two polygons at the same latitude.
FIRIS_SAME_FIRE_OVERLAP = 0.5  # shared area, as a share of EACH polygon
FIRIS_COVERED_SHARE = 0.9      # share of a candidate already drawn by others
# A merge keeps the fresher shape and drops the older, so the fresher must
# already hold most of the older or the merge erases area only the older drew.
# Fires grow, and what one source draws outside the other is edge noise:
# WAGON's older flight sat 88% inside WFIGS's shape. A complex against one of
# its sub-fires is no such thing ("5-3" is only 69% of CINDER COMPLEX), and
# without this a fresh flight of one sub-fire would replace a whole-complex
# WFIGS perimeter, leaving the rest of the complex undrawn.
FIRIS_FRESHER_HOLDS = 0.8      # share of the older shape inside the fresher


def _shape_cache():
    """Memoized shapely geometry per perimeter feature; None where unusable.

    Holds each feature it has seen, so an id() can't be reused under it.
    """
    from shapely.geometry import shape
    from shapely.validation import make_valid
    cache: dict[int, tuple[dict, object]] = {}

    def shape_of(f: dict):
        hit = cache.get(id(f))
        if hit is not None:
            return hit[1]
        g = None
        if f.get("geometry"):
            try:
                g = shape(f["geometry"])
                if not g.is_valid:
                    # buffer(0) drops the slivers and lines make_valid can leave.
                    g = make_valid(g).buffer(0)
                if g.is_empty or g.area <= 0:
                    g = None
            except Exception as e:
                p = f["properties"]
                print(f"  [fires] unusable geometry for "
                      f"{p.get('attr_IncidentName') or p.get('poly_IncidentName')}: {e}")
                g = None
        cache[id(f)] = (f, g)
        return g

    shape_of.forget = lambda f: cache.pop(id(f), None)
    return shape_of


def _fresher(cand: dict, existing: dict) -> bool:
    """Whether a FIRIS candidate's flight is newer than the perimeter we have."""
    return ((cand["properties"].get("poly_DateCurrent") or 0)
            > (existing["properties"].get("poly_DateCurrent") or 0))


def _match_firis(cand: dict, features: list[dict], by_key: dict,
                 shape_of) -> tuple[str, dict | list[dict] | None]:
    """How a FIRIS candidate relates to the perimeters already in hand.

    ("name" | "overlap" | "alias", feature) when it is that fire; ("covered",
    [the perimeters that draw it]) when the map already does; ("new", None)
    otherwise. Past the exact name every rule needs shapes, so without shapely
    (shape_of is None) or a usable candidate polygon, only the name can match.
    """
    keys = _fire_keys(cand["properties"].get("attr_IncidentName"))
    if keys and keys[0] in by_key:
        return "name", by_key[keys[0]]

    cs = shape_of(cand) if shape_of else None
    if cs is None:
        # Not even the alias: on Aug 4 it was cross-wired (below), and with no
        # shape to check it against it would pair the wrong fires.
        return "new", None

    def merge_keeps_area(f) -> bool:
        fs = shape_of(f)
        if fs is None:
            return False
        older = fs if _fresher(cand, f) else cs
        return cs.intersection(fs).area >= FIRIS_FRESHER_HOLDS * older.area

    # Overlap before alias, because it can't be fooled by names: on Aug 4
    # WFIGS's "5-3" carried 5-4's polygon, so the alias alone would have
    # paired CINDERCOMPLEX-5-3 with the other fire.
    best, best_area, swallowed = None, 0.0, 0
    for f in features:
        fs = shape_of(f)
        if fs is None or not cs.intersects(fs):
            continue
        shared = cs.intersection(fs).area
        if shared < FIRIS_SAME_FIRE_OVERLAP * fs.area:
            continue
        swallowed += 1
        if shared >= FIRIS_SAME_FIRE_OVERLAP * cs.area and shared > best_area:
            best, best_area = f, shared
    # Holding most of two fires makes it an umbrella over them (CINDER
    # COMPLEX held 98% of both "5-3" and "5-4"), not either one: swapping
    # its shape into one would draw the other twice. The cover check below
    # is what deals with those.
    if best is not None and swallowed == 1 and merge_keeps_area(best):
        return "overlap", best

    # The alias is a bare sub-designation like "53": trust it only nearby.
    centre = _geom_center(cand.get("geometry"))
    for alias in keys[1:]:
        f = by_key.get(alias)
        fc = _geom_center(f.get("geometry")) if f else None
        if centre and fc and _km_apart(*centre, *fc) <= ALIAS_CONFIRM_KM and merge_keeps_area(f):
            return "alias", f

    # Already drawn: an umbrella over its sub-fires (each mostly inside it),
    # or an older flight inside a perimeter at least as fresh. NOT a fresher
    # flight inside an older perimeter: that is a new fire — a reburn, an
    # unburned island catching — and dropped, its shape went undrawn and its
    # name, folded into the old perimeter, marked its point as mapped and
    # could hand the old fire its containment and cause.
    from shapely.ops import unary_union
    held = [(f, fs) for f in features
            if (fs := shape_of(f)) is not None and fs.intersects(cs)
            and (cs.intersection(fs).area >= FIRIS_SAME_FIRE_OVERLAP * fs.area
                 or not _fresher(cand, f))]
    if held and (cs.intersection(unary_union([fs for _, fs in held])).area
                 >= FIRIS_COVERED_SHARE * cs.area):
        # What draws it is each perimeter sharing half of it or half of itself
        # with it, not one that merely touches its edge.
        return "covered", [f for f, fs in held if cs.intersection(fs).area
                           >= FIRIS_SAME_FIRE_OVERLAP * min(cs.area, fs.area)]
    return "new", None


def _swap_in_firis(existing: dict, cand: dict) -> None:
    """Take a fresher FIRIS flight's shape for a fire we already have.

    Only the geometry and what describes it — its acreage, when it was mapped
    and by whom — change hands. WFIGS's own attributes stay authoritative:
    swapping the whole feature used to replace its containment, cause, county
    and IRWIN id with FIRIS's blanks.
    """
    cp, p = cand["properties"], existing["properties"]
    existing["geometry"] = cand["geometry"]
    p["poly_DateCurrent"] = cp.get("poly_DateCurrent")
    p["_perimeter_source"] = cp.get("_perimeter_source")
    if cp.get("poly_GISAcres") is not None:
        p["poly_GISAcres"] = cp["poly_GISAcres"]


def _note_aka(feature: dict, name: str) -> None:
    """Keep a FIRIS name that was merged into, or covered by, this perimeter.

    The name leaves the file with the feature it came on, and incident points
    find their perimeter by name. Without this, replaying Aug 4 2026 with
    Cinder Complex still FIRIS-only, CAL FIRE's "Cinder Complex" point (WFIGS
    points are single fires, so a complex's own point is CAL FIRE's) was drawn
    as unmapped once the umbrella was dropped over its sub-fires, and no
    perimeter carried its CAL FIRE link; a "Wagon Fire" point would fare the
    same once WAGON joins "3-6 WAGON". fetch_fire_points and its backfill read
    these back, confirmed by position.
    """
    aka = feature["properties"].setdefault("_aka", [])
    if name not in aka:
        aka.append(name)


# =============================================================================
# 6a. CAL FIRE incidents — the state's own numbers, fresher than IRWIN's
# =============================================================================
# WFIGS carries whatever IRWIN was last told, which for state-responsibility
# fires goes stale fast: Grade read 0.1 acres in WFIGS against 689 at CAL FIRE,
# and Gann 3000 against 3760. CAL FIRE publishes plain JSON (not ArcGIS), so it
# doesn't go through arcgis_query.
CALFIRE_URL = "https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List"


def fetch_calfire() -> dict:
    year = datetime.now(timezone.utc).year
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = HTTP.get(CALFIRE_URL, params={"year": year}, timeout=30)
            r.raise_for_status()
            items = r.json()
            if not isinstance(items, list):
                raise RuntimeError(f"CAL FIRE returned {type(items).__name__}, expected a list")
            break
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last = e
            if attempt == 3:
                raise
            print(f"  [calfire] transient failure, retrying in 5s: {e}")
            time.sleep(5)

    features = []
    for i in items:
        lat, lon = i.get("Latitude"), i.get("Longitude")
        # A few records carry 0/None coordinates; they can't be placed.
        if not lat or not lon:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: i.get(k) for k in (
                "Name", "AcresBurned", "PercentContained", "County", "Location",
                "Started", "Updated", "IsActive", "Url", "AdminUnit", "Final")},
        })
    gj = {"type": "FeatureCollection", "features": features}
    (DATA_DIR / "calfire.geojson").write_text(json.dumps(gj))
    return {
        "incidents": len(features),
        "active": sum(1 for f in features if f["properties"].get("IsActive")),
        "dropped_no_coords": len(items) - len(features),
    }


# =============================================================================
# 6b. WFIGS incident points — fires with no mapped perimeter yet
# =============================================================================
# A perimeter polygon only exists once someone flies or GPS-walks the fire and
# a GIS specialist uploads it, which can lag a day or more — and lags hardest
# on CAL FIRE state-responsibility incidents, which don't feed the national
# pipeline as promptly as federal ones. Until that upload happens the fire has
# an IRWIN record and no geometry, so a perimeter-only map shows nothing at
# all. These points cover the gap.
FIRE_POINTS_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
                   "WFIGS_Incident_Locations_Current/FeatureServer/0/query")


def _fire_points_from_wfigs() -> dict:
    return arcgis_query(FIRE_POINTS_URL, {
        # WF excludes prescribed burns (RX), which aren't what this map is for.
        "where": "POOState='US-CA' AND IncidentTypeCategory='WF'",
        "outFields": ("IncidentName,IncidentSize,PercentContained,FireDiscoveryDateTime,"
                      "POOCounty,POOProtectingAgency,IrwinID,FireOutDateTime,FireCause"),
        "outSR": 4326,
        "f": "geojson",
    }, what="fire points (WFIGS)")


def _fire_points_from_mirror() -> dict:
    """Same incidents from the Living Atlas layer 0, in the primary's schema."""
    gj = arcgis_query(f"{FIRES_MIRROR_BASE}/0/query", {
        "where": "POOState='US-CA' AND IncidentTypeCategory='WF'",
        "outFields": ("IncidentName,DailyAcres,CalculatedAcres,PercentContained,"
                      "FireDiscoveryDateTime,POOCounty,IrwinID,FireOutDateTime,FireCause"),
        "outSR": 4326,
        "f": "geojson",
    }, what="fire points mirror")

    for f in gj["features"]:
        p = f["properties"]
        # DailyAcres is the current reported size. Deliberately NOT falling back
        # to DiscoveryAcres, which is the size when the fire was first found —
        # Gann reads 0.01 there against 3000 today, and size drives the marker
        # radius. Better to report no size than a wrong one.
        daily = p.pop("DailyAcres", None)
        calc = p.pop("CalculatedAcres", None)
        p["IncidentSize"] = daily if daily is not None else calc
        # The mirror's point layer carries no protecting-agency field.
        p["POOProtectingAgency"] = None
    return gj


def fetch_fire_points() -> dict:
    try:
        gj = _fire_points_from_wfigs()
        source = "wfigs"
    except Exception as e:
        print(f"  [fire points] WFIGS unavailable ({e}) — failing over to Living Atlas mirror")
        gj = _fire_points_from_mirror()
        source = "living_atlas"

    # Overlay CAL FIRE's numbers where they line up. Best-effort: WFIGS points
    # with stale acreage still beat no points at all.
    enriched = appended = 0
    try:
        cal = json.loads((DATA_DIR / "calfire.geojson").read_text())["features"]
        by_key = {(_fire_key(c["properties"].get("Name")),
                   (c["properties"].get("County") or "").upper()): c for c in cal}
        used = set()
        for f in gj["features"]:
            p = f["properties"]
            key = (_fire_key(p.get("IncidentName")), (p.get("POOCounty") or "").upper())
            c = by_key.get(key)
            if not c:
                continue
            lon, lat = f["geometry"]["coordinates"]
            clon, clat = c["geometry"]["coordinates"]
            # Name+county can collide across a big county; confirm by position.
            if _km_apart(lat, lon, clat, clon) > 25:
                continue
            cp = c["properties"]
            used.add(key)
            p["_calfire_acres"] = cp.get("AcresBurned")
            p["_calfire_contained"] = cp.get("PercentContained")
            p["_calfire_updated"] = cp.get("Updated")
            p["_calfire_url"] = cp.get("Url")
            enriched += 1

        # Active CAL FIRE incidents WFIGS hasn't got. Skip any with a WFIGS
        # point close by: Cinder Complex sits 1.2 km from WFIGS's "5-4", the
        # same fire under lightning-complex numbering, and appending it would
        # draw the thing twice.
        for c in cal:
            cp = c["properties"]
            key = (_fire_key(cp.get("Name")), (cp.get("County") or "").upper())
            if key in used or not cp.get("IsActive"):
                continue
            clon, clat = c["geometry"]["coordinates"]
            if any(_km_apart(clat, clon, f["geometry"]["coordinates"][1],
                             f["geometry"]["coordinates"][0]) < 5
                   for f in gj["features"]):
                continue
            started = cp.get("Started")
            gj["features"].append({
                "type": "Feature",
                "geometry": c["geometry"],
                "properties": {
                    "IncidentName": cp.get("Name"),
                    "IncidentSize": cp.get("AcresBurned"),
                    "PercentContained": cp.get("PercentContained"),
                    "POOCounty": cp.get("County"),
                    "POOProtectingAgency": "CAL FIRE",
                    "FireDiscoveryDateTime": _iso_to_ms(started),
                    "IrwinID": None,
                    "FireOutDateTime": None,
                    "_calfire_acres": cp.get("AcresBurned"),
                    "_calfire_contained": cp.get("PercentContained"),
                    "_calfire_updated": cp.get("Updated"),
                    "_calfire_url": cp.get("Url"),
                    "_calfire_only": True,
                },
            })
            appended += 1
    except Exception as e:
        print(f"  [fire points] CAL FIRE overlay unavailable: {e}")

    # Tag the points a perimeter already covers, so the map can play those down
    # and highlight the fires where the dot is the only thing there is to draw.
    mapped: set[str] = set()
    mapped_names: set[str] = set()
    nearby_names: dict[str, list[tuple[float, float, float, float]]] = {}
    try:
        perims = json.loads((DATA_DIR / "wildfires.geojson").read_text())
        mapped = {_irwin(f["properties"].get("attr_IrwinID"))
                  for f in perims["features"]} - {""}
        # FIRIS perimeters carry no IRWIN id at all, so they need a name path
        # or every FIRIS-only fire would still claim to have no perimeter.
        for f in perims["features"]:
            p = f["properties"]
            keys = _fire_keys(p.get("attr_IncidentName") or p.get("poly_IncidentName"))
            # Any other name hit has to land near the perimeter. On Aug 4 2026
            # a San Diego County point named "54" matched both WFIGS's "5-4"
            # and CINDERCOMPLEX-5-4's alias, ~500 km away in Inyo, and was
            # drawn dimmed as mapped when nothing mapped it. A perimeter with
            # an IRWIN id already matched its own point by that id above.
            if _irwin(p.get("attr_IrwinID")):
                confirm = keys
            else:
                mapped_names.update(keys[:1])
                confirm = keys[1:]
            # So do the FIRIS names fetch_fires folded into this one.
            confirm = confirm + [k for n in p.get("_aka") or [] for k in _fire_keys(n)]
            box = _geom_bbox(f.get("geometry"))
            for key in confirm:
                if box:
                    nearby_names.setdefault(key, []).append(box)
    except Exception as e:
        # Fail toward visibility: without the cross-reference every point is
        # treated as unmapped, which over-draws rather than hiding a fire.
        print(f"  [fire points] no perimeter file to cross-reference ({e})")

    unmapped = 0
    for f in gj["features"]:
        p = f["properties"]
        key = _fire_key(p.get("IncidentName"))
        has = _irwin(p.get("IrwinID")) in mapped or key in mapped_names
        if not has and key in nearby_names:
            here = _geom_center(f.get("geometry"))
            has = bool(here) and any(_km_outside(*here, box) <= ALIAS_CONFIRM_KM
                                     for box in nearby_names[key])
        p["_has_perimeter"] = has
        if not p["_has_perimeter"]:
            unmapped += 1

    (DATA_DIR / "fire_points.geojson").write_text(json.dumps(gj))

    # Push attributes back onto the perimeters. FIRIS rows are geometry and
    # little else — no containment, cause, county or discovery date — while the
    # point for the same fire has all of it, now with CAL FIRE's numbers on top.
    # This runs here rather than in fetch_fires() because the dependency goes
    # both ways: points need the perimeters to set _has_perimeter, so the
    # perimeter file already exists by the time we get here.
    perims_enriched, perims_aliased = _backfill_perimeter_attrs(gj["features"])

    return {
        "incidents": len(gj["features"]),
        "without_perimeter": unmapped,
        "source": source,
        "calfire_enriched": enriched,
        "calfire_only": appended,
        "perimeters_enriched": perims_enriched,
        "perimeters_via_alias": perims_aliased,
    }


# Only ever filled in when the perimeter itself has nothing — WFIGS's own
# values stay authoritative where it has them.
_BACKFILL = {
    "attr_PercentContained": ("PercentContained", "_calfire_contained"),
    "attr_POOCounty": ("POOCounty",),
    "attr_FireDiscoveryDateTime": ("FireDiscoveryDateTime",),
    "attr_FireCause": ("FireCause",),
}


def _backfill_perimeter_attrs(points: list[dict]) -> tuple[int, int]:
    """Fill blank perimeter attributes from the incident point for that fire.

    Returns (perimeters filled, of which matched by a name that needed
    confirming: a complex-name alias, or a FIRIS name folded into it).
    """
    path = DATA_DIR / "wildfires.geojson"
    try:
        perims = json.loads(path.read_text())
    except Exception as e:
        # Both counts: the caller unpacks two, and a bare 0 here crashed
        # fetch_fire_points on a cold start with no perimeter file.
        print(f"  [fire points] no perimeter file to backfill: {e}")
        return 0, 0

    by_key: dict[str, dict] = {}
    for pt in points:
        detail = sum(v is not None for v in pt["properties"].values())
        for key in _fire_keys(pt["properties"].get("IncidentName")):
            # Prefer the point carrying the most detail if a name repeats.
            prev = by_key.get(key)
            if prev is None or detail > sum(v is not None for v in prev["properties"].values()):
                by_key[key] = pt

    filled = aliased = 0
    for f in perims["features"]:
        p = f["properties"]
        box = _geom_bbox(f.get("geometry"))
        keys = _fire_keys(p.get("attr_IncidentName") or p.get("poly_IncidentName"))
        for name in p.get("_aka") or []:
            keys += [k for k in _fire_keys(name) if k not in keys]
        src = None
        for i, key in enumerate(keys):
            cand = by_key.get(key)
            if not cand:
                continue
            # The alias is a bare sub-designation like "53", and a folded-in
            # FIRIS name isn't this perimeter's own — confirm either lands on
            # the same fire before trusting it.
            if i > 0:
                cc = _geom_center(cand.get("geometry"))
                if not box or not cc or _km_outside(*cc, box) > ALIAS_CONFIRM_KM:
                    continue
                aliased += 1
            src = cand["properties"]
            break
        if not src:
            continue
        touched = False
        for target, candidates in _BACKFILL.items():
            if p.get(target) is not None:
                continue
            for c in candidates:
                if src.get(c) is not None:
                    p[target] = src[c]
                    touched = True
                    break
        # Carry the CAL FIRE link through so the perimeter popup can offer it.
        if src.get("_calfire_url") and not p.get("_calfire_url"):
            p["_calfire_url"] = src["_calfire_url"]
            touched = True
        if touched:
            filled += 1

    path.write_text(json.dumps(perims))
    if aliased:
        print(f"  [fire points] {aliased} perimeter(s) matched via an alias or folded-in name")
    return filled, aliased


# =============================================================================
# 6c. RAWS observed wind — the ground truth HRRR is forecasting
# =============================================================================
# HRRR gives a smooth modelled field; RAWS gives what anemometers actually
# recorded. Both matter here: the forecast shows where wind is heading, the
# stations show whether it arrived. Published by the same CAL FIRE ArcGIS org
# as the FIRIS perimeters, updated within the hour.
RAWS_URL = ("https://bz1uwwpkuinzbk94.svcs5.arcgis.com/bz1uwWPKUInZBK94/arcgis/rest/"
            "services/RAWS_Wind_2D_Public_View/FeatureServer/0/query")
# A gust has to bear some relation to the sustained wind. THERMALITO RIVER
# OUTLET reported 109 mph against a 6 mph sustained wind — a broken anemometer,
# but it set max_gust_mph for the whole state and painted its station red.
# Both conditions must hold, so a real 20 mph gust on a calm day survives:
# across 4,404 stations these thresholds reject exactly that one sensor.
GUST_RATIO_LIMIT = 3.0
GUST_ABS_FLOOR_MPH = 40.0
# The query is where 1=1 with no time filter, so a short count is never real
# weather — it is the upstream layer caught mid-refill. 12 of 100 deploys from
# Sep 15 to Oct 9 2026 published 532-1,864 stations as ok, in six episodes of
# 10-20 h. Normal runs return 3,900-4,450 and the short ones never topped
# 2,672, so 3,000 sits clear of both.
RAWS_MIN_STATIONS = 3000


def _gust_is_suspect(speed, gust) -> bool:
    if gust is None or speed is None:
        return False
    return gust > GUST_ABS_FLOOR_MPH and (speed == 0 or gust > GUST_RATIO_LIMIT * speed)


def fetch_raws() -> dict:
    gj = arcgis_query_all(RAWS_URL, {
        "where": "1=1",
        "outFields": ("station_id,station_name,wind_speed_mph,wind_direction_deg,"
                      "wind_gust_mph,peak_wind_speed_mph,wind_cardinal_direction,"
                      "relative_humidity_pct,air_temp_f,last_updated"),
        "outSR": 4326,
        "f": "geojson",
    }, what="RAWS")

    # Round the floats — the raw feed carries 14 significant digits per field
    # across 4000+ stations, which is most of the file for no added meaning.
    round1 = ("wind_speed_mph", "wind_gust_mph", "peak_wind_speed_mph",
              "relative_humidity_pct", "air_temp_f")
    kept = []
    for f in gj["features"]:
        if not f.get("geometry"):
            continue
        p = f["properties"]
        for k in round1:
            if isinstance(p.get(k), float):
                p[k] = round(p[k], 1)
        kept.append(f)
    gj["features"] = kept

    if not kept:
        # 4,000+ stations always report; zero means the query broke, not calm.
        raise RuntimeError("RAWS query returned no stations")
    if len(kept) < RAWS_MIN_STATIONS:
        # Raised before the write, so run_one keeps the last full file as stale.
        raise RuntimeError(
            f"RAWS returned only {len(kept)} stations (expected ~4,200, floor "
            f"{RAWS_MIN_STATIONS}); the upstream layer is likely being emptied "
            f"and refilled")

    rejected = 0
    for f in kept:
        p = f["properties"]
        if _gust_is_suspect(p.get("wind_speed_mph"), p.get("wind_gust_mph")):
            # Keep the raw value so the popup can say why the gust is blank.
            p["_gust_rejected"] = p["wind_gust_mph"]
            p["wind_gust_mph"] = None
            rejected += 1

    (DATA_DIR / "raws.geojson").write_text(json.dumps(gj))
    gusts = [f["properties"].get("wind_gust_mph") for f in kept
             if f["properties"].get("wind_gust_mph") is not None]
    return {
        "stations": len(kept),
        "max_gust_mph": round(max(gusts), 1) if gusts else None,
        "gusts_rejected": rejected,
        "reporting_wind": sum(1 for f in kept
                              if f["properties"].get("wind_speed_mph") is not None),
    }


# =============================================================================
# 6d. Evacuation zones — who has actually been told to leave
# =============================================================================
# Genasys's product is Zonehaven, and CAL FIRE republishes it here (the layer
# is named Zonehaven_Evacuation_Status_exp3) alongside zones some counties
# publish themselves. Public, no token, and it only carries zones that are
# *not* Normal — so everything in it is a live order, warning or advisory.
EVAC_URL = ("https://bz1uwwpkuinzbk94.svcs5.arcgis.com/bz1uwWPKUInZBK94/arcgis/rest/"
            "services/California_Combined_Statewide_Evacuation_Public_View/"
            "FeatureServer/0/query")


def fetch_evacuations() -> dict:
    gj = arcgis_query_all(EVAC_URL, {
        # Belt and braces: the layer is already filtered upstream, but say so
        # in case it ever starts carrying the Normal zones too.
        "where": "zone_status IS NOT NULL AND zone_status <> 'Normal'",
        "outFields": ("zone_id,zone_status,zone_status_reason,county_name,community,"
                      "acreage,last_updated,url,known_as,data_source_name"),
        "outSR": 4326,
        "f": "geojson",
    }, what="evacuations")

    gj["features"] = [f for f in gj["features"] if f.get("geometry")]
    counts: dict[str, int] = {}
    for f in gj["features"]:
        s = f["properties"].get("zone_status") or "Unknown"
        counts[s] = counts.get(s, 0) + 1

    (DATA_DIR / "evacuations.geojson").write_text(json.dumps(gj))
    return {
        "zones": len(gj["features"]),
        "orders": counts.get("Evacuation Order", 0),
        "warnings": counts.get("Evacuation Warning", 0),
        "advisories": counts.get("Advisory", 0),
        "counties": len({f["properties"].get("county_name")
                         for f in gj["features"] if f["properties"].get("county_name")}),
    }


# =============================================================================
# 7. InciWeb incidents (origin points + rich narrative metadata)
# =============================================================================
# WFIGS gives us fire perimeters; InciWeb gives us per-incident pages with
# news releases, evacuation orders, photos, etc. They complement each other.
INCIWEB_URL = ("https://services7.arcgis.com/KrXqMokvukYo0YOo/arcgis/rest/services/"
               "Wildfire_Aware_Inciweb/FeatureServer/0/query")


def fetch_inciweb() -> dict:
    bbox = PGE_BBOX
    params = {
        "where": "1=1",
        "geometry": f"{bbox['lon_min']},{bbox['lat_min']},{bbox['lon_max']},{bbox['lat_max']}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ("IncidentName,OriginDate,Location,InciwebLink,Agency,"
                      "FacebookURL,TwitterURL,IncidentOverview"),
        "outSR": 4326,
        "f": "geojson",
    }
    gj = arcgis_query(INCIWEB_URL, params, what="InciWeb", timeout=30)
    (DATA_DIR / "inciweb.geojson").write_text(json.dumps(gj))
    return {"incidents": len(gj["features"])}


# =============================================================================
# 5. CA county polygons (reused from OES, 58 counties)
# =============================================================================
COUNTIES_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/"
    "Power_Outages_(View)/FeatureServer/2/query"
)


def fetch_counties() -> dict:
    params = {"where": "1=1", "outFields": "NAME", "outSR": 4326, "f": "geojson"}
    gj = arcgis_query(COUNTIES_URL, params, what="counties")
    if not gj["features"]:
        # This has actually happened, repeatedly (2026-06-06, 06-13, 06-22,
        # 06-30, 07-23): OES returns an empty set, we wrote an empty file and
        # reported ok, and the county layer plus the hover panel's County row
        # went blank statewide with nothing flagging it. California has 58
        # counties; zero is always a broken query, never real.
        raise RuntimeError("counties query returned no features")

    # The same layer has also served every county twice (116 rows for 58).
    seen: set[str] = set()
    deduped = []
    for f in gj["features"]:
        name = (f["properties"].get("NAME") or "").strip().upper()
        if name and name in seen:
            continue
        seen.add(name)
        deduped.append(f)
    if len(deduped) != len(gj["features"]):
        print(f"  [counties] dropped {len(gj['features']) - len(deduped)} duplicate rows")
    gj["features"] = deduped

    (DATA_DIR / "ca_counties.geojson").write_text(json.dumps(gj))
    return {"counties": len(gj["features"])}


# =============================================================================
# 4. All CA utility service territories (CEC layer, IOU + POU)
# =============================================================================
TERRITORY_BASE = ("https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services/"
                  "ElectricLoadServingEntities_IOU_POU/FeatureServer/0")


def fetch_territory() -> dict:
    params = {
        "where": "1=1",
        # No Sales_GWh_2023: CEC dropped it (the layer carries no sales/GWh
        # field at all now), and ArcGIS rejects the whole query with a 400
        # "'outFields' parameter is invalid" for one unknown field rather than
        # ignoring it — which took the layer off the map entirely.
        "outFields": "Acronym,Utility,Type",
        "outSR": 4326,
        "f": "geojson",
    }
    gj = arcgis_query(f"{TERRITORY_BASE}/query", params, what="territory")
    if not gj["features"]:
        raise RuntimeError("CEC utility territories query returned no features")
    # Strip whitespace from acronyms — CEC data has trailing spaces sometimes.
    for f in gj["features"]:
        a = f["properties"].get("Acronym") or ""
        f["properties"]["Acronym"] = a.strip()
    # Filename kept for back-compat with the existing HTML fetch path.
    (DATA_DIR / "pge_territory.geojson").write_text(json.dumps(gj))
    return {
        "features": len(gj["features"]),
        "iou_count": sum(1 for f in gj["features"] if f["properties"].get("Type") == "IOU"),
        "pou_count": sum(1 for f in gj["features"] if f["properties"].get("Type") == "POU"),
    }


# =============================================================================
# Driver
# =============================================================================
def _previous_manifest() -> dict:
    """Last run's manifest, so a failed fetcher can carry its counts forward."""
    try:
        return json.loads((DATA_DIR / "manifest.json").read_text())
    except Exception:
        return {}


def run_one(name: str, fn, prev: dict | None = None, cache_file: str | None = None) -> dict:
    print(f"\n[{name}] fetching ...")
    t0 = time.time()
    try:
        result = fn()
        dt = time.time() - t0
        print(f"[{name}] ok in {dt:.1f}s -> {result}")
        return {"status": "ok", "duration_s": round(dt, 1), **result}
    except Exception as e:
        print(f"[{name}] FAILED: {e}")
        traceback.print_exc()
        # A dead upstream doesn't invalidate what's already on disk — nothing
        # overwrote it, so the map still draws the previous snapshot. Report
        # that as stale (with the age of the data) rather than as a bare
        # failure, which reads as "this layer is missing" when it isn't.
        before = (prev or {}).get(name) or {}
        if cache_file and (DATA_DIR / cache_file).exists() and before.get("status") in ("ok", "stale"):
            carried = {k: v for k, v in before.items()
                       if k not in ("status", "duration_s", "error", "as_of")}
            print(f"[{name}] serving last-good snapshot as stale")
            return {
                **carried,
                "status": "stale",
                "error": repr(e),
                "as_of": before.get("as_of") or (prev or {}).get("generated_at"),
            }
        return {"status": "error", "error": repr(e)}


def main() -> int:
    prev = _previous_manifest()
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wind": run_one("wind", fetch_wind, prev, "wind.json"),
        "raws": run_one("raws", fetch_raws, prev, "raws.geojson"),
        "outages": run_one("outages", fetch_outages, prev, "outage_points.geojson"),
        "psps": run_one("psps", fetch_psps, prev, "psps.geojson"),
        "alerts": run_one("alerts", fetch_alerts, prev, "nws_alerts.geojson"),
        "territory": run_one("territory", fetch_territory, prev, "pge_territory.geojson"),
        "counties": run_one("counties", fetch_counties, prev, "ca_counties.geojson"),
        "fires": run_one("fires", fetch_fires, prev, "wildfires.geojson"),
        "calfire": run_one("calfire", fetch_calfire, prev, "calfire.geojson"),
        # After fires and calfire — it cross-references both files this run wrote.
        "fire_points": run_one("fire_points", fetch_fire_points, prev, "fire_points.geojson"),
        "evacuations": run_one("evacuations", fetch_evacuations, prev, "evacuations.geojson"),
        "inciweb": run_one("inciweb", fetch_inciweb, prev, "inciweb.geojson"),
    }
    (DATA_DIR / "manifest.json").write_text(json.dumps(results, indent=2))
    print("\nmanifest:")
    print(json.dumps(results, indent=2))
    # Non-zero exit only if EVERYTHING failed. Derived from the results rather
    # than a hardcoded key list, so adding a fetcher can't silently skip it.
    ok = sum(1 for v in results.values()
             if isinstance(v, dict) and v.get("status") == "ok")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
