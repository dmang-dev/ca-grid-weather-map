"""The deploy's seed step (.github/scripts/seed_data.py).

Its whole job is to be harmless. It must never fail a build, never keep a
half-downloaded file, and never turn a cold start into an error — while still
handing fetch_data.py a previous snapshot so the stale carry-forward works.
"""

import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".github" / "scripts"))

import seed_data  # noqa: E402


class SeedDataTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cagrid-seed-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name) / "data"
        patcher = mock.patch.object(seed_data, "DATA", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub_urlopen(self, responder):
        """responder(url) -> bytes, or raises."""
        class _Ctx:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return io.BytesIO(self.body)

            def __exit__(self, *a):
                return False

        def _open(url, timeout=None):
            return _Ctx(responder(url))

        patcher = mock.patch.object(seed_data.urllib.request, "urlopen", side_effect=_open)
        patcher.start()
        self.addCleanup(patcher.stop)


class HappyPath(SeedDataTest):
    def test_every_file_is_written_and_parses(self):
        self.stub_urlopen(lambda url: json.dumps({"type": "FeatureCollection",
                                                  "features": []}).encode())
        self.assertEqual(seed_data.main(), 0)
        written = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(written, sorted(seed_data.FILES))

    def test_manifest_is_included(self):
        """Without it _previous_manifest() returns {} and nothing can be
        marked stale even when its file is present."""
        self.assertIn("manifest.json", seed_data.FILES)

    def test_the_file_list_matches_what_fetch_data_writes(self):
        source = (REPO / "fetch_data.py").read_text(encoding="utf-8")
        import re
        written = set(re.findall(r'DATA_DIR / "([^"]+)"', source))
        missing = written - set(seed_data.FILES)
        self.assertEqual(missing, set(),
                         f"fetch_data.py writes files the seed step won't restore: {missing}")


class Robustness(SeedDataTest):
    def test_a_404_is_skipped_not_fatal(self):
        def responder(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        self.stub_urlopen(responder)
        self.assertEqual(seed_data.main(), 0, "a cold start must not fail the build")
        self.assertFalse(self.dir.exists(), "nothing should be created")

    def test_a_network_error_is_skipped_not_fatal(self):
        def responder(url):
            raise urllib.error.URLError("dns went away")

        self.stub_urlopen(responder)
        self.assertEqual(seed_data.main(), 0)

    def test_a_truncated_download_is_discarded(self):
        """Keeping half a file would be worse than keeping none: a later fetch
        failure would publish the broken copy as the last-good snapshot."""
        self.stub_urlopen(lambda url: b'{"type": "FeatureCollection", "featur')
        self.assertEqual(seed_data.main(), 0)
        self.assertFalse(self.dir.exists())

    def test_an_empty_body_is_discarded(self):
        self.stub_urlopen(lambda url: b"")
        self.assertEqual(seed_data.main(), 0)
        self.assertFalse(self.dir.exists())

    def test_a_partial_outage_keeps_the_files_that_did_arrive(self):
        def responder(url):
            if url.endswith("wind.json"):
                raise urllib.error.HTTPError(url, 500, "boom", None, None)
            return json.dumps({"ok": True}).encode()

        self.stub_urlopen(responder)
        self.assertEqual(seed_data.main(), 0)
        names = {p.name for p in self.dir.iterdir()}
        self.assertNotIn("wind.json", names)
        self.assertIn("manifest.json", names)


if __name__ == "__main__":
    unittest.main()
