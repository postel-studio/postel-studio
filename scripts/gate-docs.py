#!/usr/bin/env python3
"""文档门禁：相对链接、锚点、行号引用、模块行数表、页面 id 一致性。

**为什么需要它**（S4.5 引入）：这些错误**不会**让 ruff / mypy / pytest 变红，只能靠人眼发现，
而它们每一刀都在发生：

| 真实事故 | 后果 |
|---|---|
| S3.5：codemap 里 `../../design/` 应为 `../design/`（同一种错出现 2 次） | 链接点开是 404 |
| S3.5：`docs/design/issues/` 里少一级 `../` | 同上（S4 审计才扫出来） |
| S4：一次改 226 行 → 5 个模块页与能力表的 `:行号` 全漂 | 读者按行号翻到错的地方 |
| S4：`03-modules/README.md` 的行数表要手工维护 | 过期后"地图在撒谎" |
| 页面：HTML `id` 与 JS `getElementById("…")` 是字符串耦合 | 改 id 不报错，浏览器静默 `null` |

**刻意不检查的东西**（避免门禁变成伪精确）：

- 文字内容对不对（06-pitfalls U8：内容质量没有机械判据，靠 review）；
- 跨仓库外链是否可达（网络不该进门禁）。

用法：

    python scripts/gate-docs.py              # 检查整个仓库
    python scripts/gate-docs.py --root DIR   # 检查指定根目录（测试用）

退出码：0 = 通过；1 = 有问题（逐条打印 `FAIL <文件>:<行> → <原因>`）；2 = 用法错误 / 根目录不对。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 相对链接 / 锚点 / 行号
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
LINE_REF = re.compile(r"L(\d+)(?:-L(\d+))?")
# 模块索引表的行：| L0 | [gitops](gitops.md) | `src/postel_studio/gitops/repo.py` | 197 | … |
MODULE_ROW = re.compile(
    r"^\|\s*L\d\s*\|\s*\[[^\]]+\]\(([^)]+)\)\s*\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|"
)
# HTML 里的 id
HTML_ID = re.compile(r'id="([A-Za-z][\w-]*)"')
# 内联 JS 的 getElementById
JS_ID = re.compile(r'getElementById\("([\w-]+)"\)')
# CSS 的 #id 选择器（纯 hex 色值单独过滤）
CSS_ID = re.compile(r"(?<![\w&])#([A-Za-z][\w-]*)")

MODULE_INDEX = Path("docs/codemap/03-modules/README.md")
ASSETS = Path("src/postel_studio/web/assets.py")
ROOT_MARKDOWN = ("README.md", "README.zh.md", "CONTRIBUTING.md")
SKIP_PREFIXES = ("http://", "https://", "mailto:")


class Report:
    """收集 FAIL 并按文件排序打印（顺序稳定，diff 友好）。"""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.checked: dict[str, int] = {}

    def fail(self, where: str, reason: str) -> None:
        self.problems.append(f"FAIL {where} → {reason}")

    def count(self, kind: str) -> None:
        self.checked[kind] = self.checked.get(kind, 0) + 1

    def emit(self) -> int:
        for line in self.problems:
            print(line)
        if self.problems:
            print(f"\n文档门禁未通过：{len(self.problems)} 处问题", file=sys.stderr)
            return 1
        summary = " / ".join(f"{kind} {n}" for kind, n in sorted(self.checked.items()))
        print(f"✅ 文档门禁通过（{summary}）")
        return 0


def slug(text: str) -> str:
    """GitHub 风格的标题锚点：小写、去掉标点（保留 `-`/`_`/空格）、空格转 `-`。

    与 GitHub 的差异只可能在极端标点上；这里宁可用**更宽松**的算法（少报错），
    也不要把正确链接误判成坏的——门禁一旦误报就会被绕过。
    """
    lowered = text.strip().lower()
    return re.sub(r"[^\w\- ]", "", lowered).replace(" ", "-")


def headings_of(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if match:
            out.add(slug(match.group(2)))
    return out


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


FENCE = "```"
INLINE_CODE = re.compile(r"`[^`]*`")


def _without_code(text: str) -> list[tuple[int, str]]:
    """去掉围栏代码块与行内代码，保留行号。

    文档里常把「链接长什么样」当例子写进代码（`` `[…](…py#L24)` ``）。那些不是链接，
    当成链接会让门禁误报——而**误报的门禁会被绕过**，比没有门禁更糟。
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(FENCE):
            in_fence = not in_fence
            continue
        out.append((line_no, "" if in_fence else INLINE_CODE.sub("", line)))
    return out


