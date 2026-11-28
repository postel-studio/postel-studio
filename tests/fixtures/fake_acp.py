#!/usr/bin/env python3
"""假 ACP 服务端：**只用于异常路径与回收断言**，不是第二个实现。

真握手由 `test_real_handshake_*` 对本机 `opencode acp` 做（未安装则 skip）；这个脚本
负责真 Agent 很难稳定制造的那几种情况：噪声行、永不应答、缺字段、以及「答完还赖着不死」。

用法：`python fake_acp.py <mode> [--pidfile=PATH] [--envfile=PATH]`

| mode | 行为 |
|---|---|
| `ok`（缺省） | 打一行合法响应，然后**保持存活**（证明回收真的杀了它，而不是它自己退了） |
| `garbage` | 先打 banner / 非 JSON / 空行，再打合法响应 |
| `silent` | 读到请求后永不应答，也不退出（喂超时路径） |
| `no-version` | 回一个没有 `protocolVersion` 的 `result` |
| `v1` | 回 v1 风格的版本 `1.9.9`（S16 D6：顶层 `permissions` 不被识别） |
| `exit-early` | 读到请求后**一个字节都不写**就退出（喂「应答前关闭 stdout」路径） |

S16 的 `v1` 模式还会把收到的每个方法名追加到 `--logfile=PATH`；测试据此断言
`session/new` **从未被调用**（会话真的没启动，而不只是任务标成 failed）。
"""

from __future__ import annotations

import json
import os
import sys
import time

RESPONSE: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": 1,
        # 与 S9 规格 §8.1 的实测响应同形：既有布尔叶子，也有**空对象**
        "agentCapabilities": {
            "loadSession": True,
            "mcpCapabilities": {"http": True, "sse": True},
            "promptCapabilities": {"embeddedContext": True, "image": True},
            "sessionCapabilities": {"close": {}, "fork": {}, "list": {}, "resume": {}},
        },
        "agentInfo": {"name": "Fake Agent", "version": "9.9.9"},
    },
}

NO_VERSION: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"agentCapabilities": {}, "agentInfo": {"name": "Fake Agent"}},
}

V1_RESPONSE: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True},
        "agentInfo": {"name": "Fake Agent", "version": "1.9.9"},
    },
}


def _flag(name: str, argv: list[str]) -> str | None:
    prefix = f"--{name}="
    for arg in argv:
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "ok"
    pidfile = _flag("pidfile", argv[2:])
    envfile = _flag("envfile", argv[2:])
    logfile = _flag("logfile", argv[2:])
    if pidfile:
        with open(pidfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    if envfile:
        with open(envfile, "w", encoding="utf-8") as handle:
            json.dump(dict(os.environ), handle)

    # v1 模式：逐行记录请求的方法名，让测试能证明 session/new 根本没发出来。
    if mode == "v1":
        first = json.loads(sys.stdin.readline())
        with open(str(logfile), "w", encoding="utf-8") as handle:
            handle.write(str(first.get("method")) + "\n")
        response = dict(V1_RESPONSE)
        response["id"] = first.get("id")
        print(json.dumps(response), flush=True)
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            with open(str(logfile), "a", encoding="utf-8") as handle:
                handle.write(str(request.get("method")) + "\n")
            reply = dict(response)
            reply["id"] = request.get("id")
            print(json.dumps(reply), flush=True)
        return 0

    # 读一行请求（不解析：这个脚本只关心"什么时候回"）。
    sys.stdin.readline()

    if mode == "exit-early":
        return 0  # 一个字节都不写：父进程读到的第一件事就是 EOF，而不是超时

    if mode == "silent":
        while True:  # 永远不答，只能被杀
            time.sleep(0.05)

    if mode == "garbage":
        print("fake-acp 9.9.9 starting", flush=True)
        print("this is not json {{{", flush=True)
        print("", flush=True)

    payload = NO_VERSION if mode == "no-version" else RESPONSE
    print(json.dumps(payload), flush=True)

    # 答完**不退出**：如果被测实现忘了回收，父进程会一直握着这个 pid。
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
