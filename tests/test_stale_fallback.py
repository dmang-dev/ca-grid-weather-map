"""run_one()'s stale carry-forward, and the seed step that keeps it alive.

A dead upstream doesn't invalidate what's already on disk: nothing overwrote
it, so the map can keep drawing the previous snapshot as long as the banner
says it's cached. That only works if a previous snapshot exists — which is
exactly what broke when data/ stopped being committed, and why the deploy now
seeds from the published site first.
"""

import unittest

from helpers import FetchDataTest, collection, fd, point

PREV_MANIFEST = {
    "generated_at": "2026-09-10T07:58:03+00:00",
    "territory": {"status": "ok", "duration_s": 0.7, "features": 54,
                  "iou_count": 6, "pou_count": 48},
}


def boom():
    raise fd.ArcGISError("territory: ArcGIS error 400: 'outFields' parameter is invalid")


class WithoutASeededSnapshot(FetchDataTest):
    """How a fresh runner behaved before the seed step — the September
    territory outage."""

    def test_no_previous_manifest_is_readable(self):
        self.assertEqual(fd._previous_manifest(), {})

    def test_failure_is_a_hard_error(self):
        entry = fd.run_one("territory", boom, {}, "pge_territory.geojson")
        self.assertEqual(entry["status"], "error")

    def test_nothing_is_left_for_the_site_to_serve(self):
        fd.run_one("territory", boom, {}, "pge_territory.geojson")
        self.assertFalse((self.data_dir / "pge_territory.geojson").exists())


class WithASeededSnapshot(FetchDataTest):
    """How it behaves now: the layer degrades instead of disappearing."""

    def setUp(self):
        super().setUp()
        self.write_data("manifest.json", PREV_MANIFEST)
        self.write_data("pge_territory.geojson", collection(point(-120, 38, Acronym="PGE")))

    def test_previous_manifest_is_loaded(self):
        self.assertIn("territory", fd._previous_manifest())

    def test_failure_reports_stale_not_error(self):
        entry = fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        self.assertEqual(entry["status"], "stale")

    def test_previous_counts_are_carried_forward(self):
        entry = fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        self.assertEqual(entry["features"], 54)
        self.assertEqual(entry["iou_count"], 6)

    def test_as_of_is_recorded_for_the_banner(self):
        entry = fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        self.assertEqual(entry["as_of"], PREV_MANIFEST["generated_at"])

    def test_the_error_is_kept_for_diagnosis(self):
        entry = fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        self.assertIn("outFields", entry["error"])

    def test_the_last_good_file_is_left_intact(self):
        before = (self.data_dir / "pge_territory.geojson").read_bytes()
        fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        self.assertEqual((self.data_dir / "pge_territory.geojson").read_bytes(), before)

    def test_as_of_does_not_drift_across_repeated_failures(self):
        """A layer stale for a day must report when it was last *good*, not
        when it last failed."""
        first = fd.run_one("territory", boom, fd._previous_manifest(), "pge_territory.geojson")
        later = {"generated_at": "2026-09-10T20:00:00+00:00", "territory": first}
        second = fd.run_one("territory", boom, later, "pge_territory.geojson")
        self.assertEqual(second["as_of"], PREV_MANIFEST["generated_at"])

    def test_a_missing_file_still_gives_a_hard_error(self):
        """Manifest present but the data file absent: nothing to serve, so
        don't claim staleness."""
        entry = fd.run_one("territory", boom, fd._previous_manifest(), "not-seeded.geojson")
        self.assertEqual(entry["status"], "error")


class SuccessPath(FetchDataTest):
    def test_success_reports_ok_and_never_carries_forward(self):
        entry = fd.run_one("thing", lambda: {"count": 3}, PREV_MANIFEST, "whatever.geojson")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["count"], 3)
        self.assertNotIn("as_of", entry)
        self.assertNotIn("error", entry)


class ExitCode(FetchDataTest):
    """Derived from the results rather than a hardcoded layer list, so adding
    a fetcher can't silently skip it."""

    def test_any_ok_layer_means_success(self):
        results = {"generated_at": "x", "a": {"status": "error"}, "b": {"status": "ok"}}
        ok = sum(1 for v in results.values()
                 if isinstance(v, dict) and v.get("status") == "ok")
        self.assertEqual(0 if ok > 0 else 1, 0)

    def test_everything_failed_means_failure(self):
        results = {"generated_at": "x", "a": {"status": "error"}, "b": {"status": "stale"}}
        ok = sum(1 for v in results.values()
                 if isinstance(v, dict) and v.get("status") == "ok")
        self.assertEqual(0 if ok > 0 else 1, 1)


if __name__ == "__main__":
    unittest.main()
