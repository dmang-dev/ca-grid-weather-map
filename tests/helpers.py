"""Shared fixtures for the fetch_data suite.

The one rule here that matters: **tests never write into the project's
`data/`.** An earlier throwaway version of this suite wrote its mock stubs
straight into the real directory, and a 1-feature `wildfires.geojson` got
committed and served to the live site before anyone noticed. `FetchDataTest`
redirects `DATA_DIR` at a temp directory and puts the real one back afterwards.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import fetch_data as fd  # noqa: E402  (path setup must come first)

# ---------------------------------------------------------------------------
# Canned upstream responses
# ---------------------------------------------------------------------------

# ArcGIS reports rate limiting as HTTP 200 with the error in the body, which is
# the trap the retry logic exists for.
ARC_QUOTA = {
    "error": {
        "code": 429,
        "message": "Unable to perform query. Too many requests.",
        "details": ["API calls quota exceeded (63197 request units)! "
                    "maximum allowed request units (57600) per Minute."],
    }
}

# What CEC dropping a field looks like: a 400 that must NOT be retried.
ARC_BAD_FIELD = {
    "error": {
        "code": 400,
        "message": "Cannot perform query. Invalid query parameters.",
        "details": ["'outFields' parameter is invalid"],
    }
}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def point(lon, lat, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def polygon(ring, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": props,
    }


class FakeResponse:
    """Stands in for a requests.Response.

    Pass an Exception as the payload to make .json() raise, which is how a
    truncated or HTML error page behaves.
    """

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FetchDataTest(unittest.TestCase):
    """Base case: isolated DATA_DIR, no real sleeping, no real HTTP."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cagrid-tests-")
        self.data_dir = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._real_data_dir = fd.DATA_DIR
        fd.DATA_DIR = self.data_dir
        self.addCleanup(setattr, fd, "DATA_DIR", self._real_data_dir)

        # Retry backoff is asserted on, never waited on.
        self.slept: list[float] = []
        patcher = mock.patch("time.sleep", side_effect=self.slept.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Belt and braces against the mistake this suite was born from.
        self.assertIs(fd.DATA_DIR, self.data_dir,
                      "a test reassigned DATA_DIR without restoring it")

    def stub_http(self, *responses):
        """Serve `responses` in order to fd.HTTP.get; the last one repeats.

        Returns the list that records each call, so tests can assert on the
        URL, params and call count.
        """
        calls: list[dict] = []
        queue = list(responses)

        def _get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        patcher = mock.patch.object(fd.HTTP, "get", side_effect=_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def write_data(self, name, obj):
        """Put a file in the isolated data dir, as the seed step would."""
        (self.data_dir / name).write_text(json.dumps(obj))

    def read_data(self, name):
        return json.loads((self.data_dir / name).read_text())
