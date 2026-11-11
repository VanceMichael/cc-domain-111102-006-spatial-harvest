"""查询接口客户端抽象。

采集引擎只依赖 :class:`QueryClient` 的 ``query(west, south, east, north)``
协议：输入必须是不跨 180 度经线的有序闭区间，返回
``{"points": [...], "count": int, "truncated": bool}``。
跨经线拆分由 :mod:`app.geo` 负责，客户端本身不处理接缝。
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib import parse, request

from app.spatial import GridEndpoint


class QueryClient(Protocol):
    def query(self, west: float, south: float, east: float, north: float) -> dict[str, Any]:
        ...


class DirectClient:
    """进程内调用合成接口（演示与测试默认使用，无网络）。"""

    def __init__(self, path: str = "fixtures/points.json", cap: int = 3):
        self.endpoint = GridEndpoint(path=path, cap=cap)

    def query(self, west, south, east, north):
        return self.endpoint.query(west, south, east, north)


class HttpClient:
    """真实 HTTP 适配：GET {base}/query?wsent=...，响应为 JSON。

    仅使用标准库，超时与重试策略由调用方另行决定；本地演示不启用。
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, west, south, east, north):
        query = parse.urlencode(
            {"west": west, "south": south, "east": east, "north": north}
        )
        with request.urlopen(f"{self.base_url}/query?{query}", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class ReplayClient:
    """把 :class:`app.wire.Replay` 的录制响应适配成查询协议（离线、确定性）。"""

    def __init__(self, replay):
        self.replay = replay

    def query(self, west, south, east, north):
        url = (
            "https://endpoint.invalid/query?"
            + parse.urlencode(
                {"west": west, "south": south, "east": east, "north": north}
            )
        )
        reply = self.replay.get(url)
        if reply.status != 200:
            raise RuntimeError(f"重放响应异常：HTTP {reply.status}")
        return json.loads(reply.body.decode("utf-8"))
