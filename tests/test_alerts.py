"""fetch_alerts: drawing NWS alerts that arrive without a polygon.

Red Flag Warnings and Fire Weather Watches are issued on *fire* weather zones
(CAZ270-285, NVZ4xx), which 404 on the /zones/forecast/ endpoint the bare UGC
code was looked up on. The Oct 8-9 2026 deploys dropped 3 of 5 matched alerts
that way, without counting them, and reported the layer ok.
"""

import unittest
from unittest import mock

from helpers import FakeResponse, FetchDataTest, collection, fd, needs_shapely

FIRE_ZONE = "https://api.weather.gov/zones/fire/CAZ270"
GONE_ZONE = "https://api.weather.gov/zones/fire/CAZ285"
FORECAST_ZONE = "https://api.weather.gov/zones/forecast/CAZ017"

# Squares well inside California, so the CA clip leaves them whole.
SQUARE = {"type": "Polygon", "coordinates": [[[-121, 40], [-120, 40], [-120, 41],
                                              [-121, 41], [-121, 40]]]}
OTHER_SQUARE = {"type": "Polygon", "coordinates": [[[-119, 36], [-118, 36], [-118, 37],
                                                    [-119, 37], [-119, 36]]]}

# Every property the frontend reads off nws_alerts.geojson.
ALERT_PROPS = {"event", "headline", "severity", "urgency", "certainty", "areaDesc",
               "senderName", "effective", "onset", "expires", "ends",
               "description", "instruction"}


def alert(event, geometry=None, affected=None, ugc=None, aid=None):
    props = {"id": aid or f"urn:{event}:{affected or ugc}", "event": event,
             "headline": f"{event} issued", "areaDesc": "Somewhere, CA",
             "geocode": {"UGC": ugc or []}}
    if affected is not None:
        props["affectedZones"] = affected
    return {"type": "Feature", "geometry": geometry, "properties": props}


