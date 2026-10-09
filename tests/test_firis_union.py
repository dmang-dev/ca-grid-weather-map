"""fetch_fires: unioning FIRIS heat perimeters into WFIGS without drawing a fire twice.

A FIRIS perimeter merged with a WFIGS one only on an exact name match, so the
same fire under another name was appended again. The Aug 4 2026 snapshot drew
4 of its 37 fires twice: "3-6 WAGON" as FIRIS "WAGON", WFIGS "5-3"/"5-4" as
CINDERCOMPLEX-5-4/-5-3 (cross-named), and FIRIS's umbrella CINDER COMPLEX over
both. Merging is conservative on purpose — a wrong merge hides a real fire —
so every rule here has a case that must stay separate.
"""

import itertools
import sys
import unittest
from unittest import mock

from helpers import FetchDataTest, collection, fd, needs_shapely, point, polygon, square

OLD, NEW = 1_784_000_000_000, 1_785_000_000_000  # poly_DateCurrent, epoch ms


def rect(lon0, lat0, lon1, lat1, **props):
    return polygon([[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]], **props)


def wfigs(feature, name, irwin=None, **props):
    feature["properties"].update({
        "attr_IncidentName": name, "poly_IncidentName": name.title(), "poly_GISAcres": 100.0,
        "poly_DateCurrent": NEW, "attr_IrwinID": irwin, "attr_PercentContained": 40,
        "attr_POOCounty": "Inyo", "attr_FireCause": "Natural", **props})
    return feature


def firis(feature, name, date=OLD, acres=120.0):
    feature["properties"].update({
        "attr_IncidentName": name, "poly_IncidentName": name, "poly_GISAcres": acres,
        "poly_DateCurrent": date, "attr_IrwinID": None, "attr_PercentContained": None,
        "attr_POOCounty": None, "attr_FireCause": None, "_perimeter_source": "FIRIS"})
    return feature


class FirisUnionTest(FetchDataTest):
    def run_fires(self, base, extra):
        with mock.patch.object(fd, "_fires_from_wfigs", return_value=collection(*base)), \
             mock.patch.object(fd, "_fires_from_firis", return_value=list(extra)):
            out = fd.fetch_fires()
        feats = self.read_data("wildfires.geojson")["features"]
        return out, {f["properties"]["attr_IncidentName"]: f for f in feats}


class SameNameSwap(FirisUnionTest):
    """CLAUDE.md: WFIGS's own values stay authoritative. A fresher flight
    brings its shape, not its blanks."""

    def test_fresher_flight_swaps_geometry_and_keeps_wfigs_attributes(self):
        mine = firis(square(38.1, -120.77, half=0.03), "GANN", date=NEW + 1, acres=3760.0)
        out, fires = self.run_fires(
            [wfigs(square(38.1, -120.77), "GANN", irwin="abc", poly_GISAcres=3000.0)], [mine])
        p = fires["GANN"]["properties"]
        self.assertEqual(out["firis_fresher"], 1)
        self.assertEqual(out["firis_merged"], 0, "a name match is not a merge")
        self.assertEqual(fires["GANN"]["geometry"], mine["geometry"])
        self.assertEqual((p["poly_GISAcres"], p["poly_DateCurrent"], p["_perimeter_source"]),
                         (3760.0, NEW + 1, "FIRIS"))
        self.assertEqual((p["attr_IrwinID"], p["attr_PercentContained"], p["attr_POOCounty"],
                          p["attr_FireCause"], p["poly_IncidentName"]),
                         ("abc", 40, "Inyo", "Natural", "Gann"))

    def test_older_flight_is_dropped(self):
        out, fires = self.run_fires([wfigs(square(38.1, -120.77), "GANN")],
                                    [firis(square(38.1, -120.77, half=0.03), "GANN")])
        self.assertEqual(out["fires"], 1)
        self.assertEqual(fires["GANN"]["properties"]["_perimeter_source"], "WFIGS")


