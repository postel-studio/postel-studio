#!/usr/bin/env python3
"""层依赖门禁：把 02-layers 的「只能向下 import」落成可执行检查（S4.5，结掉 U5）。

**为什么需要它**：`docs/codemap/02-layers.md §4` 早就写了一段「可执行检查」的代码片段，但它
一直只是**文档里的片段**——没有任何东西会跑它。规则不跑就会腐烂：S2 用函数内 import 绕过环
（T14）、S3 差点让 `repository` 反向 import `app`，都是靠 review 才拦下来的。这篇文档自己写着
「若层数继续变多，应把它固化进门禁」——层数已经 5 层、模块 14 个了。

与文档片段的三点差异：

1. **双向检查**：既查「向上/环 import」，也查「新模块有没有登记层」——不登记的模块等于
   没有规则（文档片段只查前者，新文件可以静默逃逸）。
2. **`import x` 与 `from x import y` 都查**（文档片段只看 `ImportFrom`）。
3. **违例带行号与原因**，且把「为什么这条边禁止」写进行输出（照着改的人不需要再翻文档）。

用法：

    python scripts/gate-layers.py              # 检查整个仓库
    python scripts/gate-layers.py --root DIR   # 检查指定根目录（测试用）

退出码：0 = 通过；1 = 有违例；2 = 用法错误 / 根目录不对。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# 每个源文件「不许 import 的包前缀」。**新增模块必须在这里登记**，否则门禁直接报未登记。
# 键是相对 `src/postel_studio/` 的路径；值是禁止的前缀（匹配模块名或 import 的名字）。
LAYERS: dict[str, tuple[str, ...]] = {
    # ---- L0：最底层，不许碰任何上层 ----
    "storage/paths.py": ("postel_studio.api", "postel_studio.web", "postel_studio.gitops"),
    "storage/db.py": ("postel_studio.api", "postel_studio.web", "postel_studio.gitops"),
    "domain/models.py": (
        "postel_studio.api",
        "postel_studio.web",
        "postel_studio.gitops",
        "postel_studio.storage",
    ),
    "gitops/repo.py": (
        "postel_studio.api",
        "postel_studio.web",
        "postel_studio.storage",
        "postel_studio.domain",
    ),
    # S7：worktree 的**写**操作。与 repo.py 同为 L0（起子进程、碰磁盘），但它是唯一允许
    # 改 `.git/worktrees/` 的模块，所以额外禁止碰 storage / domain / api / web。
    "gitops/worktrees.py": (
        "postel_studio.api",
        "postel_studio.web",
        "postel_studio.storage",
        "postel_studio.domain",
    ),
    # ---- L1：纯数据变换，不许 IO / 框架 ----
    "api/frames.py": (
        "postel_studio.api.app",
        "postel_studio.api.gateway",
        "postel_studio.api.server",
        "postel_studio.api.repository",
        "postel_studio.web",
        "postel_studio.storage",
        "postel_studio.gitops",
        "postel_studio.domain",
        "fastapi",
        "starlette",
        "uvicorn",
        "websockets",
        "subprocess",
        "sqlite3",
    ),
    # ---- L2：进程生命周期，唯一拥有 socket / 信号 ----
    "api/server.py": (
        "postel_studio.api.repository",
        "postel_studio.web",
        "postel_studio.storage.db",
    ),
    # ---- L3：路由表 / 网关 / 业务编排 ----
    "api/app.py": ("postel_studio.api.server",),  # 反向边会成环（server 已经 import app）
    "api/gateway.py": (
        "postel_studio.api.app",  # T14：网关不反向 import app，能力靠 Providers 注入
        "postel_studio.api.server",
        "postel_studio.api.repository",
        "postel_studio.web",
        "postel_studio.storage",
        "postel_studio.gitops",
        "postel_studio.domain",
    ),
    # S6：Bot 业务编排。只是 L3 的又一个 handler 模块：不许反向碰页面（D7）。
    "api/bots.py": ("postel_studio.web",),
    # S7：工作区业务编排。与 repository.py / bots.py 同为 L3 的 handler 模块：只能向下依赖
    # （storage / gitops / domain / frames），**不许 import 同层的其它 handler**——`02-layers §3`
    # 的唯一例外是 `app → gateway` 的装配，不是"handler 之间互相复用"。S7 想复用的两个判定
    # （baseRef 形状、仓库路径还能不能用）已经下沉到 `domain` / `gitops`，所以这里没有例外。
    "api/worktrees.py": (
        "postel_studio.api.app",
        "postel_studio.api.gateway",
        "postel_studio.api.server",
        "postel_studio.api.repository",
        "postel_studio.api.bots",
        "postel_studio.web",
        "fastapi",
        "starlette",
        "uvicorn",
    ),
    "api/repository.py": (
        "postel_studio.api.app",
        "postel_studio.api.gateway",
        "postel_studio.api.server",
        "postel_studio.web",
        "fastapi",
        "starlette",
        "uvicorn",
    ),
    # ---- L4：页面（被 app import，自己不许反向 import api）----
    "web/assets.py": ("postel_studio.api",),
    # ---- 包入口与包标记：登记为「不设 forbiddin 列表」，但必须被登记 ----
    "__init__.py": (),
    "storage/__init__.py": (),
    "domain/__init__.py": (),
    "gitops/__init__.py": (),
    "api/__init__.py": (),
    "web/__init__.py": (),
}


def imports_of(path: Path) -> list[tuple[int, str]]:
    """返回 `(行号, 被 import 的模块路径)`：**已展开** `from x import y` 这种写法。

    为什么必须展开：`from postel_studio.api import app` 的 `node.module` 只到
    `postel_studio.api`，真正越界的是被 import 的 **子模块 `app`**（T14 那条边的常见写法）。
    只比对 `node.module` 会漏掉它——文档片段里原来的 `any(f in a.name …)` 就是在补这一刀。

    已知的近似：`from X import Y` 之后再用 `Y.Z` 这种**跨行**越界抓不到（只展开一层）。
    这条不做，是因为要抓它就得做符号表分析，而层规则里真正要防的边都是一层内的直接 import。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
            out.extend(
                (node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
    return out


def violations_for(rel: str, path: Path, forbidden: tuple[str, ...]) -> list[str]:
    bad: list[str] = []
    for lineno, module in imports_of(path):
        for prefix in forbidden:
            if module == prefix or module.startswith(prefix + "."):
                bad.append(
                    f"{rel}:{lineno} → import {module}（禁止：{prefix}，层规则见 02-layers §2）"
                )
                break
    return bad


def run(root: Path) -> int:
    package = root / "src" / "postel_studio"
    if not package.is_dir():
        print(f"找不到包目录：{package}", file=sys.stderr)
        return 2
    problems: list[str] = []
    present = {
        str(p.relative_to(package)): p
        for p in sorted(package.rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    # 1) 新模块必须登记层（不登记 = 逃逸）
    for rel in sorted(present):
        if rel not in LAYERS:
            problems.append(f"{rel} → 未登记层依赖：在 gate-layers.py 的 LAYERS 里声明它的禁止项")
    # 2) 已登记的模块不许向上/成环
    for rel in sorted(present):
        forbidden = LAYERS.get(rel)
        if forbidden:
            problems.extend(violations_for(rel, present[rel], forbidden))
    for line in problems:
        print(f"FAIL {line}")
    if problems:
        print(f"\n层依赖门禁未通过：{len(problems)} 处违例", file=sys.stderr)
        return 1
    print(f"✅ 层依赖门禁通过（{len(present)} 个模块，{len(LAYERS)} 条登记）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="层依赖门禁：只允许向下 import，且新模块必须登记")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="仓库根目录（默认是本脚本的上一级）",
    )
    args = parser.parse_args(argv)
    return run(args.root)


if __name__ == "__main__":
    sys.exit(main())
