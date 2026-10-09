"""fetch_fire_points: which incident points a perimeter already draws.

_has_perimeter decides whether the map plays a point down (mapped) or draws it
boldly (the dot is all there is). Getting it wrong toward "mapped" hides a fire,
so name matches that could be coincidence have to land near the perimeter.
"""

import unittest
from unittest import mock

from helpers import FetchDataTest, collection, fd, point, polygon, square

INYO_IRWIN_54 = "{553795BB-902A-4D1C-9A22-5F4609EC8141}"
SAN_DIEGO_IRWIN = "{12AA2AB8-F848-4DF3-865F-DC50BAA1F18C}"


class FirePointsTest(FetchDataTest):
    def run_points(self, *points, perimeters=None):
        if perimeters is not None:
            self.write_data("wildfires.geojson", collection(*perimeters))
        with mock.patch.object(fd, "_fire_points_from_wfigs", return_value=collection(*points)):
            out = fd.fetch_fire_points()
        written = {f["properties"]["IncidentName"]: f["properties"]
                   for f in self.read_data("fire_points.geojson")["features"]}
        return out, written


class ColdStart(FirePointsTest):
    """No wildfires.geojson at all — WFIGS and its mirror both down on a fresh
    runner. The backfill's early exit returned a bare 0 into a two-value unpack
    and crashed the fetcher after it had already written its file."""

    def test_no_perimeter_file_is_not_a_crash(self):
        out, written = self.run_points(point(-120.5, 38.1, IncidentName="GANN", IrwinID=None))
        self.assertEqual(out["perimeters_enriched"], 0)
        self.assertEqual(out["perimeters_via_alias"], 0)
        self.assertEqual(out["without_perimeter"], 1)
        self.assertFalse(written["GANN"]["_has_perimeter"], "fail toward visibility")

    def test_backfill_reports_two_counts_either_way(self):
        self.assertEqual(fd._backfill_perimeter_attrs([]), (0, 0))


class HasPerimeter(FirePointsTest):
    def test_the_san_diego_54_is_not_mapped_by_inyos_fires(self):
        """Aug 4 2026: point "54" in San Diego County matched WFIGS's "5-4" and
        CINDERCOMPLEX-5-4's alias "54", both ~500 km away in Inyo."""
        perimeters = [
            square(37.032, -118.251, attr_IncidentName="5-4", attr_IrwinID=INYO_IRWIN_54),
            square(37.010, -118.295, attr_IncidentName="CINDERCOMPLEX-5-4", attr_IrwinID=None),
        ]
        _, written = self.run_points(
            point(-117.0685, 32.6604, IncidentName="54", IrwinID=SAN_DIEGO_IRWIN),
            point(-118.2580, 37.0158, IncidentName="5-4", IrwinID=INYO_IRWIN_54),
            perimeters=perimeters)
        self.assertFalse(written["54"]["_has_perimeter"])
        self.assertTrue(written["5-4"]["_has_perimeter"], "its own fire still matches")

    def test_an_alias_hit_nearby_counts(self):
        _, written = self.run_points(
            point(-118.284, 36.996, IncidentName="5-3", IrwinID=None),
            perimeters=[square(37.032, -118.251, attr_IncidentName="CINDERCOMPLEX-5-3")])
        self.assertTrue(written["5-3"]["_has_perimeter"])

    def test_an_alias_hit_far_away_does_not(self):
        _, written = self.run_points(
            point(-117.07, 32.66, IncidentName="5-3", IrwinID=None),
            perimeters=[square(37.032, -118.251, attr_IncidentName="CINDERCOMPLEX-5-3")])
        self.assertFalse(written["5-3"]["_has_perimeter"])

    def test_a_firis_primary_name_needs_no_position(self):
        """FIRIS perimeters have no IRWIN id; the name is all there is, and a
        full name like GANN is specific enough. An origin point can sit well
        outside a grown perimeter."""
        _, written = self.run_points(
            point(-120.0, 39.0, IncidentName="Gann Fire", IrwinID="{A}"),
            perimeters=[square(38.10, -120.77, attr_IncidentName="GANN", attr_IrwinID=None)])
        self.assertTrue(written["Gann Fire"]["_has_perimeter"])

    def test_a_big_fires_origin_far_from_its_middle_still_counts(self):
        """A Dixie-sized WFIGS perimeter, and a point at its origin with no
        IRWIN id to match by (CAL FIRE's own, once the WFIGS points feed has
        let the fire go): ~50 km from the perimeter's centre, but inside it."""
        ring = [[-121.45, 39.82], [-120.60, 40.05], [-120.55, 40.60], [-121.20, 40.62],
                [-121.50, 40.20], [-121.45, 39.82]]
        dixie = polygon(ring, attr_IncidentName="DIXIE", attr_IrwinID="{ABC}")
        self.assertGreater(fd._km_apart(39.871, -121.389, *fd._geom_center(dixie["geometry"])),
                           fd.ALIAS_CONFIRM_KM)
        out, written = self.run_points(
            point(-121.389, 39.871, IncidentName="Dixie Fire", IrwinID=None),
            perimeters=[dixie])
        self.assertTrue(written["Dixie Fire"]["_has_perimeter"])
        self.assertEqual(out["without_perimeter"], 0)

    def test_a_wfigs_name_hit_nearby_still_rescues_an_irwin_mismatch(self):
        _, written = self.run_points(
            point(-120.75, 38.12, IncidentName="GANN", IrwinID="{OTHER}"),
            perimeters=[square(38.10, -120.77, attr_IncidentName="GANN", attr_IrwinID="{A}")])
        self.assertTrue(written["GANN"]["_has_perimeter"])

    def test_irwin_match_needs_no_name(self):
        _, written = self.run_points(
            point(-117.0, 33.0, IncidentName="Different Name", IrwinID="{a7c8}"),
            perimeters=[square(38.10, -120.77, attr_IncidentName="X", attr_IrwinID="{A7C8}")])
        self.assertTrue(written["Different Name"]["_has_perimeter"])


if __name__ == "__main__":
    unittest.main()
