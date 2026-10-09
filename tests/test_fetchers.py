"""Fetcher behaviour: failover, data-quality guards, and empty-result guards.

Each guard here exists because the unguarded version shipped bad data to the
live site at least once.
"""

import unittest
from unittest import mock

from helpers import ARC_QUOTA, FakeResponse, FetchDataTest, collection, fd, point


class FiresFailover(FetchDataTest):
    """NIFC's WFIGS layer spends real stretches of fire season over its
    org-wide quota; the Living Atlas mirror is a different org."""

    def test_wfigs_success_needs_no_mirror(self):
        with mock.patch.object(fd, "_fires_from_wfigs",
                               return_value=collection(point(-120, 38, poly_GISAcres=10))), \
             mock.patch.object(fd, "_fires_from_firis", return_value=[]):
            out = fd.fetch_fires()
        self.assertEqual(out["source"], "wfigs")

    def test_quota_error_fails_over_to_the_mirror(self):
        boom = fd.ArcGISError("fires (WFIGS): ArcGIS error 429: quota", code=429, transient=True)
        with mock.patch.object(fd, "_fires_from_wfigs", side_effect=boom), \
             mock.patch.object(fd, "_fires_from_mirror",
                               return_value=collection(point(-120, 38, poly_GISAcres=12.5))), \
             mock.patch.object(fd, "_fires_from_firis", return_value=[]):
            out = fd.fetch_fires()
        self.assertEqual(out["source"], "living_atlas")
        self.assertEqual(out["fires"], 1)

    def test_firis_outage_does_not_cost_us_the_wfigs_perimeters(self):
        """The FIRIS union is best-effort — losing it must not lose the base
        layer."""
        with mock.patch.object(fd, "_fires_from_wfigs",
                               return_value=collection(point(-120, 38, poly_GISAcres=10))), \
             mock.patch.object(fd, "_fires_from_firis", side_effect=RuntimeError("FIRIS down")):
            out = fd.fetch_fires()
        self.assertEqual(out["fires"], 1)
        self.assertEqual(out["firis_added"], 0)

    def test_perimeters_are_tagged_with_their_source(self):
        with mock.patch.object(fd, "_fires_from_wfigs",
                               return_value=collection(point(-120, 38, poly_GISAcres=10))), \
             mock.patch.object(fd, "_fires_from_firis", return_value=[]):
            fd.fetch_fires()
        written = self.read_data("wildfires.geojson")
        self.assertEqual(written["features"][0]["properties"]["_perimeter_source"], "WFIGS")


class RawsGustQC(FetchDataTest):
    """One anemometer reported 109 mph against a 6 mph sustained wind and set
    max_gust_mph for the whole state."""

    def test_the_real_bad_reading_is_rejected(self):
        self.assertTrue(fd._gust_is_suspect(6, 109))

    def test_a_plausible_ridge_top_gust_survives(self):
        self.assertFalse(fd._gust_is_suspect(30.1, 50.2))

    def test_a_modest_gust_on_a_calm_day_survives(self):
        """Low sustained wind gives a huge ratio, so the absolute floor is what
        keeps this reading."""
        self.assertFalse(fd._gust_is_suspect(3, 20))
        self.assertFalse(fd._gust_is_suspect(0, 20))

    def test_both_conditions_are_required(self):
        self.assertTrue(fd._gust_is_suspect(0, 45))       # calm + over the floor
        self.assertFalse(fd._gust_is_suspect(15, 45))     # ratio exactly 3.0, not over
        self.assertTrue(fd._gust_is_suspect(14, 45))      # ratio 3.2 and over the floor

    def test_missing_readings_are_not_suspect(self):
        self.assertFalse(fd._gust_is_suspect(None, 50))
        self.assertFalse(fd._gust_is_suspect(10, None))

    def _stub_stations(self, *stations):
        # Padded with calm, gustless stations up to the floor RawsMinimum covers,
        # so these tests exercise QC rather than the short-count guard.
        filler = [point(-120, 37, station_name=f"FILL{i}", wind_speed_mph=2.0, wind_gust_mph=None)
                  for i in range(max(0, fd.RAWS_MIN_STATIONS - len(stations)))]
        return mock.patch.object(fd, "arcgis_query_all",
                                 return_value=collection(*stations, *filler))

    def test_bad_sensor_is_nulled_flagged_and_excluded_from_the_max(self):
        with self._stub_stations(
            point(-121, 39, station_name="BROKEN", wind_speed_mph=6.0, wind_gust_mph=109.0),
            point(-120, 38, station_name="GOOD", wind_speed_mph=30.1, wind_gust_mph=50.2),
        ):
            out = fd.fetch_raws()
        self.assertEqual(out["gusts_rejected"], 1)
        self.assertEqual(out["max_gust_mph"], 50.2)
        bad = [f["properties"] for f in self.read_data("raws.geojson")["features"]
               if f["properties"]["station_name"] == "BROKEN"][0]
        self.assertIsNone(bad["wind_gust_mph"])
        self.assertEqual(bad["_gust_rejected"], 109.0, "raw value kept for the popup")

    def test_zero_stations_is_an_error_not_calm_weather(self):
        with mock.patch.object(fd, "arcgis_query_all", return_value=collection()):
            with self.assertRaises(RuntimeError) as ctx:
                fd.fetch_raws()
        self.assertIn("no stations", str(ctx.exception))