@needs_shapely
class AliasMatch(FirisUnionTest):
    """(a) CINDERCOMPLEX-5-3 answers to "53" — but only near the fire."""

    def test_alias_nearby_is_the_same_fire_even_when_it_has_grown(self):
        # The new flight is 6x the old perimeter: too big for the overlap rule.
        grown = firis(square(37.0, -118.3, half=0.05), "CINDERCOMPLEX-5-3", date=NEW + 1)
        out, fires = self.run_fires([wfigs(square(37.0, -118.3), "5-3")], [grown])
        self.assertEqual(out["fires"], 1)
        self.assertEqual((out["firis_merged"], out["firis_fresher"], out["firis_added"]), (1, 1, 0))
        self.assertEqual(fires["5-3"]["geometry"], grown["geometry"])

    def test_alias_far_away_is_a_different_fire(self):
        out, fires = self.run_fires([wfigs(square(37.0, -118.3), "5-3")],
                                    [firis(square(32.66, -117.07), "CINDERCOMPLEX-5-3")])
        self.assertEqual(out["fires"], 2)
        self.assertEqual(out["firis_merged"], 0)


@needs_shapely
class OverlapMatch(FirisUnionTest):
    """(b) Shared area >= 50% of EACH polygon."""

    def test_wagon_under_two_names_is_one_fire(self):
        # ~86% of FIRIS's polygon, ~95% of WFIGS's — and only 86% covered, so
        # the cover rule alone would not catch it.
        out, fires = self.run_fires(
            [wfigs(square(40.5, -120.5), "3-6 WAGON")],
            [firis(square(40.5, -120.497, half=0.021), "WAGON")])
        self.assertEqual(out["fires"], 1)
        self.assertEqual((out["firis_merged"], out["firis_added"]), (1, 0))
        self.assertIn("3-6 WAGON", fires)

    def test_overlap_beats_a_cross_wired_alias(self):
        """WFIGS's "5-3" carried 5-4's polygon on Aug 4. The fresher flight
        must land on the feature with its shape, not the one with its name."""
        a = wfigs(square(37.03, -118.25, half=0.015), "5-3")
        b = wfigs(square(37.01, -118.30, half=0.015), "5-4")
        flight = firis(square(37.01, -118.30, half=0.016), "CINDERCOMPLEX-5-3", date=NEW + 1)
        out, fires = self.run_fires([a, b], [flight])
        self.assertEqual(out["fires"], 2)
        self.assertEqual(fires["5-4"]["geometry"], flight["geometry"])
        self.assertEqual(fires["5-3"]["geometry"], square(37.03, -118.25, half=0.015)["geometry"])

    def test_a_fresh_subfire_flight_does_not_shrink_its_complex(self):
        """WFIGS draws ZEUS COMPLEX as one perimeter; FIRIS flies its western
        60% as a sub-fire. That holds half of each, but swapping it in would
        leave the other 40% of the complex undrawn."""
        complex_ = wfigs(rect(-118.40, 36.97, -118.30, 37.03), "ZEUS COMPLEX",
                         poly_GISAcres=5000.0, poly_DateCurrent=OLD)
        sub = firis(rect(-118.40, 36.97, -118.34, 37.03), "ZEUSCOMPLEX-A1",
                    date=NEW, acres=3000.0)
        out, fires = self.run_fires([complex_], [sub])
        self.assertEqual((out["fires"], out["total_acres"]), (1, 5000))
        self.assertEqual((out["firis_merged"], out["firis_fresher"], out["firis_covered"]),
                         (0, 0, 1))
        self.assertEqual(fires["ZEUS COMPLEX"]["geometry"],
                         rect(-118.40, 36.97, -118.30, 37.03)["geometry"])

    def test_an_older_complex_flight_is_not_dropped_behind_one_subfire(self):
        """The mirror case: WFIGS has only "5-3" so far, FIRIS an older flight
        of the whole complex. Dropping it would hide the 31% that is 5-4."""
        sub = wfigs(rect(-118.33, 36.97, -118.27, 37.03), "5-3")
        umbrella = firis(rect(-118.331, 36.969, -118.249, 37.031), "CINDER COMPLEX",
                         date=OLD, acres=2784.0)
        out, fires = self.run_fires([sub], [umbrella])
        self.assertEqual(out["fires"], 2)
        self.assertEqual((out["firis_added"], out["firis_merged"]), (1, 0))
        self.assertEqual(set(fires), {"5-3", "CINDER COMPLEX"})

    def test_adjacent_fires_with_a_small_overlap_stay_separate(self):
        out, fires = self.run_fires(
            [wfigs(rect(-120.00, 38.0, -119.96, 38.04), "ALPHA")],
            [firis(rect(-119.97, 38.0, -119.93, 38.04), "BRAVO", date=NEW + 1)])
        self.assertEqual(out["fires"], 2)
        self.assertEqual((out["firis_added"], out["firis_merged"], out["firis_covered"]), (1, 0, 0))
        self.assertEqual(fires["ALPHA"]["properties"]["_perimeter_source"], "WFIGS")


