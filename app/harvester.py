"""自适应空间采集引擎。

职责
----
1. 从目标矩形开始，遇到 ``truncated=True`` 就闭区间四等分继续查询，
   ``count`` 不参与任何覆盖判断（接口不承诺其为真实总量）。
2. 同一稳定 id 只输出一次，但完整保留该点出现过的所有查询框（来源证据）。
3. 同 id 坐标相互矛盾时单独列入冲突清单，不静默覆盖。
4. 到达最小格宽或查询预算仍被截断时，输出"尚未证实覆盖"的精确区域，
   绝不把它计为空地；设置最大深度作为兜底，防止无限分裂。
5. 每次查询后追加证据并原子落盘状态，中断后可原样续采；
   边界或细分参数变化时，配置指纹不匹配会拒绝套用旧的完成标记，
   显式 ``reuse_evidence`` 时只复用原始响应、重建整棵树。

状态目录结构（与 fixtures 分离，runtime/ 已在 .gitignore 中）::

    state_dir/
      config.fingerprint   本次配置指纹
      evidence.json        每个查询框的原始响应证据（追加式）
      state.json           树节点、点位、冲突、隔离区与预算进度
      points.geojson       去重后点位（含来源框）
      conflicts.json       同 id 矛盾坐标
      manifest.json        覆盖清单（每个叶子格的完整/未完成/范围外原因）
      leaves.geojson       叶子格覆盖图
      unverified.geojson   尚未证实覆盖的精确区域
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from app import geo

# 兜底最大深度，防止浮点异常导致无限分裂（24 层约为厘米级）
MAX_DEPTH = 24


class HarvestConfigError(RuntimeError):
    """配置与已有运行状态不匹配等不可自动恢复的错误。"""


@dataclass(frozen=True)
class HarvestConfig:
    """一次采集任务的全部参数；指纹覆盖任何会改变细分结果的字段。"""

    target: list[float]          # 原始输入 [west, south, east, north]
    min_width: float             # 最小格宽（度），到限仍截断则停止并登记未证实区域
    request_budget: int          # 实际接口请求数上限（缓存命中不计数）
    cap: int                     # 接口单次最多返回条数
    endpoint: str                # 来源标识（fixture 路径或 base URL）

    def fingerprint(self) -> str:
        # 注意：request_budget 不参与指纹。预算只是运营上限，不改变细分几何
        # 与完成标记的语义；加大预算续采必须能复用已完成叶子和原始证据。
        payload = json.dumps(
            {
                "target": self.target,
                "min_width": self.min_width,
                "cap": self.cap,
                "endpoint": self.endpoint,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Harvester:
    def __init__(self, config: HarvestConfig, client, state_dir: str | Path):
        self.config = config
        self.client = client
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.frame, self.raw, self.crossing = geo.parse_target(*self.config.target)
        if self.config.min_width <= 0:
            raise ValueError("min_width 必须为正数")
        if self.config.request_budget < 1:
            raise ValueError("request_budget 至少为 1")

        self.fingerprint = config.fingerprint()
        self.evidence: dict[str, dict] = {}     # cell_key -> 合并后的证据记录
        self.nodes: dict[str, dict] = {}        # cell_key -> 节点状态
        self.points: dict[str, dict] = {}       # id -> 去重点位
        self.conflicts: dict[str, list] = {}    # id -> 不同坐标变体
        self.quarantine: list[dict] = []        # 越界响应点等异常证据
        self.imported_outside: list[dict] = []  # 复用证据但不在新目标范围的框
        self.requests_made = 0
        self.budget_exhausted = False

        self._fp_path = self.state_dir / "config.fingerprint"
        self._evidence_path = self.state_dir / "evidence.json"
        self._state_path = self.state_dir / "state.json"
        self._load_or_init()

    # ------------------------------------------------------------------ 持久化
    def _load_or_init(self) -> None:
        if self._fp_path.exists():
            old_fp = self._fp_path.read_text(encoding="utf-8").strip()
            if old_fp != self.fingerprint:
                raise HarvestConfigError(
                    "状态目录中的配置指纹与当前参数不一致，拒绝套用旧的完成标记；"
                    "请使用新的 state_dir，或显式 reuse_evidence 只复用原始响应。"
                )
            self._load_state()
            return

        if self._state_path.exists():
            # 没有指纹文件的遗留状态，同样不能信任其完成标记
            raise HarvestConfigError(
                "状态目录缺少配置指纹且已有 state.json，无法确认其适用范围，"
                "请使用新的 state_dir 重新开始。"
            )
        self._write_fingerprint()

    def _load_state(self) -> None:
        state = json.loads(self._state_path.read_text(encoding="utf-8"))
        if state.get("fingerprint") != self.fingerprint:
            raise HarvestConfigError("state.json 指纹校验失败")
        self.evidence = json.loads(self._evidence_path.read_text(encoding="utf-8"))
        self.nodes = state["nodes"]
        self.points = state["points"]
        self.conflicts = state.get("conflicts", {})
        self.quarantine = state.get("quarantine", [])
        self.imported_outside = state.get("imported_outside", [])
        self.requests_made = state.get("requests_made", 0)
        self.budget_exhausted = state.get("budget_exhausted", False)

    def _write_fingerprint(self) -> None:
        tmp = self._fp_path.with_suffix(".tmp")
        tmp.write_text(self.fingerprint, encoding="utf-8")
        os.replace(tmp, self._fp_path)

    def _persist_evidence(self) -> None:
        tmp = self._evidence_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._evidence_path)

    def _persist_state(self) -> None:
        state = {
            "fingerprint": self.fingerprint,
            "config": {
                "target": self.config.target,
                "min_width": self.config.min_width,
                "request_budget": self.config.request_budget,
                "cap": self.config.cap,
                "endpoint": self.config.endpoint,
                "crossing_antimeridian": self.crossing,
            },
            "frame": self.frame.as_list(),
            "root": geo.cell_key(self.frame),
            "nodes": self.nodes,
            "points": self.points,
            "conflicts": self.conflicts,
            "quarantine": self.quarantine,
            "imported_outside": self.imported_outside,
            "requests_made": self.requests_made,
            "budget_exhausted": self.budget_exhausted,
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._state_path)

    def import_evidence_from(self, other_state_dir: str | Path) -> dict:
        """从另一参数的运行目录复用原始响应证据（完成标记不迁移）。

        只有完整落在当前目标框内的查询框可复用；其余原样登记为
        ``outside_target`` 证据，供覆盖清单解释，不会被当作当前任务的叶子。
        """
        other = Path(other_state_dir)
        data = json.loads((other / "evidence.json").read_text(encoding="utf-8"))
        reused, rejected = 0, 0
        for key, record in data.items():
            if key in self.evidence:
                continue
            b = Bounds_from_key(key)
            if geo.bounds_within(self.frame, b):
                self.evidence[key] = record
                reused += 1
            else:
                self.imported_outside.append(
                    {"cell": b.as_list(), "reason": "outside_target"}
                )
                rejected += 1
        self._persist_evidence()
        self._persist_state()
        return {"reused": reused, "rejected": rejected}

    # ------------------------------------------------------------------ 查询
    def _fetch(self, b: geo.Bounds) -> dict:
        """查询一个展开坐标格框，跨经线时拆成两个接口请求并合并。

        每个不跨经线请求框各消耗一次预算；任一底层响应 truncated 则整体截断。
        证据按展开格框缓存，续采时命中缓存不产生请求、不消耗预算。
        """
        key = geo.cell_key(b)
        if key in self.evidence:
            cached = self.evidence[key]
            return {
                "points": cached["points"],
                "count": cached.get("count", len(cached["points"])),
                "truncated": cached["truncated"],
                "from_cache": True,
            }

        parts = geo.wire_parts(b)
        if self.requests_made + len(parts) > self.config.request_budget:
            self.budget_exhausted = True
            raise _BudgetExhausted

        merged: list[dict] = []
        truncated = False
        wire_calls: list[list[float]] = []
        for part in parts:
            response = self.client.query(part.west, part.south, part.east, part.north)
            self.requests_made += 1
            wire_calls.append(part.as_list())
            truncated = truncated or bool(response.get("truncated"))
            # count 仅原样存档，覆盖判断只看 truncated
            merged.extend(response.get("points", []))

        record = {
            "cell": b.as_list(),
            "wire_calls": wire_calls,
            "points": merged,
            "count": len(merged),
            "truncated": truncated,
        }
        self.evidence[key] = record
        self._persist_evidence()
        return {
            "points": merged,
            "count": len(merged),
            "truncated": truncated,
            "from_cache": False,
        }

    def _ingest(self, b: geo.Bounds, response: dict) -> None:
        """把响应点规范进展开坐标系，按 id 去重并保留来源框、登记冲突。"""
        key = geo.cell_key(b)
        for raw_point in response["points"]:
            pid = str(raw_point["id"])
            lon = float(raw_point["lon"])
            lat = float(raw_point["lat"])
            x = geo.normalize_lon(lon, self.frame)
            if x is None or not geo.contains(b, x, lat):
                # 接口返回了请求框之外的点：不丢弃证据，隔离另列
                self.quarantine.append(
                    {
                        "id": pid,
                        "lon": lon,
                        "lat": lat,
                        "observed_in": key,
                        "reason": "outside_requested_cell",
                    }
                )
                continue

            coord = [x, lat]
            existing = self.points.get(pid)
            if existing is None:
                self.points[pid] = {
                    "id": pid,
                    "lon": x,
                    "lat": lat,
                    "source_cells": [key],
                }
                continue

            if key not in existing["source_cells"]:
                existing["source_cells"].append(key)
            if not geo.same_point((existing["lon"], existing["lat"]), (x, lat)):
                variants = self.conflicts.setdefault(
                    pid,
                    [
                        {
                            "lon": existing["lon"],
                            "lat": existing["lat"],
                            "source_cells": list(existing["source_cells"]),
                        }
                    ],
                )
                if not any(
                    geo.same_point((v["lon"], v["lat"]), (x, lat)) for v in variants
                ):
                    variants.append(
                        {"lon": x, "lat": lat, "source_cells": [key]}
                    )

    # ------------------------------------------------------------------ 运行
    def run(self) -> dict:
        """执行采集直至完成或预算耗尽，返回摘要并写出全部成果文件。"""
        root_key = geo.cell_key(self.frame)
        if root_key not in self.nodes:
            self.nodes[root_key] = {"depth": 0, "status": "pending", "cell": self.frame.as_list()}
            self._persist_state()

        # DFS 栈：中断重启时根节点往往已是 split，重新装入全部 pending 节点，
        # 已完成的叶子与已缓存证据原样复用，不重复请求
        stack = sorted(
            (k for k, n in self.nodes.items() if n["status"] == "pending"),
            reverse=True,
        )
        # 每轮运行开始先复位历史耗尽标志；本轮若真的再次超预算，
        # _fetch 会重新置位（预算不参与配置指纹，可逐轮调整）
        self.budget_exhausted = False
        while stack:
            key = stack.pop()
            node = self.nodes[key]
            if node["status"] != "pending":
                continue
            b = geo.Bounds(*node["cell"])
            try:
                response = self._fetch(b)
            except _BudgetExhausted:
                # 剩余 pending 全部原样保留为可续采的未决格
                self.budget_exhausted = True
                self._persist_state()
                break

            self._ingest(b, response)

            if not response["truncated"]:
                node["status"] = "complete"
                node["reason"] = "not_truncated"
                node["point_ids"] = [str(p["id"]) for p in response["points"]]
            else:
                stop = self._stop_reason(node["depth"], b)
                if stop is not None:
                    node["status"] = "incomplete"
                    node["reason"] = stop
                    node["point_ids"] = [str(p["id"]) for p in response["points"]]
                else:
                    node["status"] = "split"
                    children = geo.split_quad(b)
                    node["children"] = [geo.cell_key(c) for c in children]
                    for child in children:
                        ck = geo.cell_key(child)
                        if ck not in self.nodes:
                            self.nodes[ck] = {
                                "depth": node["depth"] + 1,
                                "status": "pending",
                                "cell": child.as_list(),
                            }
                    stack.extend(reversed(node["children"]))
            self._persist_state()

        return self.build_manifest()

    def _stop_reason(self, depth: int, b: geo.Bounds) -> str | None:
        # 最小格宽优先：这是任务定义的精度终止条件
        if b.width <= self.config.min_width + geo.COORD_EPS:
            return "min_width_reached_truncated"
        if b.height <= self.config.min_width + geo.COORD_EPS:
            return "min_height_reached_truncated"
        if depth >= MAX_DEPTH:
            return "max_depth_reached_truncated"
        return None

    # ------------------------------------------------------------------ 成果
    def _all_leaves(self) -> list[dict]:
        """叶子 = 没有子节点的节点（complete / incomplete / 仍 pending）。"""
        leaves = []
        for key, node in self.nodes.items():
            if "children" not in node:
                leaves.append({"key": key, **node})
        leaves.sort(key=lambda n: (n["depth"], n["key"]))
        return leaves

    def build_manifest(self) -> dict:
        leaves = self._all_leaves()
        complete, incomplete, pending = [], [], []
        unverified_area = 0.0
        for leaf in leaves:
            b = geo.Bounds(*leaf["cell"])
            entry = {
                "cell": b.as_list(),
                "depth": leaf["depth"],
                "status": leaf["status"],
                "reason": leaf.get("reason", "pending_resumable"),
                "area_km2": geo.area_km2(b),
            }
            if leaf["status"] == "complete":
                complete.append(entry)
            elif leaf["status"] == "incomplete":
                unverified_area += entry["area_km2"]
                incomplete.append(entry)
            else:
                unverified_area += entry["area_km2"]
                pending.append(entry)

        total_area = geo.area_km2(self.frame)
        leaf_area = sum(geo.area_km2(geo.Bounds(*l["cell"])) for l in leaves)
        # 闭区间细分在测度上保持面积守恒（边界面积为零），偏差只应来自浮点
        assert abs(leaf_area - total_area) < 1e-6 * max(total_area, 1e-12)

        overall = (
            "complete"
            if not incomplete and not pending
            else "incomplete_budget" if self.budget_exhausted else "incomplete"
        )

        manifest = {
            "overall_status": overall,
            "config": {
                "target_raw": self.raw,
                "target_frame_unwrapped": self.frame.as_list(),
                "crossing_antimeridian": self.crossing,
                "min_width_degrees": self.config.min_width,
                "request_budget": self.config.request_budget,
                "cap": self.config.cap,
                "endpoint": self.config.endpoint,
                "fingerprint": self.fingerprint,
            },
            "summary": {
                "unique_point_ids": len(self.points),
                "conflict_ids": len(self.conflicts),
                "quarantined_points": len(self.quarantine),
                "leaf_complete": len(complete),
                "leaf_incomplete": len(incomplete),
                "leaf_pending": len(pending),
                "requests_made": self.requests_made,
                "budget_exhausted": self.budget_exhausted,
                "target_area_km2": total_area,
                "verified_area_km2": sum(e["area_km2"] for e in complete),
                "unverified_area_km2": unverified_area,
            },
            "leaves": {
                "complete": complete,
                "incomplete": incomplete,
                "pending": pending,
            },
            "outside_target_evidence": self.imported_outside,
            "unverified_regions": [e["cell"] for e in incomplete + pending],
            "conflicts": [
                {
                    "id": pid,
                    "variants": variants,
                    "canonical_kept": [
                        self.points[pid]["lon"],
                        self.points[pid]["lat"],
                    ],
                }
                for pid, variants in sorted(self.conflicts.items())
            ],
            "quarantine": self.quarantine,
            "evidence_cells": len(self.evidence),
            "reason_dictionary": {
                "not_truncated": "响应未截断，框内点位已完整证实（可能为空，空即经证实的无观测区域）",
                "min_width_reached_truncated": "已到最小格宽仍被截断，该精确区域尚未证实覆盖，不计为空地",
                "min_height_reached_truncated": "纬度方向已到最小尺寸仍被截断，同上",
                "max_depth_reached_truncated": "触及最大深度兜底，停止分裂，区域未证实",
                "pending_resumable": "预算或中断导致未查询，证据落盘后可原样续采",
                "outside_target": "复用证据中的查询框不落在当前目标范围内，不参与本次覆盖统计",
            },
        }

        self._write_outputs(manifest, complete, incomplete, pending)
        return manifest

    def _write_outputs(self, manifest, complete, incomplete, pending) -> None:
        # 点位文件：每个 id 一个要素，来源框完整保留
        point_features = []
        for pid, p in sorted(self.points.items()):
            point_features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [geo.wrap_lon(p["lon"]), p["lat"]],
                    },
                    "properties": {
                        "id": pid,
                        "in_conflict": pid in self.conflicts,
                        "source_cells": p["source_cells"],
                        "source_cell_count": len(p["source_cells"]),
                    },
                }
            )
        self._dump_geojson(
            self.state_dir / "points.geojson", point_features, "harvested_points"
        )

        def leaf_feature(entry):
            b = geo.Bounds(*entry["cell"])
            return {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": geo.cell_polygons(b)},
                "properties": {
                    "status": entry["status"],
                    "reason": entry["reason"],
                    "depth": entry["depth"],
                    "area_km2": entry["area_km2"],
                    "cell_unwrapped": entry["cell"],
                },
            }

        leaf_features = [leaf_feature(e) for e in complete + incomplete + pending]
        self._dump_geojson(
            self.state_dir / "leaves.geojson", leaf_features, "coverage_leaves"
        )

        unverified_features = [leaf_feature(e) for e in incomplete + pending]
        self._dump_geojson(
            self.state_dir / "unverified.geojson",
            unverified_features,
            "unverified_regions",
        )

        tmp = (self.state_dir / "manifest.json").with_suffix(".tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.state_dir / "manifest.json")

        tmp = (self.state_dir / "conflicts.json").with_suffix(".tmp")
        tmp.write_text(
            json.dumps(manifest["conflicts"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_dir / "conflicts.json")

    def _dump_geojson(self, path: Path, features: list[dict], name: str) -> None:
        payload = {
            "type": "FeatureCollection",
            "name": name,
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:EPSG::4326"},
            },
            "features": features,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


class _BudgetExhausted(Exception):
    """内部信号：剩余预算不足以完成本格框的全部底层请求。"""


def Bounds_from_key(key: str) -> geo.Bounds:
    return geo.Bounds(*json.loads(key))
