"""fetch_data.py imports with nothing but numpy and requests behind it.

herbie, cfgrib and shapely are imported inside the fetchers that use them, so
a missing one costs only its own layer, and tests.yml can skip the ~5-minute
eccodes install. tests.yml does install shapely, for the geometry tests, so a
module-scope `import shapely` would pass there unnoticed: this blocks the lazy
dependencies by hand and imports fetch_data in a fresh interpreter.
"""

import subprocess
import sys
import unittest

from helpers import REPO

LAZY = ("herbie", "cfgrib", "eccodes", "xarray", "pyproj", "shapely")


class LazyImports(unittest.TestCase):
    def test_fetch_data_imports_without_its_lazy_dependencies(self):
        code = (f"import sys\n"
                f"for name in {LAZY!r}:\n"
                f"    sys.modules[name] = None  # any import of it now raises\n"
                f"import fetch_data\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
