"""Cross-source fire identity: names, aliases, IRWIN ids, geometry centres.

Four upstreams name the same fire four ways. CAL FIRE writes "Gann Fire",
WFIGS writes "GANN", FIRIS writes a complex's sub-fires as
"CINDERCOMPLEX-5-3" where WFIGS calls that same fire "5-3", and FIRIS
re-uploads under "<NAME>-UPDATED". Each rule below fixed a real defect, and
each has a matching negative case that must keep working — the aliasing and
the dedup pull in opposite directions and it is easy to break one with the
other.
"""

import unittest

from helpers import FetchDataTest, fd


class FireKey(FetchDataTest):
    def test_trailing_noun_is_stripped(self):
        self.assertEqual(fd._fire_key("Gann Fire"), "GANN")
        self.assertEqual(fd._fire_key("GANN"), "GANN")
        self.assertEqual(fd._fire_key("Cinder Complex"), "CINDER")

    def test_case_and_punctuation_normalize(self):
        self.assertEqual(fd._fire_key("Biscar"), fd._fire_key("BISCAR"))
        self.assertEqual(fd._fire_key("5-3"), "53")

    def test_empty_input_is_safe(self):
        self.assertEqual(fd._fire_key(None), "")
        self.assertEqual(fd._fire_key(""), "")


class FireKeyAliases(FetchDataTest):
    def test_complex_subfire_also_answers_to_its_designation(self):
        self.assertEqual(fd._fire_keys("CINDERCOMPLEX-5-3"), ["CINDERCOMPLEX53", "53"])
        self.assertEqual(fd._fire_keys("CINDERCOMPLEX-5-4"), ["CINDERCOMPLEX54", "54"])

    def test_wfigs_side_of_that_join(self):
        self.assertEqual(fd._fire_keys("5-3"), ["53"])

    def test_plain_names_get_no_alias(self):
        self.assertEqual(fd._fire_keys("GANN"), ["GANN"])
        self.assertEqual(fd._fire_keys("Gann Fire"), ["GANN"])

    def test_hyphen_without_a_complex_prefix_gets_no_alias(self):
        """LAC-274775 must not answer to '274775' — those ids are dense enough
        to collide."""
        self.assertEqual(fd._fire_keys("LAC-274775"), ["LAC274775"])
        self.assertEqual(fd._fire_keys("RUMSEY-UPDATED"), ["RUMSEYUPDATED"])
        self.assertEqual(fd._fire_keys("3-1 PIT"), ["31 PIT"])

    def test_umbrella_complex_keeps_only_its_own_key(self):
        self.assertEqual(fd._fire_keys("CINDER COMPLEX"), ["CINDER"])

    def test_empty_input_is_safe(self):
        self.assertEqual(fd._fire_keys(None), [])
        self.assertEqual(fd._fire_keys(""), [])


class RevisionSuffix(FetchDataTest):
    """RUMSEY and RUMSEY-UPDATED sat 0.01 km apart at 396 and 380 acres — one
    fire drawn twice."""

    def sub(self, name):
        return fd._FIRIS_REVISION_SUFFIX.sub("", name.strip())

    def test_updated_suffix_is_folded_away(self):
        self.assertEqual(self.sub("RUMSEY-UPDATED"), "RUMSEY")
        self.assertEqual(self.sub("RUMSEY - UPDATED"), "RUMSEY")
        self.assertEqual(self.sub("rumsey-updated"), "rumsey")

    def test_a_trailing_number_is_a_real_subfire_not_a_revision(self):
        """The rule that must never generalise: folding these would merge two
        distinct fires of one complex."""
        self.assertEqual(self.sub("CINDERCOMPLEX-5-3"), "CINDERCOMPLEX-5-3")
        self.assertEqual(self.sub("CINDERCOMPLEX-5-4"), "CINDERCOMPLEX-5-4")
        self.assertEqual(self.sub("LAC-274775"), "LAC-274775")

    def test_unsuffixed_names_are_untouched(self):
        self.assertEqual(self.sub("RUMSEY"), "RUMSEY")
        self.assertEqual(self.sub("GANN"), "GANN")
        self.assertEqual(self.sub("UPDATED"), "UPDATED")


