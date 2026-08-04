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

    bbox = PGE_BBOX
    # Build a regular target grid over PG&E bbox at ~0.05° (~5km, close to HRRR native)
    step = 0.05
    target_lats = np.arange(bbox["lat_max"], bbox["lat_min"] - 1e-9, -step)
    target_lons = np.arange(bbox["lon_min"], bbox["lon_max"] + 1e-9, step)

    # Nearest-neighbor resample from HRRR native to regular grid.
    # For each target cell, find the nearest HRRR grid point. We do this with
    # a flat KD-style lookup approximated by indexing into a coarse mask first,
    # then taking argmin over the small subset — fast enough for ~10k cells.
    u_vals = u.values
    v_vals = v.values

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
        "run": H.date.isoformat(),
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
NWS_ZONE_URL = "https://api.weather.gov/zones/forecast/{zone}"
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
# Per-event severity ordering used to keep popups consistent.
_zone_cache: dict[str, dict | None] = {}


def _fetch_zone(zone_id: str) -> dict | None:
    """Return GeoJSON geometry for a forecast zone, or None on failure."""
    if zone_id in _zone_cache:
        return _zone_cache[zone_id]
    try:
        r = HTTP.get(NWS_ZONE_URL.format(zone=zone_id), headers=NWS_HEADERS, timeout=20)
        r.raise_for_status()
        geom = r.json().get("geometry")
    except Exception as e:
        print(f"    zone {zone_id}: {e}")
        geom = None
    _zone_cache[zone_id] = geom
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
    needed = set()
    for f in alerts:
        if not f.get("geometry"):
            for z in (f["properties"].get("geocode") or {}).get("UGC", []):
                needed.add(z)
    print(f"  fetching {len(needed)} forecast-zone polygons")
    for z in sorted(needed):
        _fetch_zone(z)

    # Build per-alert features with merged geometry clipped to California.
    out_features = []
    dropped_out_of_state = 0
    for f in alerts:
        p = f["properties"]
        geom_dict = f.get("geometry")
        if geom_dict:
            merged = shape(geom_dict)
        else:
            zone_geoms = []
            for z in (p.get("geocode") or {}).get("UGC", []):
                g = _zone_cache.get(z)
                if g:
                    try:
                        zone_geoms.append(shape(g))
                    except Exception:
                        pass
            if not zone_geoms:
                continue  # nothing to draw
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
    return {
        "alerts_total": len(out_features),
        "by_event": counts,
        "dropped_out_of_state": dropped_out_of_state,
    }


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


