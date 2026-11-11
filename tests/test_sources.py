import unittest
import json
from app.wire import Replay

from app.spatial import GridEndpoint
class DomainTests(unittest.TestCase):
    def test_truncation(self):
        self.assertTrue(GridEndpoint().query(100,35,102,37)["truncated"])
    def test_boundary(self):
        self.assertEqual(GridEndpoint().query(180,0,180,0)["points"][0]["id"],"seam-east")
    def test_reversed(self):
        with self.assertRaises(ValueError): GridEndpoint().query(179,-1,-179,1)


class WireTests(unittest.TestCase):
    def test_unknown_route(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"routes.json";p.write_text("{}")
            self.assertEqual(Replay(p).get("https://missing.invalid").status,404)
