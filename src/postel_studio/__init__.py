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
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=0, help="0 表示自动选择一个空闲端口")

    sub.add_parser("version", help="显示版本")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        # M1 才实现；此处显式失败而非静默成功
        print("serve 尚未实现（见 issues/I01-kernel-process.md）", file=sys.stderr)
        return 1

    if args.cmd == "version":
        print(__version__)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
