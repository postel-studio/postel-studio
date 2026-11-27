#!/usr/bin/env python3
"""M2 全链路验收：一条命令复现 S10–S13（派活 → 候选 → 批准）。

用法（仓库根、已 `uv sync`）：

    uv run python scripts/accept_m2.py

十步：建临时仓库 → 绑定 → 建 Bot → 分配工作区 → 检测 Agent → 派活 → 等完成 →
候选出现 → 查 diff → 批准。每一步打印 `✓ <step> (<sec>s)`；成功最后一行
`M2 OK (10/10 steps)`；首个失败即 `M2 FAILED at <step>: <reason>` + 非零退出。

硬约束（S14 spec / 禁止事项）：

- 起**真内核**子进程（`postel-studio serve --port 0`），一条真 WS 连接上发真帧；
  不直接调 store/service，不 mock。
- 全程临时 `POSTEL_HOME` / `HOME` / 临时仓库，**只读快照**真实 opencode 凭据库到
  临时目录（D3）：源 `mode=ro` + `sqlite3.Connection.backup`，脚本从不读 key 值。
- 真 Agent 用 `deepseek/deepseek-flash`（免费模型额度已耗尽，见 U21/S13）。
- 没装 opencode / 没有 deepseek 凭据 → `SKIP M2: …` + exit 0，**绝不输出 `M2 OK`**。
- `finally` 里回收 worktree、停内核、删临时目录并验证无残留（D6）。

输出格式是契约（`tests/test_accept_m2.py` 解析）：`✓ ` / `M2 OK (` / `M2 FAILED at `
/ `SKIP M2:` / `清理：` / `工作目录：`。
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import httpx
import websockets

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT: Final = 30.0
EXIT_TIMEOUT: Final = 15.0
TASK_TIMEOUT: Final = 180.0  # 与 test_dispatch 的 TERMINAL_WAIT_S 同口径
MODEL: Final = "deepseek/deepseek-flash"
META_TICKET: Final = re.compile(r'<meta name="postel-handshake" content="([^"]+)"')
META_WS_PATH: Final = re.compile(r'<meta name="postel-ws-path" content="([^"]+)"')
STEP_NAMES: Final = (
    "建临时仓库",
    "绑定仓库",
    "建 Bot",
    "分配工作区",
    "检测 Agent",
    "派活",
    "等待完成",
    "候选出现",
    "查 diff",
    "批准",
)


class SkipM2Error(Exception):
    """前置条件缺失（不是失败）：明确 SKIP，绝不伪装成功。"""


class StepFailureError(Exception):
    """某一步失败：带上步骤名与原因，由 main 转成 `M2 FAILED at <step>`。"""

    def __init__(self, step: str, reason: object) -> None:
        self.step = step
        self.reason = reason
        super().__init__(str(reason))


def cli_binary() -> Path:
    """内核控制台入口（`uv run` 的 `.venv/bin`），回退仓库 `.venv/bin`。"""
    for candidate in (
        Path(sys.executable).with_name("postel-studio"),
        REPO_ROOT / ".venv" / "bin" / "postel-studio",
    ):
        if candidate.exists():
            return candidate
    raise SkipM2Error("找不到 postel-studio：先在仓库根跑 `uv sync`")


def git(*args: str, cwd: Path) -> None:
    """脚手架自己的 git（不是被测代码）：真 git，身份与默认分支显式给。"""
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
    """临时真仓库：main 上一个提交（Agent 要在它的 worktree 里建文件）。"""
    repo = root / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "--initial-branch=main", cwd=repo)
    (repo / "README.md").write_text("M2 acceptance\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def credential_snapshot(work: Path) -> Path:
    """把真实 opencode 凭据库只读快照到临时目录；返回 XDG_DATA_HOME。

    只读 `integration_id`/`active` 判断有没有付费凭据——**never read the key**.
    源库 `mode=ro` + `Connection.backup()` 保证一致性且不写真实文件（D3）。
    """
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "opencode"
    if not (real / "opencode.db").is_file():
        raise SkipM2Error("no deepseek credential configured (opencode data dir absent)")
    xdg_data = work / "opencode-data"
    dest_dir = xdg_data / "opencode"
    dest_dir.mkdir(parents=True)
    src = sqlite3.connect(f"file:{real / 'opencode.db'}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest_dir / "opencode.db"))
        try:
            src.backup(dst)
            row = dst.execute(
                "SELECT 1 FROM credential WHERE integration_id='deepseek' AND active=1 LIMIT 1"
            ).fetchone()
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise SkipM2Error(f"no deepseek credential configured (snapshot failed: {exc})") from exc
    finally:
        src.close()
    if row is None:
        raise SkipM2Error("no deepseek credential configured")
    return xdg_data


def start_kernel(home: Path, xdg_data: Path, log_path: Path) -> tuple[subprocess.Popen[str], str]:
    """起真内核子进程；env 让它把 opencode 数据指向临时快照并选 deepseek。"""
    env = dict(os.environ)
    env["POSTEL_HOME"] = str(home)
    env["HOME"] = str(home)  # 双保险：不落真实 ~/.postel
    env["XDG_DATA_HOME"] = str(xdg_data)  # opencode 只在这里读/写（快照）
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": MODEL})
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
    """SIGTERM 优雅停；15s 不退则 SIGKILL。"""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=EXIT_TIMEOUT)


def fetch_page(base: str) -> tuple[str, str]:
    """取 `/` 的 ticket / wsPath（从 meta 抠，不引 HTML 解析库）。"""
    response = httpx.get(f"{base}/", timeout=10.0)
    assert response.status_code == 200, f"GET / 回 {response.status_code}"
    ticket = META_TICKET.search(response.text)
    ws_path = META_WS_PATH.search(response.text)
    assert ticket is not None and ws_path is not None, "页面没有 handshake meta"
    return ticket.group(1), ws_path.group(1)


def decode(raw: str | bytes) -> dict[str, Any]:
    parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    assert isinstance(parsed, dict), f"帧不是 JSON 对象：{parsed!r}"
    return parsed


def result_data(frame: dict[str, Any]) -> Any:
    assert frame.get("ok") is True, f"帧失败：{frame.get('error')}"
    result = frame["result"]
    assert isinstance(result, dict), result
    return result["data"]


async def ten_steps(base: str, ws_path: str, ticket: str, repo: Path) -> None:
    """一条真 WS 连接上按顺序跑十步；首步失败即抛 `StepFailureError`。"""
    ws_url = f"ws://{base.removeprefix('http://')}{ws_path}?ticket={ticket}"
    seq = 0

    async with websockets.connect(ws_url) as ws:

        async def call(method: str, params: dict[str, Any] | None = None) -> Any:
            nonlocal seq
            seq += 1
            message: dict[str, Any] = {"type": "req", "id": f"m2-{seq}", "method": method}
            if params is not None:
                message["params"] = params
            await ws.send(json.dumps(message))
            return result_data(decode(await ws.recv()))

        @asynccontextmanager
        async def guard(index: int) -> AsyncIterator[None]:
            """任一步抛出的异常都带上步骤名（D2：失败指明具体步）。"""
            try:
                yield
            except StepFailureError:
                raise
            except Exception as exc:
                raise StepFailureError(STEP_NAMES[index], exc) from exc

        def step(index: int, detail: str) -> None:
            print(f"✓ {STEP_NAMES[index]} ({detail})", flush=True)

        # 1 建临时仓库（脚手架已完成；这里确认它仍是干净的真 git 仓库）
        async with guard(0):
            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert status.stdout.strip() == "", f"临时仓库不干净：{status.stdout}"
            step(0, repo.name)

        # 2 绑定仓库
        async with guard(1):
            data = await call("repo.bind", {"path": str(repo)})
            assert isinstance(data, dict) and isinstance(data.get("repoId"), str), data
            repo_id: str = data["repoId"]
            step(1, repo_id)

        # 3 建 Bot（默认 L1，能派活）
        async with guard(2):
            bot = await call("bot.create", {"repoId": repo_id, "name": "builder"})
            assert isinstance(bot, dict) and isinstance(bot.get("botId"), str), bot
            bot_id: str = bot["botId"]
            step(2, bot_id)

        # 4 分配工作区
        async with guard(3):
            wt = await call("worktree.acquire", {"repoId": repo_id, "botId": bot_id})
            assert isinstance(wt, dict) and isinstance(wt.get("path"), str) and wt["path"], wt
            step(3, wt["path"])

        # 5 检测 Agent（真握手）：先 agent.list 拿真实 kind，再 capabilities
        async with guard(4):
            agents = await call("agent.list")
            assert isinstance(agents, list) and agents, agents
            agent_kind = agents[0].get("agentKind")
            assert isinstance(agent_kind, str) and agent_kind, agents
            caps = await call("agent.capabilities", {"agentKind": agent_kind})
            assert isinstance(caps, dict) and caps.get("agentVersion"), caps
            step(4, f"Agent {caps['agentVersion']}")

        # 6 派活（明确要求建文件，候选才有 diff）
        async with guard(5):
            dispatched = await call(
                "task.dispatch",
                {"botId": bot_id, "prompt": "请在这个工作区里创建 hello.txt，内容写 M2 acceptance"},
            )
            assert isinstance(dispatched, dict) and isinstance(dispatched.get("taskId"), str), (
                dispatched
            )
            task_id: str = dispatched["taskId"]
            step(5, task_id)

        # 7 等终态
        async with guard(6):
            deadline = time.monotonic() + TASK_TIMEOUT
            state = ""
            task: dict[str, Any] = {}
            while time.monotonic() < deadline:
                task = await call("task.status", {"taskId": task_id})
                assert isinstance(task, dict), task
                state = str(task.get("state", ""))
                if state not in ("queued", "running"):
                    break
                await asyncio.sleep(0.2)
            assert state == "succeeded", f"任务终态 {state}（error={task.get('errorCode')}）"
            step(6, state)

        # 8 候选出现
        async with guard(7):
            candidates = await call("candidate.list", {"repoId": repo_id, "taskId": task_id})
            assert isinstance(candidates, list) and len(candidates) == 1, candidates
            candidate = candidates[0]
            assert isinstance(candidate, dict), candidate
            candidate_id = candidate.get("candidateId")
            base_sha, head_sha = candidate.get("baseSha"), candidate.get("headSha")
            assert isinstance(candidate_id, str) and candidate_id, candidate
            assert isinstance(base_sha, str) and isinstance(head_sha, str), candidate
            assert candidate.get("reviewState") == "local_only", candidate
            step(7, candidate_id)

        # 9 查 diff（真 repo.diff，断言非零）
        async with guard(8):
            diff = await call("repo.diff", {"repoId": repo_id, "base": base_sha, "head": head_sha})
            assert isinstance(diff, dict), diff
            stat = diff.get("stat")
            assert isinstance(stat, dict) and stat.get("filesChanged", 0) >= 1, diff
            assert isinstance(diff.get("patch"), str) and diff["patch"], "候选分支的 patch 为空"
            step(
                8,
                f"{stat['filesChanged']} 文件 / +{stat['insertions']} -{stat['deletions']}",
            )

        # 10 批准并复读
        async with guard(9):
            approved = await call("candidate.approve", {"candidateId": candidate_id})
            assert isinstance(approved, dict) and approved.get("reviewState") == "approved", (
                approved
            )
            after = await call("candidate.list", {"repoId": repo_id, "taskId": task_id})
            assert isinstance(after, list) and len(after) == 1, after
            assert after[0].get("reviewState") == "approved", after
            step(9, "approved")


def main() -> int:
    if shutil.which("opencode") is None:
        print("SKIP M2: opencode not installed", flush=True)
        return 0
    work = Path(tempfile.mkdtemp(prefix="postel-m2-"))
    home = work / "home"
    home.mkdir()
    proc: subprocess.Popen[str] | None = None
    clean_detail = ""
    print(f"工作目录：{work}", flush=True)
    try:
        repo = make_repo(work)
        xdg_data = credential_snapshot(work)
        proc, base = start_kernel(home, xdg_data, work / "kernel.log")
        ticket, ws_path = fetch_page(base)
        asyncio.run(ten_steps(base, ws_path, ticket, repo))
    except SkipM2Error as exc:
        print(f"SKIP M2: {exc}", flush=True)
        return 0
    except StepFailureError as exc:
        print(f"M2 FAILED at {exc.step}: {exc.reason}", flush=True)
        return 1
    except Exception as exc:
        print(f"M2 FAILED at 启动: {exc}", flush=True)
        return 1
    finally:
        # D6：先停内核，再删目录；失败也要记账（不静默）。
        if proc is not None:
            try:
                stop_kernel(proc)
            except Exception as exc:
                print(f"M2 FAILED at 清理: 内核没有在超时内退出：{exc}", flush=True)
        shutil.rmtree(work, ignore_errors=True)
        clean_detail = "已删除" if not work.exists() else "删除失败（残留）"
        print(f"清理：内核已停止，临时目录{clean_detail}：{work}", flush=True)

    if clean_detail != "已删除":
        print("M2 FAILED at 清理: 临时目录残留", flush=True)
        return 1
    print("M2 OK (10/10 steps)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
