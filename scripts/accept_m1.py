#!/usr/bin/env python3
"""M1 全链路验收：一条命令复现 S1–S6（`docs/design/issues/S6-bot-charter.md` D10 的契约）。

用法（仓库根、已 `uv sync`）：

    uv run python scripts/accept_m1.py

硬约束：

- 全程 `tempfile.mkdtemp()` 造独立工作目录，`POSTEL_HOME` 与 `HOME` 都指过去，**绝不碰真实
  `~/.postel`**；
- 起**真内核**子进程（`<venv>/bin/postel-studio serve --port 0`），从 `server.json` 取地址，
  从 `/` 页面抠 `ticket` / `wsPath`，用真 WebSocket 发帧；
- 建**真 git 仓库**：`main` 一个提交、`feature` 分支改一个文件（供 S5 用）；
- 无论成功失败都收掉内核进程并删临时目录（`try/finally`），不留残留（06-pitfalls T18）；
- 不依赖浏览器、不依赖 puppeteer（DOM 由 `tools/acceptance/*.mjs` 负责，本脚本只验协议 +
  页面静态标记）。

输出格式是契约（`tests/test_accept_m1.py` 会解析）：每步一行 `PASS S<n> <名字>`；失败打印
`FAIL S<n>: <原因>` 并**继续跑完剩余步骤**（一次跑出全部问题），最后一行
`M1 OK（6/6 步 + 家具）`，失败时 `M1 FAILED（<n> 处）`；退出码 0/1。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

import httpx
import websockets

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT: Final = 30.0
EXIT_TIMEOUT: Final = 15.0
META_TICKET: Final = re.compile(r'<meta name="postel-handshake" content="([^"]+)"')
META_WS_PATH: Final = re.compile(r'<meta name="postel-ws-path" content="([^"]+)"')
# D10 的「家具」：六刀各自的容器 id 都在页面里（S1/S3/S4/S5/S6 + 连接状态）。
FURNITURE: Final = (
    'id="bots"',
    'id="repos"',
    'id="branches"',
    'id="diff"',
    'id="charter"',
    'id="status"',
)
STEPS: Final = ("S1", "S2", "S3", "S4", "S5", "S6")
STEP_NAMES: Final = {
    "S1": "页面与存活",
    "S2": "连接与帧",
    "S3": "绑仓库",
    "S4": "分支",
    "S5": "diff",
    "S6": "Bot 与 charter",
    "家具": "",
}


class Runner:
    """打印 `PASS` / `FAIL` 并累计失败数；单步失败**不中断**后续步骤。"""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, step: str, detail: str = "") -> None:
        suffix = f"（{detail}）" if detail else ""
        label = f"PASS {step} {STEP_NAMES.get(step, step)}".rstrip()
        print(f"{label}{suffix}", flush=True)

    def fail(self, step: str, reason: object) -> None:
        self.failures += 1
        print(f"FAIL {step}: {reason}", flush=True)


def cli_binary() -> Path:
    """内核控制台入口：`sys.executable` 旁边的（`uv run` 的 `.venv/bin`），回退 `.venv/bin`。"""
    for candidate in (
        Path(sys.executable).with_name("postel-studio"),
        REPO_ROOT / ".venv" / "bin" / "postel-studio",
    ):
        if candidate.exists():
            return candidate
    raise SystemExit("找不到 postel-studio：先在仓库根跑 `uv sync`")


def start_kernel(home: Path, log_path: Path) -> tuple[subprocess.Popen[str], str]:
    """起真内核子进程，等它写出属于自己 pid 的 `server.json`，返回 `(proc, base_url)`。"""
    env = dict(os.environ)
    env["POSTEL_HOME"] = str(home)
    env["HOME"] = str(home)  # 双保险：环境变量没生效也落不到真实 ~/.postel
    env.pop("PYTHONPATH", None)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(cli_binary()), "serve", "--port", "0"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    info_path = home / "server.json"
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"内核启动失败（退出码 {proc.returncode}，日志见 {log_path}）")
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if isinstance(info, dict) and info.get("pid") == proc.pid:
            return proc, f"http://{info['host']}:{info['port']}"
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"内核 {BOOT_TIMEOUT}s 内没写 server.json（日志见 {log_path}）")


def stop_kernel(proc: subprocess.Popen[str]) -> None:
    """SIGTERM 优雅停；15s 不退则 SIGKILL（不把残留留给用户）。"""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=EXIT_TIMEOUT)


def git(*args: str, cwd: Path) -> None:
    """测试自己的脚手架（不是被测代码）：真 git，身份与初始分支显式给。"""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=acceptance@example.invalid",
            "-c",
            "user.name=acceptance",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {args} 失败：{result.stderr}"


def make_repo(root: Path) -> Path:
    """造 D10 要求的真仓库：main 一个提交 + feature 改一个文件（S5 的 diff 有内容可比）。"""
    repo = root / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "--initial-branch=main", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    (repo / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "f.txt").write_text("one\nTWO\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "feature", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return repo


def fetch_page(base: str) -> tuple[str, str, str]:
    """取 `/`，返回 `(page_html, ticket, wsPath)`（从 meta 里抠，不引 HTML 解析库）。"""
    response = httpx.get(f"{base}/", timeout=10.0)
    assert response.status_code == 200, f"GET / 回 {response.status_code}"
    ticket = META_TICKET.search(response.text)
    ws_path = META_WS_PATH.search(response.text)
    assert ticket is not None, "页面里没有 postel-handshake meta"
    assert ws_path is not None, "页面里没有 postel-ws-path meta"
    return response.text, ticket.group(1), ws_path.group(1)


def decode(raw: str | bytes) -> dict[str, Any]:
    """一帧 JSON → dict；不是对象就直接失败（别在很远的地方炸）。"""
    parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    assert isinstance(parsed, dict), f"帧不是 JSON 对象：{parsed!r}"
    return parsed


def result_data(frame: dict[str, Any]) -> Any:
    """取 `result.data`；失败帧直接断言失败（这本身就是断言）。"""
    assert frame.get("ok") is True, f"帧失败：{frame.get('error')}"
    result = frame["result"]
    assert isinstance(result, dict), result
    return result["data"]


def check_s1(runner: Runner, base: str) -> str:
    """**S1**：`GET /` 200 且页面含渲染后的版本号；`/api/health` 的 status/apiVersion 正确。"""
    page, _ticket, _ws_path = fetch_page(base)
    health = httpx.get(f"{base}/api/health", timeout=10.0)
    assert health.status_code == 200, f"GET /api/health 回 {health.status_code}"
    payload = health.json()
    assert payload.get("status") == "ok", payload
    assert payload.get("apiVersion") == "1", payload
    version = payload.get("version")
    assert isinstance(version, str) and version, f"health 里的 version 不合法：{version!r}"
    assert version in page, f"页面里没有渲染后的版本号 {version}"
    runner.ok("S1", f"version {version}")
    return page


async def ws_steps(runner: Runner, base: str, ws_path: str, ticket: str, repo: Path) -> None:
    """**S2–S6**：一条真 WS 连接上按顺序发帧。单步失败只记账，后续步骤继续尝试。"""
    ws_url = f"ws://{base.removeprefix('http://')}{ws_path}?ticket={ticket}"

    async with websockets.connect(ws_url) as ws:

        async def call(rid: str, method: str, params: dict[str, Any] | None = None) -> Any:
            message: dict[str, Any] = {"type": "req", "id": rid, "method": method}
            if params is not None:
                message["params"] = params
            await ws.send(json.dumps(message))
            return decode(await ws.recv())

        repo_id: str | None = None

        # S2：连接与帧（真 WS 往返）
        try:
            data = result_data(await call("m1", "system.health"))
            assert isinstance(data, dict) and data.get("status") == "ok", data
            runner.ok("S2", "system.health")
        except Exception as exc:
            runner.fail("S2", exc)

        # S3：绑真仓库 + repo.list 能找到它
        try:
            data = result_data(await call("m2", "repo.bind", {"path": str(repo)}))
            assert isinstance(data, dict), data
            repo_id = data.get("repoId")
            assert isinstance(repo_id, str) and repo_id, data
            listed = result_data(await call("m3", "repo.list"))
            assert isinstance(listed, list) and any(
                isinstance(row, dict) and row.get("repoId") == repo_id for row in listed
            ), listed
            runner.ok("S3", f"repoId {repo_id}")
        except Exception as exc:
            runner.fail("S3", exc)

        # S4：分支（至少 main 且 isCurrent）
        try:
            assert repo_id is not None, "依赖 S3 的 repoId"
            branches = result_data(await call("m4", "repo.branches", {"repoId": repo_id}))
            assert isinstance(branches, list), branches
            main = next(
                (row for row in branches if isinstance(row, dict) and row.get("name") == "main"),
                None,
            )
            assert main is not None, branches
            assert main.get("isCurrent") is True, main
            runner.ok("S4", f"{len(branches)} 个分支")
        except Exception as exc:
            runner.fail("S4", exc)

        # S5：三点 diff（stat / patch / truncated）
        try:
            assert repo_id is not None, "依赖 S3 的 repoId"
            data = result_data(
                await call(
                    "m5", "repo.diff", {"repoId": repo_id, "base": "main", "head": "feature"}
                )
            )
            assert isinstance(data, dict), data
            diff_stat = data.get("stat")
            assert isinstance(diff_stat, dict), data
            assert diff_stat.get("filesChanged", 0) >= 1, diff_stat
            patch = data.get("patch")
            assert isinstance(patch, str) and patch, "patch 为空"
            assert data.get("truncated") is False, data
            runner.ok("S5", f"{diff_stat.get('filesChanged')} 个文件 · patch {len(patch)} 字符")
        except Exception as exc:
            runner.fail("S5", exc)

        # S6：Bot 与 charter（S8 起：空仓库列出 []，创建后才有一个 Bot）
        try:
            assert repo_id is not None, "依赖 S3 的 repoId"
            empty = result_data(await call("m6", "bot.list", {"repoId": repo_id}))
            assert empty == [], f"S8 起 bot.list 不再播种，空仓库应回 []：{empty!r}"
            created = result_data(
                await call("m6b", "bot.create", {"repoId": repo_id, "name": "builder"})
            )
            assert isinstance(created, dict), created
            assert created.get("name") == "builder", created
            assert created.get("permission") == "L1", created  # 默认级别（05 §2.2）
            assert created.get("charterSummary") == "尚未填写职责", created
            bots = result_data(await call("m6c", "bot.list", {"repoId": repo_id}))
            assert isinstance(bots, list) and len(bots) == 1, bots
            bot = bots[0]
            assert isinstance(bot, dict), bot
            assert bot.get("botId") == created.get("botId"), bot
            bot_id = bot.get("botId")
            memory = Path(str(bot.get("memoryPath")))
            charter = result_data(await call("m7", "memory.charter.get", {"botId": bot_id}))
            assert isinstance(charter, dict), charter
            content = charter.get("content")
            rev = charter.get("rev")
            assert isinstance(content, str) and "# builder" in content, content
            assert isinstance(rev, int) and not isinstance(rev, bool) and rev > 0, rev
            assert memory.is_dir(), f"记忆目录不存在：{memory}"
            for name in ("charter.md", "memory", "sessions"):
                assert (memory / name).exists(), f"记忆目录缺 {name}：{memory}"
            assert not memory.resolve().is_relative_to(repo.resolve()), "记忆目录在仓库内！"
            assert stat.S_IMODE(memory.stat().st_mode) == 0o700, oct(memory.stat().st_mode)
            charter_file = memory / "charter.md"
            assert stat.S_IMODE(charter_file.stat().st_mode) == 0o600, oct(
                charter_file.stat().st_mode
            )
            runner.ok("S6", f"builder / rev {rev} / {memory}")
        except Exception as exc:
            runner.fail("S6", exc)


def check_furniture(runner: Runner, page: str) -> None:
    """**家具**：页面 HTML 含六刀各自的容器 id（D10 的最后一行）。"""
    try:
        missing = [fragment for fragment in FURNITURE if fragment not in page]
        assert not missing, f"页面缺少：{missing}"
        runner.ok("家具", f"{len(FURNITURE)}/{len(FURNITURE)}")
    except Exception as exc:
        runner.fail("家具", exc)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="postel-m1-"))
    home = work / "home"
    home.mkdir()
    # 本进程也指过去：即使某个依赖读缺省状态根，也不会碰真实 ~/.postel。
    os.environ["POSTEL_HOME"] = str(home)
    os.environ["HOME"] = str(home)
    log_path = work / "kernel.log"
    runner = Runner()
    proc: subprocess.Popen[str] | None = None
    print(f"工作目录：{work}", flush=True)
    try:
        repo = make_repo(work)
        proc, base = start_kernel(home, log_path)
        page = check_s1(runner, base)
        _page, ticket, ws_path = fetch_page(base)
        asyncio.run(ws_steps(runner, base, ws_path, ticket, repo))
        check_furniture(runner, page)
    except Exception as exc:
        runner.fail("S1", exc)
    finally:
        if proc is not None:
            try:
                stop_kernel(proc)
            except Exception as exc:
                runner.fail("清理", f"内核没有在超时内退出：{exc}")
        shutil.rmtree(work, ignore_errors=True)
        state = "已删除" if not work.exists() else "删除失败（残留）"
        print(f"清理：内核已停止，临时目录{state}：{work}", flush=True)

    if runner.failures == 0:
        print(f"M1 OK（{len(STEPS)}/{len(STEPS)} 步 + 家具）", flush=True)
        return 0
    print(f"M1 FAILED（{runner.failures} 处）", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
