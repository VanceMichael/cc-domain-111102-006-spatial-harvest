"""采集引擎与几何工具的回归测试。运行：python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import geo
from app.client import DirectClient, ReplayClient
from app.harvester import HarvestConfig, HarvestConfigError, Harvester
from app.wire import Replay


def config(target, min_width=0.1, budget=500, fixture="fixtures/points.json", cap=3):
    return HarvestConfig(
        target=target, min_width=min_width, request_budget=budget,
        cap=cap, endpoint=fixture,
    )


def run_once(tmpdir, target, min_width=0.1, budget=500,
             fixture="fixtures/points.json", cap=3, reuse_from=None):
    client = DirectClient(path=fixture, cap=cap)
    h = Harvester(config(target, min_width, budget, fixture, cap), client, tmpdir)
    if reuse_from is not None:
        h.import_evidence_from(reuse_from)
    return h, h.run()


class GeoTests(unittest.TestCase):
    def test_parse_crossing_antimeridian(self):
        frame, raw, crossing = geo.parse_target(179, -1, -179, 1)
        self.assertTrue(crossing)
        self.assertEqual(frame.as_list(), [179.0, -1.0, 181.0, 1.0])

    def test_seam_longitudes_normalize_to_same_point(self):
        frame, _, _ = geo.parse_target(179, -1, -179, 1)
        self.assertEqual(geo.normalize_lon(180, frame), 180.0)
        self.assertEqual(geo.normalize_lon(-180, frame), 180.0)
        self.assertIsNone(geo.normalize_lon(0, frame))

    def test_quad_split_closed_union(self):
        b = geo.Bounds(100, 35, 102, 37)
        sw, se, nw, ne = geo.split_quad(b)
        # 共享分割线：相邻子框闭区间重叠于分割线，且并集边界等于父框
        self.assertEqual(sw.east, se.west)
        self.assertEqual(sw.north, nw.south)
        self.assertEqual(sw.west, b.west)
        self.assertEqual(ne.east, b.east)
        self.assertEqual(ne.north, b.north)
        for child in (sw, se, nw, ne):
            self.assertTrue(geo.contains(b, child.west, child.south))
            self.assertTrue(geo.contains(b, child.east, child.north))

    def test_wire_parts_split_on_seam(self):
        b = geo.Bounds(179.5, -1, 180.5, 1)
        parts = geo.wire_parts(b)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].as_list(), [179.5, -1, 180.0, 1.0])
        self.assertEqual(parts[1].as_list(), [-180.0, -1, -179.5, 1.0])

    def test_polygons_and_area(self):
        b = geo.Bounds(179, -1, 181, 1)
        rings = geo.cell_polygons(b)
        self.assertEqual(len(rings), 2)
        self.assertGreater(geo.area_km2(b), 0.0)
        # 面积对同宽度的跨/不跨经线框应当一致
        b2 = geo.Bounds(100, -1, 102, 1)
        self.assertAlmostEqual(geo.area_km2(b), geo.area_km2(b2), places=6)

    def test_bad_bounds(self):
        with self.assertRaises(ValueError):
            geo.parse_target(200, 0, 10, 1)
        with self.assertRaises(ValueError):
            geo.parse_target(0, 2, 1, 1)


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ordinary_harvest_dedup_and_provenance(self):
        h, manifest = run_once(self.tmp, [100, 35, 102, 37], min_width=0.5, budget=200)
        self.assertEqual(manifest["overall_status"], "complete")
        # 11 个素材点（含 ±180 接缝点不在本框），框内 9 个网格点
        self.assertEqual(manifest["summary"]["unique_point_ids"], 9)
        gj = json.loads((self.tmp / "points.geojson").read_text())
        multi = [f for f in gj["features"] if f["properties"]["source_cell_count"] > 1]
        self.assertTrue(multi, "分割线上的点必然被多个查询框返回")
        ids = [f["properties"]["id"] for f in gj["features"]]
        self.assertEqual(len(ids), len(set(ids)), "稳定 id 不允许重复输出")
        # 面积守恒：叶子并集恰好覆盖目标框
        s = manifest["summary"]
        self.assertAlmostEqual(
            s["verified_area_km2"], s["target_area_km2"], places=6
        )
        self.assertEqual(s["unverified_area_km2"], 0.0)

    def test_crossing_antimeridian_harvest(self):
        _, manifest = run_once(self.tmp, [179, -1, -179, 1], min_width=0.5, budget=200)
        self.assertEqual(manifest["overall_status"], "complete")
        ids = manifest["summary"]["unique_point_ids"]
        # 该框只包含 seam-east / seam-west，两个经度在展开坐标上是同一点位但 id 不同
        self.assertEqual(ids, 2)
        self.assertEqual(manifest["summary"]["conflict_ids"], 0)
        leaves = json.loads((self.tmp / "leaves.geojson").read_text())
        # 跨经线叶子必须拆成两个 Polygon 环
        self.assertTrue(
            any(len(f["geometry"]["coordinates"]) == 2 for f in leaves["features"])
        )

    def test_dense_same_coordinate_stops_at_min_width(self):
        _, manifest = run_once(
            self.tmp, [100, 35, 102, 37], min_width=0.1, budget=2000,
            fixture="fixtures/dense.json",
        )
        s = manifest["summary"]
        self.assertGreater(s["leaf_incomplete"], 0)
        self.assertGreater(s["unverified_area_km2"], 0.0)
        reasons = {e["reason"] for e in manifest["leaves"]["incomplete"]}
        self.assertIn("min_width_reached_truncated", reasons)
        # 未证实区域不能被算作空地：它不进入 complete 列表
        self.assertNotIn(
            "min_width_reached_truncated",
            {e["reason"] for e in manifest["leaves"]["complete"]},
        )

    def test_precision_controls_request_count_and_unverified_area(self):
        coarse = run_once(self.tmp / "a", [100, 35, 102, 37], 1.0, 2000,
                          fixture="fixtures/dense.json")[1]
        fine = run_once(self.tmp / "b", [100, 35, 102, 37], 0.1, 2000,
                        fixture="fixtures/dense.json")[1]
        self.assertGreater(
            fine["summary"]["requests_made"], coarse["summary"]["requests_made"]
        )
        self.assertLess(
            fine["summary"]["unverified_area_km2"],
            coarse["summary"]["unverified_area_km2"],
        )

    def test_budget_exhaustion_then_resume(self):
        h1, m1 = run_once(self.tmp, [100, 35, 102, 37], 0.02, budget=5)
        self.assertTrue(m1["summary"]["budget_exhausted"])
        self.assertGreater(m1["summary"]["leaf_pending"], 0)
        first = m1["summary"]["requests_made"]
        # 同参数续采：证据命中不产生新请求
        h2, m2 = run_once(self.tmp, [100, 35, 102, 37], 0.02, budget=2000)
        self.assertEqual(m2["overall_status"], "complete")
        cached = m2["evidence_cells"]
        self.assertGreaterEqual(cached, first)
        # 续采实际新增请求 = 总请求 - 已缓存证据框数量级别（每框一次请求）
        self.assertLess(m2["summary"]["requests_made"] - first,
                        m2["summary"]["requests_made"])

    def test_config_fingerprint_blocks_stale_completion(self):
        run_once(self.tmp, [100, 35, 102, 37], 0.5, budget=200)
        with self.assertRaises(HarvestConfigError):
            run_once(self.tmp, [100, 35, 101.5, 37], 0.5, budget=200)
        with self.assertRaises(HarvestConfigError):
            run_once(self.tmp, [100, 35, 102, 37], 0.25, budget=200)

    def test_reuse_evidence_marks_outside_target(self):
        old = self.tmp / "old"
        run_once(old, [100, 35, 102, 37], 0.5, budget=200)
        new = self.tmp / "new"
        _, manifest = run_once(
            new, [100, 35, 101.5, 37], 0.5, budget=200, reuse_from=old
        )
        # 新目标是旧目标的真子集：必有旧查询框落在新目标之外并被登记
        self.assertTrue(manifest["outside_target_evidence"])
        self.assertEqual(
            {e["reason"] for e in manifest["outside_target_evidence"]},
            {"outside_target"},
        )
        # 新树仍是完整覆盖，面积只统计新目标
        self.assertEqual(manifest["overall_status"], "complete")

    def test_conflicting_coordinates_listed_separately(self):
        _, manifest = run_once(
            self.tmp, [100, 35, 101, 36], 0.1, 200, fixture="fixtures/conflict.json"
        )
        conflict_ids = [c["id"] for c in manifest["conflicts"]]
        self.assertIn("c1", conflict_ids)
        c1 = next(c for c in manifest["conflicts"] if c["id"] == "c1")
        self.assertEqual(len(c1["variants"]), 2)
        # 点位文件中冲突 id 仍只出现一次
        gj = json.loads((self.tmp / "points.geojson").read_text())
        c1_features = [f for f in gj["features"] if f["properties"]["id"] == "c1"]
        self.assertEqual(len(c1_features), 1)
        self.assertTrue(c1_features[0]["properties"]["in_conflict"])
        conflicts_on_disk = json.loads((self.tmp / "conflicts.json").read_text())
        self.assertEqual(conflicts_on_disk, manifest["conflicts"])

    def test_evidence_and_state_files_persisted(self):
        run_once(self.tmp, [100, 35, 102, 37], 0.5, budget=200)
        for name in ("config.fingerprint", "evidence.json", "state.json",
                     "manifest.json", "points.geojson", "leaves.geojson",
                     "unverified.geojson", "conflicts.json"):
            self.assertTrue((self.tmp / name).exists(), name)
        evidence = json.loads((self.tmp / "evidence.json").read_text())
        for record in evidence.values():
            self.assertIn("wire_calls", record)
            self.assertIn("truncated", record)

    def test_replay_client_adapter(self):
        # 录制一个不跨经线请求框的响应，经 ReplayClient 采集
        import base64
        from urllib.parse import urlencode

        body = json.dumps({
            "points": [{"id": "r0", "lon": 100.5, "lat": 35.5}],
            "count": 1, "truncated": False,
        })
        url = "https://endpoint.invalid/query?" + urlencode(
            {"west": 100.0, "south": 35.0, "east": 102.0, "north": 37.0}
        )
        routes_path = self.tmp / "routes.json"
        routes_path.write_text(json.dumps({
            url: [{"status": 200, "headers": {}, "base64": True,
                   "body": base64.b64encode(body.encode()).decode()}]
        }))
        replay = Replay(routes_path)
        h = Harvester(
            config([100, 35, 102, 37], min_width=0.5, budget=10),
            ReplayClient(replay), self.tmp / "replay",
        )
        manifest = h.run()
        self.assertEqual(manifest["summary"]["unique_point_ids"], 1)


if __name__ == "__main__":
    unittest.main()