@needs_shapely
class CoveredUmbrella(FirisUnionTest):
    """(c) A perimeter >= 90% inside ones already drawn adds nothing."""

    def setUp(self):
        super().setUp()
        self.sub_a = wfigs(rect(-118.33, 36.97, -118.27, 37.03), "5-3")
        self.sub_b = wfigs(rect(-118.27, 36.97, -118.25, 37.03), "5-4")
        # 94% covered by the two, and 71% of it is "5-3" — enough to pass the
        # overlap rule with that one fire if holding a second didn't rule it out.
        self.umbrella = firis(rect(-118.331, 36.969, -118.249, 37.031), "CINDER COMPLEX",
                              date=NEW + 1, acres=2784.0)

    def test_umbrella_over_drawn_subfires_is_dropped(self):
        out, fires = self.run_fires([self.sub_a, self.sub_b], [self.umbrella])
        self.assertEqual(out["fires"], 2)
        self.assertEqual((out["firis_covered"], out["firis_merged"], out["firis_fresher"]),
                         (1, 0, 0))

    def test_a_fresher_umbrella_is_not_swapped_into_one_subfire(self):
        _, fires = self.run_fires([self.sub_a, self.sub_b], [self.umbrella])
        self.assertEqual(fires["5-3"]["geometry"], rect(-118.33, 36.97, -118.27, 37.03)["geometry"])

    def test_firis_order_does_not_decide_which_fire_is_which(self):
        """FIRIS-only stage of a complex: no WFIGS sub-fires yet, and the layer
        lists CINDER COMPLEX first. Every order must give the same map."""
        def flights():
            return [firis(rect(-118.33, 36.97, -118.27, 37.03), "CINDERCOMPLEX-5-3", date=NEW),
                    firis(rect(-118.27, 36.97, -118.25, 37.03), "CINDERCOMPLEX-5-4", date=NEW),
                    firis(rect(-118.331, 36.969, -118.249, 37.031), "CINDER COMPLEX",
                          date=NEW + 1, acres=2784.0)]
        for order in itertools.permutations(range(3)):
            with self.subTest(order=order):
                f = flights()
                out, fires = self.run_fires([], [f[i] for i in order])
                self.assertEqual(sorted(fires), ["CINDERCOMPLEX-5-3", "CINDERCOMPLEX-5-4"])
                self.assertEqual((out["firis_added"], out["firis_covered"]), (2, 1))
                for name in fires:
                    self.assertEqual(fires[name]["properties"]["poly_GISAcres"], 120.0)

    def test_a_partly_new_perimeter_is_kept(self):
        half_new = firis(rect(-118.30, 36.97, -118.20, 37.03), "SPREAD")
        out, _ = self.run_fires([self.sub_a, self.sub_b], [half_new])
        self.assertEqual(out["firis_added"], 1)

    def test_a_newer_fire_inside_an_older_perimeter_is_its_own_fire(self):
        """A reburn or an unburned island: 90% covered, but by a perimeter
        older than its flight. Dropped, it went undrawn and its name on the
        old perimeter marked its point as mapped."""
        old = wfigs(rect(-121.0, 39.5, -120.8, 39.7), "BIGOLD", poly_DateCurrent=OLD)
        out, fires = self.run_fires([old], [firis(square(39.6, -120.9), "ISLAND", date=NEW)])
        self.assertEqual((out["fires"], out["firis_added"], out["firis_covered"]), (2, 1, 0))
        self.assertNotIn("_aka", fires["BIGOLD"]["properties"])

    def test_an_older_flight_inside_a_fresher_perimeter_is_dropped(self):
        big = wfigs(rect(-121.0, 39.5, -120.8, 39.7), "BIG")
        out, fires = self.run_fires([big], [firis(square(39.6, -120.9), "EARLIER", date=OLD)])
        self.assertEqual((out["fires"], out["firis_covered"]), (1, 1))
        self.assertEqual(fires["BIG"]["properties"]["_aka"], ["EARLIER"])

    def test_the_dropped_umbrella_leaves_its_name_on_what_draws_it(self):
        # Shares a sliver of the umbrella's edge; it is not part of the complex.
        neighbour = wfigs(rect(-118.252, 36.97, -118.20, 37.03), "OTHER")
        _, fires = self.run_fires([self.sub_a, self.sub_b, neighbour], [self.umbrella])
        self.assertEqual(fires["5-3"]["properties"]["_aka"], ["CINDER COMPLEX"])
        self.assertEqual(fires["5-4"]["properties"]["_aka"], ["CINDER COMPLEX"])
        self.assertNotIn("_aka", fires["OTHER"]["properties"])