def check_links(md: Path, report: Report) -> None:
    """一条 markdown 链接的三种目标：文件存在、锚点存在、行号在范围内。

    围栏代码块与行内代码里的内容**不算链接**（见 `_without_code`）。
    """
    text = md.read_text(encoding="utf-8")
    for line_no, line in _without_code(text):
        for target in LINK.findall(line):
            if target.startswith(SKIP_PREFIXES) or target.startswith("data:"):
                continue
            report.count("链接")
            where = f"{md}:{line_no}"
            path_part, _, fragment = target.partition("#")
            if path_part:
                dest = (md.parent / path_part).resolve()
                if not dest.exists():
                    report.fail(where, f"链接指向不存在的文件：{target}")
                    continue
            else:
                dest = md
            if not fragment:
                continue
            line_ref = LINE_REF.fullmatch(fragment)
            if line_ref is not None:
                if not dest.is_file():
                    report.fail(where, f"行号引用指向非文件：{target}")
                    continue
                total = line_count(dest)
                start = int(line_ref.group(1))
                end = int(line_ref.group(2)) if line_ref.group(2) else start
                if not (1 <= start <= end <= total):
                    report.fail(where, f"行号越界：{target}（该文件共 {total} 行）")
                continue
            if dest.suffix != ".md":
                continue
            if fragment not in headings_of(dest):
                report.fail(where, f"锚点在目标文件里不存在：{target}")


def check_module_table(root: Path, report: Report) -> None:
    """`03-modules/README.md` 的行数表必须与真实文件一致（手工维护的表最容易过期）。"""
    index = root / MODULE_INDEX
    if not index.is_file():
        report.fail(str(MODULE_INDEX), "模块索引文件不存在")
        return
    for line_no, line in enumerate(index.read_text(encoding="utf-8").splitlines(), start=1):
        match = MODULE_ROW.match(line)
        if match is None:
            continue
        report.count("行数表")
        source = root / match.group(2)
        claimed = int(match.group(3))
        if not source.is_file():
            report.fail(f"{MODULE_INDEX}:{line_no}", f"表里指向不存在的源文件：{match.group(2)}")
        elif line_count(source) != claimed:
            report.fail(
                f"{MODULE_INDEX}:{line_no}",
                f"行数过期：{match.group(2)} 表里写 {claimed}，实际 {line_count(source)}",
            )


def check_assets_ids(root: Path, report: Report) -> None:
    """HTML 的 id、内联 JS 的 `getElementById`、CSS 的 `#id` 选择器三者必须对得上。

    JS 取不到 id 时**不会**报错，只会拿到 null（S4 审计时正是靠这条检查发现 CSS 色值
    `#d29922` 被误当成 id 的假阳性，所以这里显式过滤纯 hex）。
    """
    path = root / ASSETS
    if not path.is_file():
        report.fail(str(ASSETS), "页面资产文件不存在")
        return
    text = path.read_text(encoding="utf-8")
    if '<section id="' not in text:  # 找不到页面骨架 = 这个检查的前提没了，别静默通过
        report.fail(str(ASSETS), "找不到页面 HTML 骨架（`id=` 属性），无法校验 id 一致性")
        return
    html_ids = set(HTML_ID.findall(text))
    js_ids = set(JS_ID.findall(text))
    css_ids = {i for i in CSS_ID.findall(text) if not re.fullmatch(r"[0-9a-fA-F]{3,8}", i)}
    for missing in sorted(js_ids - html_ids):
        report.fail(str(ASSETS), f"JS 取不到这个 id（HTML 里没有）：{missing}")
    for missing in sorted(css_ids - html_ids):
        report.fail(str(ASSETS), f"CSS 选择器指向不存在的 id：#{missing}")
    report.count("页面 id")
    if not html_ids:
        report.fail(str(ASSETS), "HTML 里一个 id 都没有，检查失效")


def markdown_files(root: Path) -> list[Path]:
    files = [root / name for name in ROOT_MARKDOWN if (root / name).is_file()]
    files.extend(sorted((root / "docs").rglob("*.md")))
    return files


def run(root: Path) -> int:
    report = Report()
    for md in markdown_files(root):
        report.count("md 文件")
        check_links(md, report)
    check_module_table(root, report)
    check_assets_ids(root, report)
    return report.emit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="文档门禁：链接 / 锚点 / 行号 / 行数表 / 页面 id")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="仓库根目录（默认是本脚本的上一级）",
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    if not (root / "docs").is_dir():
        print(f"根目录下找不到 docs/：{root}", file=sys.stderr)
        return 2
    return run(root)


if __name__ == "__main__":
    sys.exit(main())
