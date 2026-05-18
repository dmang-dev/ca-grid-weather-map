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
        r = HTTP.get(url, params=params, timeout=60)
        r.raise_for_status()
        gj = r.json()
        # ArcGIS sometimes returns {"error": ...} with 200; sanity check.
        if "features" not in gj:
            raise RuntimeError(f"Bad response from OES layer {layer_id}: {gj!r}")
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
    r = HTTP.get(f"{PSPS_BASE}/query", params=params, timeout=60)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj:
        raise RuntimeError(f"Bad PSPS response: {gj!r}")

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
        r = HTTP.get(CA_BOUNDARY_URL, params={
            "where": "1=1", "outFields": "State", "outSR": 4326, "f": "geojson",
        }, timeout=60)
        r.raise_for_status()
        CA_BOUNDARY_FILE.write_text(r.text)
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


def fetch_fires() -> dict:
    params = {
        "where": "attr_POOState='US-CA'",
        "outFields": ("poly_IncidentName,poly_GISAcres,poly_DateCurrent,"
                      "attr_IncidentName,attr_PercentContained,attr_FireDiscoveryDateTime,"
                      "attr_FireCause,attr_IncidentTypeCategory,attr_POOCounty"),
        "outSR": 4326,
        "f": "geojson",
    }
    r = HTTP.get(FIRES_URL, params=params, timeout=60)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj:
        raise RuntimeError(f"Fires fetch failed: {gj!r}")
    (DATA_DIR / "wildfires.geojson").write_text(json.dumps(gj))
    total_acres = sum(f["properties"].get("poly_GISAcres") or 0 for f in gj["features"])
    return {
        "fires": len(gj["features"]),
        "total_acres": round(total_acres, 0),
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
    r = HTTP.get(INCIWEB_URL, params=params, timeout=30)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj:
        raise RuntimeError(f"InciWeb fetch failed: {gj!r}")
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
    r = HTTP.get(COUNTIES_URL, params=params, timeout=60)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj:
        raise RuntimeError(f"Counties fetch failed: {gj!r}")
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
    r = HTTP.get(f"{TERRITORY_BASE}/query", params=params, timeout=60)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj or not gj["features"]:
        raise RuntimeError(f"CEC utility territories not found: {gj!r}")
    # Filename kept for back-compat with the existing HTML fetch path.
    (DATA_DIR / "pge_territory.geojson").write_text(json.dumps(gj))
    # Strip whitespace from acronyms — CEC data has trailing spaces sometimes.
    for f in gj["features"]:
        a = f["properties"].get("Acronym") or ""
        f["properties"]["Acronym"] = a.strip()
    (DATA_DIR / "pge_territory.geojson").write_text(json.dumps(gj))
    return {
        "features": len(gj["features"]),
        "iou_count": sum(1 for f in gj["features"] if f["properties"].get("Type") == "IOU"),
        "pou_count": sum(1 for f in gj["features"] if f["properties"].get("Type") == "POU"),
    }


# =============================================================================
# Driver
# =============================================================================
def run_one(name: str, fn) -> dict:
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
        return {"status": "error", "error": repr(e)}


def main() -> int:
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wind": run_one("wind", fetch_wind),
        "outages": run_one("outages", fetch_outages),
        "psps": run_one("psps", fetch_psps),
        "alerts": run_one("alerts", fetch_alerts),
        "territory": run_one("territory", fetch_territory),
        "counties": run_one("counties", fetch_counties),
        "fires": run_one("fires", fetch_fires),
        "inciweb": run_one("inciweb", fetch_inciweb),
    }
    (DATA_DIR / "manifest.json").write_text(json.dumps(results, indent=2))
    print("\nmanifest:")
    print(json.dumps(results, indent=2))
    # Non-zero exit only if EVERYTHING failed.
    ok = sum(1 for k in ("wind", "outages", "psps", "alerts", "territory", "counties", "fires", "inciweb") if results[k]["status"] == "ok")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