@needs_shapely
class DrawOrder(FirisUnionTest):
    """index.html stacks perimeters in file order: the last one drawn is on top
    and takes the clicks. Matching runs smallest first, but that must not put
    an umbrella that stays over the fires inside it."""

    def test_added_perimeters_are_drawn_largest_first(self):
        def flights():
            # The two sub-fires cover only 70% of the umbrella, so it stays.
            return [firis(rect(-118.40, 36.95, -118.20, 37.05), "CINDER COMPLEX", date=NEW),
                    firis(rect(-118.40, 36.95, -118.30, 37.05), "CINDERCOMPLEX-5-3"),
                    firis(rect(-118.30, 36.95, -118.26, 37.05), "CINDERCOMPLEX-5-4")]
        for order in itertools.permutations(range(3)):
            with self.subTest(order=order):
                f = flights()
                out, _ = self.run_fires([wfigs(square(38.1, -120.77), "GANN")],
                                        [f[i] for i in order])
                drawn = [g["properties"]["attr_IncidentName"]
                         for g in self.read_data("wildfires.geojson")["features"]]
                self.assertEqual(out["firis_added"], 3)
                self.assertEqual(drawn, ["GANN", "CINDER COMPLEX", "CINDERCOMPLEX-5-3",
                                         "CINDERCOMPLEX-5-4"])


@needs_shapely
class FoldedNames(FirisUnionTest):
    """A FIRIS perimeter merged or dropped takes its name out of the file, and
    incident points find their perimeter by name. CAL FIRE's "Cinder Complex"
    point lost its perimeter and its link when the umbrella was dropped over
    its sub-fires — and a complex's own point can only come from CAL FIRE,
    since WFIGS's points are filtered to single wildfires."""

    def run_points(self, calfire_point):
        self.write_data("calfire.geojson", collection(calfire_point))
        with mock.patch.object(fd, "_fire_points_from_wfigs", return_value=collection()):
            out = fd.fetch_fire_points()
        pts = {f["properties"]["IncidentName"]: f["properties"]
               for f in self.read_data("fire_points.geojson")["features"]}
        perims = {f["properties"]["attr_IncidentName"]: f["properties"]
                  for f in self.read_data("wildfires.geojson")["features"]}
        return out, pts, perims

    @staticmethod
    def calfire(lon, lat, name, county):
        return point(lon, lat, Name=name, County=county, IsActive=True, AcresBurned=2784,
                     PercentContained=20, Url=f"https://www.fire.ca.gov/incidents/{name}")

    def test_a_calfire_complex_point_survives_its_umbrella_being_dropped(self):
        """The FIRIS-only stage of Cinder Complex: no WFIGS perimeter or point
        for either sub-fire yet."""
        out, _ = self.run_fires([], [
            firis(rect(-118.33, 36.97, -118.27, 37.03), "CINDERCOMPLEX-5-3"),
            firis(rect(-118.27, 36.97, -118.25, 37.03), "CINDERCOMPLEX-5-4"),
            firis(rect(-118.331, 36.969, -118.249, 37.031), "CINDER COMPLEX", date=NEW)])
        self.assertEqual(out["firis_covered"], 1)
        res, pts, perims = self.run_points(self.calfire(-118.29, 37.0, "Cinder Complex", "Inyo"))
        self.assertTrue(pts["Cinder Complex"]["_has_perimeter"])
        self.assertEqual(res["without_perimeter"], 0)
        for name in ("CINDERCOMPLEX-5-3", "CINDERCOMPLEX-5-4"):
            self.assertEqual(perims[name]["_calfire_url"],
                             "https://www.fire.ca.gov/incidents/Cinder Complex")
            self.assertEqual(perims[name]["attr_POOCounty"], "Inyo")
        self.assertEqual((res["perimeters_enriched"], res["perimeters_via_alias"]), (2, 2))

    def test_a_calfire_point_follows_a_merged_name(self):
        out, _ = self.run_fires([wfigs(square(40.5, -120.5), "3-6 WAGON", irwin="{W}")],
                                [firis(square(40.5, -120.497, half=0.021), "WAGON")])
        self.assertEqual(out["firis_merged"], 1)
        _, pts, perims = self.run_points(self.calfire(-120.49, 40.51, "Wagon Fire", "Lassen"))
        self.assertTrue(pts["Wagon Fire"]["_has_perimeter"])
        self.assertEqual(perims["3-6 WAGON"]["_calfire_url"],
                         "https://www.fire.ca.gov/incidents/Wagon Fire")

    def test_a_folded_name_far_away_is_another_fire(self):
        self.run_fires([wfigs(square(40.5, -120.5), "3-6 WAGON", irwin="{W}")],
                       [firis(square(40.5, -120.497, half=0.021), "WAGON")])
        _, pts, perims = self.run_points(self.calfire(-117.07, 32.66, "Wagon Fire", "San Diego"))
        self.assertFalse(pts["Wagon Fire"]["_has_perimeter"])
        self.assertNotIn("_calfire_url", perims["3-6 WAGON"])