@needs_shapely
class AlertZones(FetchDataTest):
    def setUp(self):
        super().setUp()
        from shapely.geometry import box
        ca = mock.patch.object(fd, "_get_ca_boundary", return_value=box(-124.5, 32.5, -114.1, 42.0))
        ca.start()
        self.addCleanup(ca.stop)
        # The zone cache is module-level; never let one test answer for another.
        cache = mock.patch.dict(fd._zone_cache, clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def run_alerts(self, *alerts, zones=None):
        """fetch_alerts against a feed of `alerts` and a {url: geometry} zone
        table; any other zone URL 404s. Returns (summary, fetched URLs)."""
        zones = zones or {}
        fetched = []

        def get(url, **kwargs):
            if url == fd.NWS_ALERTS_URL:
                return FakeResponse(collection(*alerts))
            fetched.append(url)
            if url in zones:
                return FakeResponse({"geometry": zones[url]})
            return FakeResponse(None, status=404)

        with mock.patch.object(fd.HTTP, "get", side_effect=get):
            out = fd.fetch_alerts()
        return out, fetched

    def test_fire_zone_geometry_comes_from_affected_zones(self):
        out, fetched = self.run_alerts(
            alert("Red Flag Warning", affected=[FIRE_ZONE], ugc=["CAZ270"]),
            zones={FIRE_ZONE: SQUARE})
        self.assertEqual(fetched, [FIRE_ZONE])
        self.assertNotIn("https://api.weather.gov/zones/forecast/CAZ270", fetched)
        self.assertEqual(out["alerts_total"], 1)
        self.assertEqual(out["by_event"], {"Red Flag Warning": 1})
        self.assertEqual(out["dropped_no_geometry"], 0)
        self.assertNotIn("warnings", out)
        written = self.read_data("nws_alerts.geojson")["features"]
        self.assertEqual(written[0]["properties"]["event"], "Red Flag Warning")
        self.assertEqual(set(written[0]["properties"]), ALERT_PROPS)

    def test_a_shared_zone_is_fetched_once(self):
        out, fetched = self.run_alerts(
            alert("Red Flag Warning", affected=[FIRE_ZONE], aid="a"),
            alert("Fire Weather Watch", affected=[FIRE_ZONE], aid="b"),
            zones={FIRE_ZONE: SQUARE})
        self.assertEqual(fetched, [FIRE_ZONE])
        self.assertEqual(out["alerts_total"], 2)

    def test_a_zone_that_404s_is_counted_and_warned_about(self):
        out, fetched = self.run_alerts(
            alert("Red Flag Warning", affected=[FIRE_ZONE], aid="a"),
            alert("Fire Weather Watch", affected=[GONE_ZONE], aid="b"),
            zones={FIRE_ZONE: SQUARE})
        self.assertIn(GONE_ZONE, fetched)
        self.assertEqual(out["alerts_total"], 1)
        self.assertEqual(out["dropped_no_geometry"], 1)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("1 of 2", out["warnings"][0])
        self.assertIn("Fire Weather Watch", out["warnings"][0])

    def test_an_alert_with_its_own_polygon_is_unchanged(self):
        out, fetched = self.run_alerts(
            alert("Wind Advisory", geometry=SQUARE, affected=[FORECAST_ZONE]),
            zones={FORECAST_ZONE: OTHER_SQUARE})
        self.assertEqual(fetched, [], "no zone lookup for an alert that has a shape")
        self.assertEqual(out["alerts_total"], 1)
        from shapely.geometry import shape
        drawn = shape(self.read_data("nws_alerts.geojson")["features"][0]["geometry"])
        self.assertTrue(drawn.equals(shape(SQUARE)))

    def test_a_url_off_weather_gov_is_never_fetched(self):
        """affectedZones is feed content; it must not make us GET anything."""
        out, fetched = self.run_alerts(
            alert("Red Flag Warning", affected=[
                "https://evil.example.com/zones/fire/CAZ270",
                "http://api.weather.gov/zones/fire/CAZ270",
                "https://api.weather.gov.evil.example.com/zones/fire/CAZ270",
                "https://user@api.weather.gov/zones/fire/CAZ270",
                "https://api.weather.gov/alerts/active?area=CA",
                "https://api.weather.gov/zones/fire/../../alerts",
            ]),
            zones={FIRE_ZONE: SQUARE})
        self.assertEqual(fetched, [])
        self.assertEqual(out["alerts_total"], 0)
        self.assertEqual(out["dropped_no_geometry"], 1)

    def test_good_zones_still_draw_beside_a_rejected_one(self):
        out, fetched = self.run_alerts(
            alert("Red Flag Warning", affected=["https://evil.example.com/x", FIRE_ZONE]),
            zones={FIRE_ZONE: SQUARE})
        self.assertEqual(fetched, [FIRE_ZONE])
        self.assertEqual(out["alerts_total"], 1)

    def test_ugc_fallback_only_without_affected_zones(self):
        out, fetched = self.run_alerts(
            alert("Wind Advisory", ugc=["CAZ017"]),
            zones={FORECAST_ZONE: SQUARE})
        self.assertEqual(fetched, [FORECAST_ZONE])
        self.assertEqual(out["alerts_total"], 1)


class ZoneUrl(FetchDataTest):
    """No shapely needed: the URL gate on its own."""

    def test_nws_zone_urls_are_rebuilt_canonically(self):
        self.assertEqual(fd._nws_zone_url(FIRE_ZONE), FIRE_ZONE)
        self.assertEqual(fd._nws_zone_url("https://API.weather.gov/zones/county/CAC001/"),
                         "https://api.weather.gov/zones/county/CAC001")

    def test_anything_else_is_refused(self):
        for bad in (None, 42, "", "zones/fire/CAZ270", "ftp://api.weather.gov/zones/fire/CAZ270",
                    "https://api.weather.gov:8443/zones/fire/CAZ270",
                    "https://api.weather.gov/zones/fire/CAZ270#frag",
                    "https://api.weather.gov/zones/fire/CAZ270?x=1",
                    "https://[api.weather.gov/zones/fire/CAZ270"):  # urlsplit raises
            self.assertIsNone(fd._nws_zone_url(bad), bad)


if __name__ == "__main__":
    unittest.main()
