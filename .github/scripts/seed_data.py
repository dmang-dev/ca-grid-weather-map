"""Seed data/ from the currently-published site before fetch_data.py runs.

data/ is gitignored, so a fresh runner starts with an empty directory. That
leaves run_one()'s stale carry-forward with nothing to fall back on: a failing
fetcher drops its layer from the deployed site entirely instead of serving the
last good copy. That is how one removed CEC field took the utility-territories
layer off the map for four days in September 2026 — a 404 rather than a layer
labelled "cached" in the banner.

Seeding from what is already published restores that safety net. Strictly
best-effort: anything missing or malformed is skipped, so a cold start (nothing
published yet, or a renamed site) just fetches everything from upstream exactly
as before. This must never fail the build — a bad seed is worse than none, so
a file is only kept once it has parsed as JSON.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

SITE = os.environ.get("SITE", "https://dmang.com/ca-grid-weather-map/data").rstrip("/")
DATA = pathlib.Path("data")
TIMEOUT = 60

# Everything fetch_data.py writes. manifest.json matters most: without it
# _previous_manifest() returns {} and no layer can be marked stale even when
# its file is present.
FILES = [
    "manifest.json",
    "wind.json",
    "raws.geojson",
    "outage_points.geojson",
    "outage_areas.geojson",
    "psps.geojson",
    "nws_alerts.geojson",
    "pge_territory.geojson",
    "ca_counties.geojson",
    "ca_boundary.geojson",
    "wildfires.geojson",
    "fire_points.geojson",
    "calfire.geojson",
    "evacuations.geojson",
    "inciweb.geojson",
]


def seed(name: str) -> tuple[bool, str]:
    url = f"{SITE}/{name}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # network, DNS, timeout — all non-fatal
        return False, type(e).__name__

    if not raw:
        return False, "empty"
    try:
        # Parse before keeping: a truncated download is worse than no seed,
        # because a later fetch failure would publish the broken copy.
        json.loads(raw)
    except ValueError:
        return False, "not valid JSON"

    DATA.mkdir(exist_ok=True)
    (DATA / name).write_bytes(raw)
    return True, f"{len(raw) / 1024:.0f} KB"


def main() -> int:
    print(f"seeding data/ from {SITE}")
    ok = 0
    for name in FILES:
        got, detail = seed(name)
        print(f"  {'seeded ' if got else 'skipped'} {name:<26} {detail}")
        ok += got
    print(f"{ok}/{len(FILES)} files seeded")
    if ok == 0:
        print("nothing seeded — treating as a cold start, fetchers will "
              "populate everything from upstream")
    # Always succeed: seeding is an optimisation, never a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
