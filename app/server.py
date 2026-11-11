"""本地 HTTP 封装：把合成 GridEndpoint 暴露为 GET /query 接口。

仅用于演示真实 HTTP 适配路径，人工构造坐标，不涉及任何真实点位。
启动：``python3 -m app.server --fixture fixtures/points.json --port 8765``
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler
from urllib import parse

from app.spatial import GridEndpoint


def make_handler(endpoint: GridEndpoint):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        def do_GET(self):
            parsed = parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"status": "ok"})
                return
            if parsed.path != "/query":
                self._send(404, {"error": "unknown route"})
                return
            qs = parse.parse_qs(parsed.query)
            try:
                west, south, east, north = (
                    float(qs[k][0]) for k in ("west", "south", "east", "north")
                )
                result = endpoint.query(west, south, east, north)
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": f"bad request: {exc}"})
                return
            self._send(200, result)

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地合成观测点查询服务")
    parser.add_argument("--fixture", default="fixtures/points.json")
    parser.add_argument("--cap", type=int, default=3)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    endpoint = GridEndpoint(path=args.fixture, cap=args.cap)
    handler = make_handler(endpoint)
    from http.server import HTTPServer

    server = HTTPServer((args.host, args.port), handler)
    print(f"合成接口已启动：http://{args.host}:{args.port}/query (cap={args.cap})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
