"""arcgis_query / arcgis_query_all — the retry and truncation traps.

Two upstream behaviours drive all of this:

1. ArcGIS Online returns rate-limit errors as **HTTP 200** with the error in
   the body, so `raise_for_status()` never sees them. That took the wildfire
   layer down and is what started this whole safety net.
2. It also truncates silently: RAWS holds 4,208 records and returns exactly
   2,000 with `exceededTransferLimit` *absent* — the flag only appears on
   page 2.
"""

import json
import unittest

import requests

from helpers import ARC_BAD_FIELD, ARC_QUOTA, FakeResponse, FetchDataTest, collection, fd


class InBodyErrors(FetchDataTest):
    def test_quota_error_is_detected_despite_http_200(self):
        self.stub_http(FakeResponse(ARC_QUOTA))
        with self.assertRaises(fd.ArcGISError) as ctx:
            fd.arcgis_query("http://x", {}, what="t", attempts=1)
        self.assertEqual(ctx.exception.code, 429)
        self.assertTrue(ctx.exception.transient)

    def test_quota_error_is_retried_then_raised(self):
        calls = self.stub_http(FakeResponse(ARC_QUOTA))
        with self.assertRaises(fd.ArcGISError):
            fd.arcgis_query("http://x", {}, what="t", attempts=3)
        self.assertEqual(len(calls), 3)

    def test_backoff_is_capped_below_the_quota_window(self):
        """Never wait out the full 60 s window — a caller with a mirror should
        fail over instead."""
        self.stub_http(FakeResponse(ARC_QUOTA))
        with self.assertRaises(fd.ArcGISError):
            fd.arcgis_query("http://x", {}, what="t", attempts=3, max_sleep=20.0)
        self.assertEqual(self.slept, [5.0, 10.0])
        self.assertLessEqual(max(self.slept), 20.0)

    def test_bad_field_fails_fast_without_retrying(self):
        """A 400 means our query is wrong; retrying just wastes the window.

        This is the shape of the CEC breakage: one removed field, whole query
        rejected.
        """
        calls = self.stub_http(FakeResponse(ARC_BAD_FIELD))
        with self.assertRaises(fd.ArcGISError) as ctx:
            fd.arcgis_query("http://x", {}, what="territory", attempts=3)
        self.assertEqual(len(calls), 1)
        self.assertFalse(ctx.exception.transient)
        self.assertIn("outFields", str(ctx.exception))

    def test_recovers_when_a_retry_succeeds(self):
        good = collection({"type": "Feature", "properties": {}, "geometry": None})
        self.stub_http(FakeResponse(ARC_QUOTA), FakeResponse(good))
        out = fd.arcgis_query("http://x", {}, what="t")
        self.assertEqual(len(out["features"]), 1)

    def test_missing_features_key_is_an_error(self):
        self.stub_http(FakeResponse({"totally": "unexpected"}))
        with self.assertRaises(fd.ArcGISError):
            fd.arcgis_query("http://x", {}, what="t", attempts=1)


class TransportFailures(FetchDataTest):
    def test_connection_error_is_retried(self):
        good = collection()
        self.stub_http(requests.ConnectionError("down"), FakeResponse(good))
        self.assertEqual(fd.arcgis_query("http://x", {}, what="t")["features"], [])

    def test_unparseable_body_is_retried(self):
        """An HTML error page or truncated response, not JSON."""
        good = collection()
        self.stub_http(FakeResponse(json.JSONDecodeError("boom", "", 0)), FakeResponse(good))
        self.assertEqual(fd.arcgis_query("http://x", {}, what="t")["features"], [])

    def test_http_500_is_retried(self):
        good = collection()
        self.stub_http(FakeResponse(None, status=500), FakeResponse(good))
        self.assertEqual(fd.arcgis_query("http://x", {}, what="t")["features"], [])


class Pagination(FetchDataTest):
    """arcgis_query_all exists because the truncation flag can't be trusted."""

    def _page(self, n, start=0):
        return collection(*[
            {"type": "Feature", "geometry": None, "properties": {"i": start + i}}
            for i in range(n)
        ])

    def test_pages_until_a_short_page_arrives(self):
        calls = self.stub_http(
            FakeResponse(self._page(3, 0)),
            FakeResponse(self._page(3, 3)),
            FakeResponse(self._page(1, 6)),
        )
        out = fd.arcgis_query_all("http://x", {}, what="t", page_size=3)
        self.assertEqual(len(out["features"]), 7)
        self.assertEqual([f["properties"]["i"] for f in out["features"]], list(range(7)))
        self.assertEqual(len(calls), 3)

    def test_offsets_advance_by_page_size(self):
        calls = self.stub_http(FakeResponse(self._page(2, 0)), FakeResponse(self._page(1, 2)))
        fd.arcgis_query_all("http://x", {}, what="t", page_size=2)
        self.assertEqual([c["params"]["resultOffset"] for c in calls], [0, 2])
        self.assertEqual({c["params"]["resultRecordCount"] for c in calls}, {2})

    def test_full_page_with_the_flag_absent_still_pages(self):
        """The RAWS trap: exactly maxRecordCount rows and no
        exceededTransferLimit. A single query would silently lose half the
        stations."""
        full = self._page(2, 0)
        self.assertNotIn("exceededTransferLimit", full)
        calls = self.stub_http(FakeResponse(full), FakeResponse(self._page(0, 2)))
        out = fd.arcgis_query_all("http://x", {}, what="t", page_size=2)
        self.assertEqual(len(calls), 2, "stopped after one full page")
        self.assertEqual(len(out["features"]), 2)

    def test_page_ceiling_stops_the_loop(self):
        """Every page comes back full, so only max_pages ends it."""
        calls = self.stub_http(FakeResponse(self._page(2, 0)))
        out = fd.arcgis_query_all("http://x", {}, what="t", page_size=2, max_pages=3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(out["features"]), 6)

    def test_single_short_page_makes_one_call(self):
        calls = self.stub_http(FakeResponse(self._page(1, 0)))
        fd.arcgis_query_all("http://x", {}, what="t", page_size=2000)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
