"""包初始化。

`main()` 是 CLI 入口，接受 argv 以便测试直接调用而不必起子进程。
M1 起提供 `serve` 子命令启动本地后端服务。
"""

from __future__ import annotations

import argparse
import sys

__version__ = "0.1.2"


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog="postel-studio",
        description="以仓库为第一视图的 Agent 工作台",
    )
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="启动本地后端服务")
    serve_p.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1（不可改成 0.0.0.0）")
    serve_p.add_argument("--port", type=int, default=0, help="0 表示自动选择一个空闲端口")
    serve_p.add_argument("--force", action="store_true", help="锁里的内核仍存活时强制接管（慎用）")

    sub.add_parser("version", help="显示版本")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        from postel_studio.api.server import LOOPBACK, serve
        from postel_studio.storage.paths import KernelBootError

        # 绑定面是硬约束（05-security §1.1），命令行不给绕过的口子
        if args.host != LOOPBACK:
            print(f"HOST_NOT_ALLOWED: 只能绑定 {LOOPBACK}，收到 {args.host}", file=sys.stderr)
            return 2
        try:
            return serve(args.port, force=args.force)
        except KernelBootError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.cmd == "version":
        print(__version__)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