class IrwinIds(FetchDataTest):
    def test_braces_and_case_normalize(self):
        braced = "{A7C819CA-FCE9-4E9E-9A7F-C89F2160F102}"
        bare = "a7c819ca-fce9-4e9e-9a7f-c89f2160f102"
        self.assertEqual(fd._irwin(braced), fd._irwin(bare))
        self.assertEqual(fd._irwin(braced), bare)

    def test_empty_is_safe(self):
        self.assertEqual(fd._irwin(None), "")
        self.assertEqual(fd._irwin(""), "")


class GeometryCentre(FetchDataTest):
    def test_polygon_centre(self):
        geom = {"type": "Polygon",
                "coordinates": [[[-120, 38], [-121, 38], [-121, 39], [-120, 39]]]}
        self.assertEqual(fd._geom_center(geom), (38.5, -120.5))

    def test_point_centre(self):
        self.assertEqual(
            fd._geom_center({"type": "Point", "coordinates": [-120.5, 38.5]}),
            (38.5, -120.5),
        )

    def test_multipolygon_is_walked(self):
        geom = {"type": "MultiPolygon", "coordinates": [
            [[[-120, 38], [-120, 39], [-121, 39]]],
            [[[-122, 40], [-122, 41], [-123, 41]]],
        ]}
        lat, lon = fd._geom_center(geom)
        self.assertAlmostEqual(lat, 39.5)
        self.assertAlmostEqual(lon, -121.5)

    def test_missing_geometry_is_none(self):
        self.assertIsNone(fd._geom_center(None))
        self.assertIsNone(fd._geom_center({"type": "Polygon", "coordinates": []}))


class Distance(FetchDataTest):
    def test_identical_points_are_zero(self):
        self.assertAlmostEqual(fd._km_apart(38.0, -120.0, 38.0, -120.0), 0.0)

    def test_a_degree_of_latitude_is_about_111_km(self):
        self.assertAlmostEqual(fd._km_apart(38.0, -120.0, 39.0, -120.0), 111.0, places=1)

    def test_alias_confirmation_radius_behaves(self):
        """The 40 km alias guard: same fire close, different fire far."""
        self.assertLess(fd._km_apart(38.10, -120.77, 38.12, -120.75), 40)
        self.assertGreater(fd._km_apart(38.10, -120.77, 34.05, -118.24), 40)


class DistanceToExtent(FetchDataTest):
    """Name hits are confirmed against a perimeter's extent, not its centre:
    a big fire's origin can sit far from the middle of its own box."""

    # Roughly Dixie's footprint: it started at the south-west corner and ran
    # north-east for ~100 km.
    GEOM = {"type": "Polygon", "coordinates": [
        [[-121.45, 39.82], [-120.60, 40.05], [-120.55, 40.60], [-121.20, 40.62],
         [-121.50, 40.20], [-121.45, 39.82]]]}
    BOX = fd._geom_bbox(GEOM)

    def test_bbox(self):
        self.assertEqual(self.BOX, (39.82, -121.50, 40.62, -120.55))
        self.assertIsNone(fd._geom_bbox(None))

    def test_inside_is_zero_even_far_from_the_centre(self):
        origin = (39.871, -121.389)
        self.assertGreater(fd._km_apart(*origin, *fd._geom_center(self.GEOM)),
                           fd.ALIAS_CONFIRM_KM)
        self.assertEqual(fd._km_outside(*origin, self.BOX), 0.0)

    def test_outside_measures_to_the_nearest_edge(self):
        self.assertAlmostEqual(fd._km_outside(38.82, -121.0, self.BOX), 111.0, places=1)
        self.assertGreater(fd._km_outside(32.66, -117.07, self.BOX), 500)


class IsoToMillis(FetchDataTest):
    def test_calfire_timestamp_becomes_epoch_millis(self):
        self.assertEqual(fd._iso_to_ms("2026-08-04T00:27:56Z"), 1785803276000)

    def test_bad_input_is_none_not_an_exception(self):
        self.assertIsNone(fd._iso_to_ms(None))
        self.assertIsNone(fd._iso_to_ms(""))
        self.assertIsNone(fd._iso_to_ms("not-a-date"))


if __name__ == "__main__":
    unittest.main()