class RawsMinimum(FetchDataTest):
    """12 of 100 deploys from Sep 15 to Oct 9 2026 published 532-1,864 stations
    as ok while the upstream layer was emptied and refilled. The query has no
    filter, so a short count is never real."""

    PREV = {
        "generated_at": "2026-10-08T21:29:00+00:00",
        "raws": {"status": "ok", "duration_s": 0.7, "stations": 4208,
                 "max_gust_mph": 47.0, "gusts_rejected": 1, "reporting_wind": 4100},
    }

    def _stations(self, n):
        return collection(*[point(-120 + i * 1e-4, 37, station_name=f"S{i}",
                                  wind_speed_mph=5.0, wind_gust_mph=9.0) for i in range(n)])

    def test_a_short_count_raises_and_says_why(self):
        with mock.patch.object(fd, "arcgis_query_all", return_value=self._stations(586)):
            with self.assertRaises(RuntimeError) as ctx:
                fd.fetch_raws()
        self.assertIn("586", str(ctx.exception))
        self.assertIn("refill", str(ctx.exception))
        self.assertFalse((self.data_dir / "raws.geojson").exists(), "nothing written")

    def test_a_normal_count_passes(self):
        with mock.patch.object(fd, "arcgis_query_all",
                               return_value=self._stations(fd.RAWS_MIN_STATIONS)):
            self.assertEqual(fd.fetch_raws()["stations"], fd.RAWS_MIN_STATIONS)

    def test_run_one_serves_the_last_full_file_as_stale(self):
        self.write_data("manifest.json", self.PREV)
        self.write_data("raws.geojson", self._stations(5))  # stands in for the 4,208
        before = (self.data_dir / "raws.geojson").read_bytes()
        with mock.patch.object(fd, "arcgis_query_all", return_value=self._stations(586)):
            entry = fd.run_one("raws", fd.fetch_raws, fd._previous_manifest(), "raws.geojson")
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["stations"], 4208, "previous count carried forward")
        self.assertIn("586", entry["error"])
        self.assertEqual((self.data_dir / "raws.geojson").read_bytes(), before)


class CountiesGuards(FetchDataTest):
    """ca_counties.geojson was committed with zero features at least five
    times, each reporting status ok, blanking the county layer statewide."""

    def _county(self, name):
        return {"type": "Feature", "geometry": None, "properties": {"NAME": name}}

    def test_empty_result_raises(self):
        with mock.patch.object(fd, "arcgis_query", return_value=collection()):
            with self.assertRaises(RuntimeError) as ctx:
                fd.fetch_counties()
        self.assertIn("no features", str(ctx.exception))

    def test_doubled_rows_are_deduped(self):
        """The same layer has also served 116 rows for 58 counties."""
        doubled = collection(self._county("Alpine"), self._county("Inyo"),
                             self._county("Alpine"), self._county("Inyo"))
        with mock.patch.object(fd, "arcgis_query", return_value=doubled):
            out = fd.fetch_counties()
        self.assertEqual(out["counties"], 2)
        self.assertEqual(len(self.read_data("ca_counties.geojson")["features"]), 2)

    def test_a_normal_result_passes_through(self):
        with mock.patch.object(fd, "arcgis_query",
                               return_value=collection(*[self._county(f"C{i}") for i in range(58)])):
            self.assertEqual(fd.fetch_counties()["counties"], 58)


class TerritoryGuard(FetchDataTest):
    def test_empty_result_raises(self):
        with mock.patch.object(fd, "arcgis_query", return_value=collection()):
            with self.assertRaises(RuntimeError):
                fd.fetch_territory()

    def test_a_dropped_field_surfaces_as_a_non_transient_error(self):
        """CEC removing Sales_GWh_2023 produced exactly this, and it must not
        be retried as if it were a quota blip."""
        err = fd.ArcGISError("territory: ArcGIS error 400: 'outFields' parameter is invalid",
                             code=400, transient=False)
        with mock.patch.object(fd, "arcgis_query", side_effect=err):
            with self.assertRaises(fd.ArcGISError) as ctx:
                fd.fetch_territory()
        self.assertFalse(ctx.exception.transient)

    def test_acronym_whitespace_is_stripped(self):
        feat = point(-120, 38, Acronym="  PGE  ", Utility="Pacific Gas", Type="IOU")
        with mock.patch.object(fd, "arcgis_query", return_value=collection(feat)):
            fd.fetch_territory()
        written = self.read_data("pge_territory.geojson")
        self.assertEqual(written["features"][0]["properties"]["Acronym"], "PGE")


class CalFireOverlay(FetchDataTest):
    def test_response_that_is_not_a_list_is_rejected(self):
        self.stub_http(FakeResponse({"unexpected": "shape"}))
        with self.assertRaises(RuntimeError) as ctx:
            fd.fetch_calfire()
        self.assertIn("expected a list", str(ctx.exception))

    def test_records_without_coordinates_are_dropped_not_placed_at_null_island(self):
        self.stub_http(FakeResponse([
            {"Name": "Good Fire", "Latitude": 38.1, "Longitude": -120.7, "IsActive": True},
            {"Name": "No Coords", "Latitude": None, "Longitude": None, "IsActive": True},
            {"Name": "Zero Coords", "Latitude": 0, "Longitude": 0, "IsActive": True},
        ]))
        out = fd.fetch_calfire()
        self.assertEqual(out["incidents"], 1)
        self.assertEqual(out["dropped_no_coords"], 2)

    def test_transient_failure_is_retried(self):
        good = [{"Name": "F", "Latitude": 38.0, "Longitude": -120.0, "IsActive": True}]
        self.stub_http(FakeResponse(None, status=503), FakeResponse(good))
        self.assertEqual(fd.fetch_calfire()["incidents"], 1)


if __name__ == "__main__":
    unittest.main()