@needs_shapely
class BadGeometry(FirisUnionTest):
    def test_invalid_polygons_cannot_fail_the_layer(self):
        ring = [[-121, 39], [-120.9, 39.1], [-120.9, 39], [-121, 39.1], [-121, 39]]
        bowtie = firis(polygon(ring), "BOWTIE")  # self-intersecting
        broken = firis({"type": "Feature", "properties": {},
                        "geometry": {"type": "Polygon", "coordinates": [[[-121, 39]]]}}, "BROKEN")
        out, fires = self.run_fires([wfigs(square(39.05, -120.95), "BASE")], [bowtie, broken])
        self.assertIn("BASE", fires)
        self.assertEqual(out["fires"], 3)
        self.assertEqual(out["firis_added"], 2)

    def test_an_unusable_polygon_matches_by_name_alone(self):
        """No shape to confirm an alias with, so none is trusted."""
        broken = firis({"type": "Feature", "properties": {},
                        "geometry": {"type": "Polygon", "coordinates": [[[-121, 39]]]}},
                       "CINDERCOMPLEX-5-3", date=NEW + 1)
        out, fires = self.run_fires([wfigs(square(39.0, -121.0), "5-3")], [broken])
        self.assertEqual((out["fires"], out["firis_merged"]), (2, 0))
        self.assertEqual(fires["5-3"]["geometry"], square(39.0, -121.0)["geometry"])


class WithoutShapely(FirisUnionTest):
    """Geometry is an improvement, not a dependency: without shapely the union
    behaves exactly as it did. Not even the alias rule — with no shape to
    check, the cross-wired names of Aug 4 would pair the wrong fires."""

    def test_falls_back_to_exact_names(self):
        with mock.patch.dict(sys.modules, {"shapely": None}):
            out, fires = self.run_fires(
                [wfigs(square(40.5, -120.5), "3-6 WAGON"), wfigs(square(37.0, -118.3), "5-3"),
                 wfigs(square(38.1, -120.77), "GANN")],
                [firis(square(40.5, -120.5), "WAGON"),
                 firis(square(37.0, -118.3), "CINDERCOMPLEX-5-3"),
                 firis(square(38.1, -120.77), "GANN", date=NEW + 1)])
        self.assertEqual(out["fires"], 5,
                         "identical shapes under different names: drawn twice, as before")
        self.assertEqual((out["firis_added"], out["firis_merged"], out["firis_fresher"]),
                         (2, 0, 1))


if __name__ == "__main__":
    unittest.main()