def _km_apart(lat1, lon1, lat2, lon2) -> float:
    """Rough great-circle distance in km — fine at the scale we compare at."""
    return math.hypot((lat1 - lat2) * 111.0,
                      (lon1 - lon2) * 111.0 * math.cos(math.radians(lat1)))


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
    firis_added = firis_replaced = 0
    try:
        by_key = {_fire_key(f["properties"].get("attr_IncidentName")
                            or f["properties"].get("poly_IncidentName")): f
                  for f in gj["features"]}
        for cand in _fires_from_firis():
            key = _fire_key(cand["properties"]["attr_IncidentName"])
            existing = by_key.get(key)
            if existing is None:
                gj["features"].append(cand)
                by_key[key] = cand
                firis_added += 1
            elif (cand["properties"].get("poly_DateCurrent") or 0) > \
                 (existing["properties"].get("poly_DateCurrent") or 0):
                # Same fire, fresher flight — swap the geometry in place.
                gj["features"][gj["features"].index(existing)] = cand
                by_key[key] = cand
                firis_replaced += 1
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
    }


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
    try:
        perims = json.loads((DATA_DIR / "wildfires.geojson").read_text())
        mapped = {_irwin(f["properties"].get("attr_IrwinID"))
                  for f in perims["features"]} - {""}
        # FIRIS perimeters carry no IRWIN id at all, so they need a name path
        # or every FIRIS-only fire would still claim to have no perimeter.
        mapped_names = {_fire_key(f["properties"].get("attr_IncidentName")
                                  or f["properties"].get("poly_IncidentName"))
                        for f in perims["features"]} - {""}
    except Exception as e:
        # Fail toward visibility: without the cross-reference every point is
        # treated as unmapped, which over-draws rather than hiding a fire.
        print(f"  [fire points] no perimeter file to cross-reference ({e})")

    unmapped = 0
    for f in gj["features"]:
        p = f["properties"]
        p["_has_perimeter"] = (_irwin(p.get("IrwinID")) in mapped
                               or _fire_key(p.get("IncidentName")) in mapped_names)
        if not p["_has_perimeter"]:
            unmapped += 1

    (DATA_DIR / "fire_points.geojson").write_text(json.dumps(gj))

    # Push attributes back onto the perimeters. FIRIS rows are geometry and
    # little else — no containment, cause, county or discovery date — while the
    # point for the same fire has all of it, now with CAL FIRE's numbers on top.
    # This runs here rather than in fetch_fires() because the dependency goes
    # both ways: points need the perimeters to set _has_perimeter, so the
    # perimeter file already exists by the time we get here.
    perims_enriched = _backfill_perimeter_attrs(gj["features"])

    return {
        "incidents": len(gj["features"]),
        "without_perimeter": unmapped,
        "source": source,
        "calfire_enriched": enriched,
        "calfire_only": appended,
        "perimeters_enriched": perims_enriched,
    }


# Only ever filled in when the perimeter itself has nothing — WFIGS's own
# values stay authoritative where it has them.
_BACKFILL = {
    "attr_PercentContained": ("PercentContained", "_calfire_contained"),
    "attr_POOCounty": ("POOCounty",),
    "attr_FireDiscoveryDateTime": ("FireDiscoveryDateTime",),
    "attr_FireCause": ("FireCause",),
}


def _backfill_perimeter_attrs(points: list[dict]) -> int:
    """Fill blank perimeter attributes from the incident point for that fire."""
    path = DATA_DIR / "wildfires.geojson"
    try:
        perims = json.loads(path.read_text())
    except Exception as e:
        print(f"  [fire points] no perimeter file to backfill: {e}")
        return 0

    by_key: dict[str, dict] = {}
    for f in points:
        key = _fire_key(f["properties"].get("IncidentName"))
        # Prefer the point carrying the most detail if a name repeats.
        if key and (key not in by_key
                    or sum(v is not None for v in f["properties"].values())
                    > sum(v is not None for v in by_key[key].values())):
            by_key[key] = f["properties"]

    filled = 0
    for f in perims["features"]:
        p = f["properties"]
        src = by_key.get(_fire_key(p.get("attr_IncidentName") or p.get("poly_IncidentName")))
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
    return filled


# =============================================================================
# 6c. RAWS observed wind — the ground truth HRRR is forecasting
# =============================================================================
# HRRR gives a smooth modelled field; RAWS gives what anemometers actually
# recorded. Both matter here: the forecast shows where wind is heading, the
# stations show whether it arrived. Published by the same CAL FIRE ArcGIS org
# as the FIRIS perimeters, updated within the hour.
RAWS_URL = ("https://bz1uwwpkuinzbk94.svcs5.arcgis.com/bz1uwWPKUInZBK94/arcgis/rest/"
            "services/RAWS_Wind_2D_Public_View/FeatureServer/0/query")


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

    (DATA_DIR / "raws.geojson").write_text(json.dumps(gj))
    speeds = [f["properties"].get("wind_gust_mph") or 0 for f in kept]
    return {
        "stations": len(kept),
        "max_gust_mph": round(max(speeds), 1) if speeds else None,
        "reporting_wind": sum(1 for f in kept
                              if f["properties"].get("wind_speed_mph") is not None),
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
        "outFields": "Acronym,Utility,Type,Sales_GWh_2023",
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
