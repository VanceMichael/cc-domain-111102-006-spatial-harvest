"""端到端演示：用人工构造坐标展示全部终止条件与精度权衡。

从项目根目录运行：``python3 -I -m app.demo``（成果写入 runtime/，与素材分离）。
每个场景独立目录，可重复运行验证断点续采与证据复用。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.client import DirectClient
from app.geo import parse_target
from app.harvester import HarvestConfig, HarvestConfigError, Harvester

RUNTIME = Path("runtime")


def _harvest(name, fixture, bounds, min_width, budget, cap=3, state_dir=None,
             reuse_from=None):
    state_dir = state_dir or (RUNTIME / name)
    client = DirectClient(path=fixture, cap=cap)
    config = HarvestConfig(
        target=bounds,
        min_width=min_width,
        request_budget=budget,
        cap=cap,
        endpoint=fixture,
    )
    harvester = Harvester(config, client, state_dir)
    if reuse_from is not None:
        harvester.import_evidence_from(reuse_from)
    manifest = harvester.run()
    return harvester, manifest


def _line(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _summary(manifest):
    s = manifest["summary"]
    print(
        f"  状态={manifest['overall_status']} 实际请求={s['requests_made']} "
        f"预算={manifest['config']['request_budget']} 证据框={manifest['evidence_cells']}"
    )
    print(
        f"  叶子: 完整={s['leaf_complete']} 未完成={s['leaf_incomplete']} "
        f"未决={s['leaf_pending']} | 去重点位={s['unique_point_ids']} "
        f"冲突={s['conflict_ids']} 隔离={s['quarantined_points']}"
    )
    print(
        f"  目标面积={s['target_area_km2']:.2f} km² 已证实={s['verified_area_km2']:.2f} "
        f"未证实={s['unverified_area_km2']:.2f} km²"
    )


def main():
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)

    # 场景 1：普通 3x3 网格点，展示闭区间细分、分割线重复与按 id 去重 ----------
    _line("场景 1  普通区域：分割线上的点在相邻子框重复，按稳定 id 去重并保留来源")
    _, m = _harvest("s1_ordinary", "fixtures/points.json",
                    [100, 35, 102, 37], min_width=0.5, budget=200)
    _summary(m)
    dup = [
        f for f in json.loads((RUNTIME / "s1_ordinary/points.geojson").read_text())["features"]
        if f["properties"]["source_cell_count"] > 1
    ]
    print(f"  被多个查询框返回（分割线重复）的点: {[f['properties']['id'] for f in dup]}")
    print(f"  输出点位数={m['summary']['unique_point_ids']}，接口原始累计返回可更多，无重复输出")

    # 场景 2：跨越 180 度经线 ---------------------------------------------------
    _line("场景 2  跨 180 度经线：展开为连续坐标，接缝 ±180 点不重复不冲突")
    _, m = _harvest("s2_crossing", "fixtures/points.json",
                    [179, -1, -179, 1], min_width=0.5, budget=200)
    _summary(m)
    frame, _, crossing = parse_target(179, -1, -179, 1)
    print(f"  原始输入 [179,-1,-179,1] 展开为 {frame.as_list()}，crossing={crossing}")
    seam = json.loads((RUNTIME / "s2_crossing/points.geojson").read_text())["features"]
    print(f"  接缝附近输出点位: {[(f['properties']['id'], f['geometry']['coordinates']) for f in seam]}")

    # 场景 3：同坐标密集点 —— 细分无法拆开，精度决定未证实区域与请求量 ----------
    _line("场景 3  同坐标密集点：任何细分都无法拆开，到最小格宽即停，区域不被判为空地")
    rows = []
    for tag, min_width in (("coarse", 1.0), ("fine", 0.1), ("finer", 0.02)):
        _, m = _harvest(f"s3_dense_{tag}", "fixtures/dense.json",
                        [100, 35, 102, 37], min_width=min_width, budget=2000)
        s = m["summary"]
        rows.append((min_width, s["requests_made"], s["leaf_incomplete"],
                     s["unverified_area_km2"]))
        print(f"  min_width={min_width:>5} 度 -> 请求={s['requests_made']:>4} "
              f"未完成叶子={s['leaf_incomplete']} 未证实面积={s['unverified_area_km2']:.3f} km²")
    print("  结论：min_width 每缩小一半，最坏情况下每层请求约 x4；")
    print("  未证实区域随最小格宽等比缩小，但密集同坐标点永远无法被证实完整，只能给出精确未证实范围。")

    # 场景 4：预算耗尽 —— 未决格可续采，续采不重复消耗已缓存请求 ----------------
    _line("场景 4  查询预算耗尽：未决格原样落盘，续采只补未完成部分")
    h, m = _harvest("s4_budget", "fixtures/points.json",
                    [100, 35, 102, 37], min_width=0.02, budget=5)
    _summary(m)
    first_requests = m["summary"]["requests_made"]
    _, m2 = _harvest("s4_budget", "fixtures/points.json",
                     [100, 35, 102, 37], min_width=0.02, budget=2000)
    _summary(m2)
    print(f"  首次请求 {first_requests} 次后中断；续采新增请求 "
          f"{m2['summary']['requests_made'] - first_requests} 次（缓存命中不计数），"
          f"最终状态={m2['overall_status']}")

    # 场景 5：同 id 矛盾坐标 ----------------------------------------------------
    _line("场景 5  同 id 矛盾坐标：点位保留一份，冲突变体单独列出")
    _, m = _harvest("s5_conflict", "fixtures/conflict.json",
                    [100, 35, 101, 36], min_width=0.1, budget=200)
    _summary(m)
    for c in m["conflicts"]:
        print(f"  冲突 id={c['id']} 保留坐标={c['canonical_kept']} 变体={c['variants']}")

    # 场景 6：参数变更 —— 指纹拦截 + 显式复用证据 + outside_target 说明 ---------
    _line("场景 6  修改边界/参数：旧完成标记被指纹拦截；显式复用时范围外证据单列")
    try:
        _harvest("s6_wrong", "fixtures/points.json",
                 [100, 35, 101.5, 37], min_width=0.1, budget=200,
                 state_dir=RUNTIME / "s1_ordinary")
    except HarvestConfigError as exc:
        print(f"  已拦截：{exc}")
    _, m = _harvest("s6_reuse", "fixtures/points.json",
                    [100, 35, 101.5, 37], min_width=0.5, budget=200,
                    reuse_from=RUNTIME / "s1_ordinary")
    _summary(m)
    print(f"  范围外证据条目: {m['outside_target_evidence']}")
    print(f"  清单中 reason 字典解释每个叶子格的完整/未完成/范围外原因，见 manifest.json")

    print("\n全部成果位于 runtime/*/{points,leaves,unverified}.geojson、"
          "conflicts.json、manifest.json、evidence.json、state.json")


if __name__ == "__main__":
    main()
