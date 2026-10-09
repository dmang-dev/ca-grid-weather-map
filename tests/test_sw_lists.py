"""sw.js precaches exactly what index.html loads.

The service worker's CDN_ASSETS and DATA_ASSETS are hand-copied from
index.html's unpkg <link>/<script> URLs and its fetch('data/...') calls,
and are precached at install so an offline launch works after a single
visit. A library version bumped in index.html but not in sw.js is only
cached on first use (one visit later), and a data file missing from
DATA_ASSETS is absent offline until the page has loaded it through the
worker. Nothing else notices the drift, so check the lists match.
"""

import re
import unittest

from helpers import REPO


def _list(js, name):
    m = re.search(rf"const {name} = \[(.*?)\];", js, re.S)
    if not m:
        raise AssertionError(f"sw.js has no {name} list")
    return m.group(1)


class ServiceWorkerLists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (REPO / "index.html").read_text(encoding="utf-8")
        cls.sw = (REPO / "sw.js").read_text(encoding="utf-8")

    def test_cdn_assets_match_index_html(self):
        url = r"https://unpkg\.com/[^\"'\s]+"
        page = set(re.findall(url, self.html))
        self.assertTrue(page, "no unpkg URLs found in index.html")
        self.assertEqual(set(re.findall(url, _list(self.sw, "CDN_ASSETS"))), page)

    def test_data_assets_match_index_html(self):
        page = set(re.findall(r"fetch\('data/([^']+)'", self.html))
        self.assertIn("manifest.json", page)
        precached = set(re.findall(r"'\./data/([^']+)'", _list(self.sw, "DATA_ASSETS")))
        self.assertEqual(precached, page)


if __name__ == "__main__":
    unittest.main()
