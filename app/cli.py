"""采集任务命令行入口。

示例
----
python3 -m app.cli run \
    --fixture fixtures/points.json \
    --bounds 100 35 102 37 --min-width 0.5 --budget 200 \
    --state-dir runtime/demo
"""

from __future__ import annotations

import argparse
import json

from app.client import DirectClient, HttpClient
from app.harvester import HarvestConfig, HarvestConfigError, Harvester


def _build_client(args):
    if args.http:
        return HttpClient(args.http)
    return DirectClient(path=args.fixture, cap=args.cap)


def main(argv=None):
    parser = argparse.ArgumentParser(description="草原观测点空间覆盖采集器")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="执行（或续采）一次采集任务")
    run_p.add_argument("--fixture", default="fixtures/points.json")
    run_p.add_argument("--cap", type=int, default=3)
    run_p.add_argument("--http", help="使用 HTTP 接口的 base URL，缺省走本地合成接口")
    run_p.add_argument(
        "--bounds",
        type=float,
        nargs=4,
        required=True,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="目标矩形；EAST < WEST 表示跨越 180 度经线",
    )
    run_p.add_argument("--min-width", type=float, required=True, help="最小格宽（度）")
    run_p.add_argument("--budget", type=int, required=True, help="接口请求数上限")
    run_p.add_argument("--state-dir", required=True)
    run_p.add_argument(
        "--reuse-evidence",
        help="参数变更时从该旧状态目录复用原始响应证据（完成标记不迁移）",
    )

    show_p = sub.add_parser("show", help="打印已有运行的覆盖清单摘要")
    show_p.add_argument("--state-dir", required=True)

    args = parser.parse_args(argv)

    if args.command == "show":
        with open(f"{args.state_dir}/manifest.json", encoding="utf-8") as fh:
            manifest = json.load(fh)
        print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
        return

    client = _build_client(args)
    endpoint_label = args.http or args.fixture
    config = HarvestConfig(
        target=args.bounds,
        min_width=args.min_width,
        request_budget=args.budget,
        cap=args.cap,
        endpoint=endpoint_label,
    )
    try:
        harvester = Harvester(config, client, args.state_dir)
    except HarvestConfigError as exc:
        raise SystemExit(f"配置错误：{exc}")

    if args.reuse_evidence:
        stats = harvester.import_evidence_from(args.reuse_evidence)
        print(f"旧证据复用：命中 {stats['reused']} 个查询框，"
              f"{stats['rejected']} 个不在当前目标范围（已登记为 outside_target）")

    manifest = harvester.run()
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    print(f"成果目录：{args.state_dir}")


if __name__ == "__main__":
    main()
