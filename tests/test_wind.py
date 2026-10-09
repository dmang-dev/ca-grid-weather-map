"""HRRR wind: grid-relative U/V, and the run time the banner shows.

HRRR's 10 m U/V are relative to its Lambert conformal grid
(uvRelativeToGrid=1, LoV 262.5, Latin1=Latin2=38.5), and neither Herbie nor
cfgrib rotates them. Published as eastward/northward they leaned 10-17 degrees
clockwise across California (SF 15.5) and disagreed with the RAWS stations.
"""

import json
import math
import sys
import types
import unittest
from datetime import datetime
from unittest import mock

import numpy as np

from helpers import FetchDataTest, fd

HRRR = {"GRIB_uvRelativeToGrid": 1, "GRIB_gridType": "lambert", "GRIB_LoVInDegrees": 262.5,
        "GRIB_Latin1InDegrees": 38.5, "GRIB_Latin2InDegrees": 38.5}
LAMBERT = (262.5, 38.5, 38.5)


def bearing(u, v):
    """Direction a vector points, degrees clockwise from north."""
    return math.degrees(math.atan2(u, v))


class GridToEarth(FetchDataTest):
    def test_grid_north_at_san_francisco_points_west_of_north(self):
        ue, ve = fd._grid_to_earth_winds(0.0, 1.0, -122.4, *LAMBERT)
        self.assertAlmostEqual(bearing(ue, ve), -15.5, delta=0.1)

    def test_longitude_convention_does_not_matter(self):
        """cfgrib hands back 0-360; fetch_wind normalizes to -180..180."""
        a = fd._grid_to_earth_winds(0.0, 1.0, -122.4, *LAMBERT)
        b = fd._grid_to_earth_winds(0.0, 1.0, 360 - 122.4, *LAMBERT)
        np.testing.assert_allclose(a, b, atol=1e-12)

    def test_no_rotation_on_the_central_meridian(self):
        ue, ve = fd._grid_to_earth_winds(3.0, -4.0, -97.5, *LAMBERT)
        self.assertAlmostEqual(float(ue), 3.0, places=12)
        self.assertAlmostEqual(float(ve), -4.0, places=12)

    def test_speed_is_preserved_everywhere(self):
        rng = np.random.default_rng(7)
        u = rng.uniform(-30, 30, (40, 50))
        v = rng.uniform(-30, 30, (40, 50))
        lon = np.broadcast_to(np.linspace(-134, -60, 50), (40, 50))
        ue, ve = fd._grid_to_earth_winds(u, v, lon, *LAMBERT)
        np.testing.assert_allclose(np.hypot(ue, ve), np.hypot(u, v), rtol=1e-12)
        self.assertFalse(np.allclose(ue, u), "and it did rotate something")

    def test_rotation_grows_with_distance_from_lov(self):
        """Crescent City (-124.2) leans further than Needles (-114.6)."""
        cc = bearing(*fd._grid_to_earth_winds(0.0, 1.0, -124.2, *LAMBERT))
        nd = bearing(*fd._grid_to_earth_winds(0.0, 1.0, -114.6, *LAMBERT))
        self.assertAlmostEqual(cc, -16.6, delta=0.1)
        self.assertAlmostEqual(nd, -10.6, delta=0.1)

    def test_secant_cone_approaches_the_tangent_one(self):
        tangent = fd._grid_to_earth_winds(0.0, 1.0, -122.4, 262.5, 38.5, 38.5)
        secant = fd._grid_to_earth_winds(0.0, 1.0, -122.4, 262.5, 38.5, 38.5 + 1e-6)
        np.testing.assert_allclose(secant, tangent, atol=1e-6)
        # A real secant pair: the cone constant lies between sin(33) and sin(45).
        ue, ve = fd._grid_to_earth_winds(0.0, 1.0, -122.4, 262.5, 33.0, 45.0)
        n = -bearing(ue, ve) / 24.9
        self.assertTrue(math.sin(math.radians(33)) < n < math.sin(math.radians(45)), n)


