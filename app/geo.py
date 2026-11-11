"""EPSG:4326 矩形几何：跨 180 度经线展开、闭区间四叉细分与面积计算。

约定
----
* 用户输入的边界为 ``[west, south, east, north]``，端点包含（闭区间）。
* ``east < west`` 表示跨越 180 度经线，内部把经度展开为连续数轴，
  例如 ``[179, -1, -179, 1]`` 展开为 ``west=179, east=181`` 的普通矩形。
* 展开后 ``east`` 可能大于 180，所有细分、缓存键、面积计算都在展开坐标上进行，
  仅在输出 GeoJSON 时再拆环或绕回 [-180, 180]。
* 细分得到的四个子框共享分割线，闭区间取并集恰好等于父框，
  不会为去重或容纳点而缓冲、扩大目标边界。

仅依赖 Python 标准库。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

# 地球平均半径（km），用于球面梯形面积
EARTH_RADIUS_KM = 6371.0088
# 经纬度浮点比较容差（度），仅用于吸收二进制浮点误差，不构成边界缓冲
COORD_EPS = 1e-9


@dataclass(frozen=True)
class Bounds:
    """展开坐标上的闭区间矩形 ``west <= lon <= east``、``south <= lat <= north``。"""

    west: float
    south: float
    east: float
    north: float

    @property
    def width(self) -> float:
        return self.east - self.west

    @property
    def height(self) -> float:
        return self.north - self.south

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


def parse_target(west, south, east, north) -> tuple[Bounds, list[float], bool]:
    """校验原始输入并展开为连续坐标框。

    返回 ``(展开框, 原始输入, 是否跨经线)``。纬度必须满足
    ``-90 <= south <= north <= 90``，经度分量必须在 [-180, 180]。
    """
    w, s, e, n = float(west), float(south), float(east), float(north)
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
        raise ValueError("经度必须位于 [-180, 180]")
    if not (-90.0 <= s <= n <= 90.0):
        raise ValueError("纬度必须满足 -90 <= south <= north <= 90")
    raw = [w, s, e, n]
    crossing = e < w - COORD_EPS
    frame = Bounds(w, s, e + 360.0, n) if crossing else Bounds(w, s, e, n)
    if frame.width < -COORD_EPS:
        raise ValueError("目标矩形宽度为负")
    return frame, raw, crossing


def normalize_lon(lon: float, frame: Bounds) -> float | None:
    """把接口返回的 [-180,180] 经度换算进目标展开坐标系。

    依次尝试原值、+360、-360，返回第一个落在目标框经度范围内的候选值；
    都不命中时返回 None（说明点不在目标框内）。
    180 与 -180 在跨经线框内会被规范成同一个展开坐标，不会产生伪冲突。
    """
    lon = float(lon)
    for candidate in (lon, lon + 360.0, lon - 360.0):
        if frame.west - COORD_EPS <= candidate <= frame.east + COORD_EPS:
            return candidate
    return None


def contains(b: Bounds, x: float, y: float) -> bool:
    """闭区间包含判断，带浮点容差。"""
    return (
        b.west - COORD_EPS <= x <= b.east + COORD_EPS
        and b.south - COORD_EPS <= y <= b.north + COORD_EPS
    )


def bounds_within(outer: Bounds, inner: Bounds) -> bool:
    """``inner`` 是否完整位于 ``outer`` 内（含边界）。"""
    return (
        outer.west - COORD_EPS <= inner.west
        and inner.east <= outer.east + COORD_EPS
        and outer.south - COORD_EPS <= inner.south
        and inner.north <= outer.north + COORD_EPS
    )


def split_quad(b: Bounds) -> tuple[Bounds, Bounds, Bounds, Bounds]:
    """沿经纬中线一分为四，返回 (西南, 东南, 西北, 东北)。

    子框闭区间的并集与父框完全相同：分割线上的点同时属于相邻子框，
    这是接口允许重复返回同一点的几何来源，去重交给采集引擎按 id 处理。
    """
    mid_lon = (b.west + b.east) / 2.0
    mid_lat = (b.south + b.north) / 2.0
    return (
        Bounds(b.west, b.south, mid_lon, mid_lat),
        Bounds(mid_lon, b.south, b.east, mid_lat),
        Bounds(b.west, mid_lat, mid_lon, b.north),
        Bounds(mid_lon, mid_lat, b.east, b.north),
    )


def cell_key(b: Bounds) -> str:
    """跨进程稳定的格框标识：浮点 repr 可精确往返，恢复运行时键不变。"""
    return json.dumps(b.as_list(), separators=(",", ":"))


def wire_parts(b: Bounds) -> list[Bounds]:
    """把展开坐标框翻译成接口可接受的、不跨经线的一个或两个请求框。

    跨 180 度经线时拆成共享 ±180 边的两个闭区间请求，接缝上的点
    可能在两个响应中各出现一次，由采集引擎按稳定 id 去重。
    """
    if b.east <= 180.0 + COORD_EPS:
        return [b]
    if b.west >= 180.0 - COORD_EPS:
        return [Bounds(b.west - 360.0, b.south, b.east - 360.0, b.north)]
    return [
        Bounds(b.west, b.south, 180.0, b.north),
        Bounds(-180.0, b.south, b.east - 360.0, b.north),
    ]


def wrap_lon(x: float) -> float:
    """展开经度绕回 [-180, 180]，恰好 180.0 保持 180.0。"""
    while x > 180.0:
        x -= 360.0
    while x < -180.0:
        x += 360.0
    return x


def _ring(w: float, s: float, e: float, n: float) -> list[list[float]]:
    return [
        [wrap_lon(w), s],
        [wrap_lon(e), s],
        [wrap_lon(e), n],
        [wrap_lon(w), n],
        [wrap_lon(w), s],
    ]


def cell_polygons(b: Bounds) -> list[list[list[float]]]:
    """输出 GeoJSON 外环列表；跨经线格框拆成东西两个环。"""
    if b.east <= 180.0 + COORD_EPS:
        return [_ring(b.west, b.south, b.east, b.north)]
    if b.west >= 180.0 - COORD_EPS:
        return [_ring(b.west - 360.0, b.south, b.east - 360.0, b.north)]
    return [
        _ring(b.west, b.south, 180.0, b.north),
        _ring(-180.0, b.south, b.east - 360.0, b.north),
    ]


def area_km2(b: Bounds) -> float:
    """球面梯形面积（km²）。展开宽度即真实经度差，跨经线框无需特殊处理。"""
    d_lon = math.radians(b.width)
    sin_delta = abs(
        math.sin(math.radians(b.north)) - math.sin(math.radians(b.south))
    )
    return EARTH_RADIUS_KM**2 * d_lon * sin_delta


def same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """展开坐标下的点位一致性判断，容差仅吸收浮点误差。"""
    return abs(a[0] - b[0]) <= COORD_EPS and abs(a[1] - b[1]) <= COORD_EPS