class EarthRelativePolicy(FetchDataTest):
    def test_grid_relative_lambert_is_rotated(self):
        ue, ve = fd._earth_relative_winds(np.array([0.0]), np.array([1.0]),
                                          np.array([-122.4]), HRRR)
        self.assertAlmostEqual(bearing(ue[0], ve[0]), -15.5, delta=0.1)

    def test_already_earth_relative_is_left_alone(self):
        u, v = np.array([1.0]), np.array([2.0])
        ue, ve = fd._earth_relative_winds(u, v, np.array([-122.4]),
                                          {**HRRR, "GRIB_uvRelativeToGrid": 0})
        self.assertIs(ue, u)
        self.assertIs(ve, v)

    def test_unknown_orientation_refuses_rather_than_guessing(self):
        """Raising keeps the last good wind.json (stale) instead of publishing a
        field that might be rotated."""
        broken = [
            {},                                                     # no GRIB attrs at all
            {k: v for k, v in HRRR.items() if k != "GRIB_uvRelativeToGrid"},
            {k: v for k, v in HRRR.items() if k != "GRIB_LoVInDegrees"},
            {**HRRR, "GRIB_gridType": "polar_stereographic"},
        ]
        for attrs in broken:
            with self.assertRaises(RuntimeError, msg=attrs):
                fd._earth_relative_winds(np.zeros(1), np.zeros(1), np.zeros(1), attrs)


class _Arr:
    def __init__(self, values, attrs=None, coords=None):
        self.values = values
        self.attrs = attrs or {}
        self._coords = coords or {}

    def __getitem__(self, name):
        return self._coords[name]


class FetchWindOffline(FetchDataTest):
    """fetch_wind end to end against a fake Herbie: a uniform grid-north wind on
    a patch of HRRR-like grid around the Bay Area."""

    RUN = datetime(2026, 10, 8, 23)  # Herbie's dates are naive UTC

    def setUp(self):
        super().setUp()
        lats, lons = np.meshgrid(np.arange(36.0, 39.001, 0.05),
                                 np.arange(236.0, 239.001, 0.05), indexing="ij")
        coords = {"latitude": _Arr(lats), "longitude": _Arr(lons)}
        u = _Arr(np.zeros_like(lats), HRRR, coords)
        v = _Arr(np.full_like(lats, 10.0), HRRR, coords)
        run = self.RUN

        class FakeHerbie:
            def __init__(self, *args, **kwargs):
                self.grib = "s3://fake"
                self.date = run

            def xarray(self, search, remove_grib=False):
                return {"u10": u, "v10": v}

        patches = [
            mock.patch.dict(sys.modules, {"herbie": types.SimpleNamespace(Herbie=FakeHerbie)}),
            mock.patch.object(fd, "PGE_BBOX", {"lat_min": 37.0, "lat_max": 38.0,
                                               "lon_min": -123.0, "lon_max": -122.0}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_published_winds_are_earth_relative(self):
        fd.fetch_wind()
        u_rec, v_rec = json.loads((self.data_dir / "wind.json").read_text())
        h = u_rec["header"]
        u = np.array(u_rec["data"]).reshape(h["ny"], h["nx"])
        v = np.array(v_rec["data"]).reshape(h["ny"], h["nx"])
        col = int(round((-122.4 - h["lo1"]) / h["dx"]))
        self.assertAlmostEqual(bearing(u[0, col], v[0, col]), -15.5, delta=0.2)
        np.testing.assert_allclose(np.hypot(u, v), 10.0, atol=0.01)

    def test_run_time_carries_its_utc_offset(self):
        """isoformat() on a naive date wrote no offset, and browsers read that
        as local time — the banner put the run 7-8 h late."""
        out = fd.fetch_wind()
        self.assertEqual(out["run"], "2026-10-08T23:00:00Z")
        u_rec, _ = json.loads((self.data_dir / "wind.json").read_text())
        self.assertEqual(out["run"], u_rec["header"]["refTime"])


if __name__ == "__main__":
    unittest.main()
