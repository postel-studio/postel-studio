"""S4.5 · 文档门禁（`scripts/gate-docs.py`）的**反向验证**。

为什么这个文件是必需的（06-pitfalls T23 的同一纪律）：门禁的价值不是「它会失败」，
而是「它在**该失败的那种错**上失败」。所以每条用例都：

1. 在临时目录里搭一棵**最小合法**的文档树（基线能过）；
2. 只注入**一类**缺陷；
3. 断言退出码是 1 **且** 输出里出现该类缺陷的判定词。

只断言「非零退出」会放过「因为 A 挂了所以 B 也挂了」的假通过（S3.5 T32 的教训）。
最后一条用例守「真实仓库现在是干净的」——否则门禁一加进来就是红的。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "gate-docs.py"

# 最小页面骨架：门禁靠 `id="…"` 判断「这是页面」而不是随便一个 py 文件
ASSETS = """INDEX_HTML = '''<section id="repos"><ul id="repo-list"></ul></section>'''

def script() -> str:
    return 'document.getElementById("repos")'
"""

MODULE_INDEX = "| L0 | [x](x.md) | `src/x.py` | 3 |\n"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """一棵**能通过**的最小文档树：两页 md（含链接 + 锚点 + 行号引用 + 行数表 + 页面 id）。"""
    root = tmp_path / "repo"
    _write(root / "src" / "x.py", "line1\nline2\nline3\n")
    _write(root / "src" / "postel_studio" / "web" / "assets.py", ASSETS)
    _write(root / "docs" / "codemap" / "03-modules" / "README.md", MODULE_INDEX)
    _write(root / "docs" / "codemap" / "03-modules" / "x.md", "# 模块页\n")
    _write(root / "docs" / "b.md", "# 目标页\n\n## 第一节\n\n内容\n")
    _write(
        root / "docs" / "a.md", "# 甲\n\n[去乙](b.md#第一节) · [精确行](b.md#L3) · [整文件](b.md)\n"
    )
    return root


def test_baseline_passes(tree: Path) -> None:
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "文档门禁通过" in result.stdout


def test_broken_relative_link_is_reported(tree: Path) -> None:
    """S3.5 真事故：`../../design/` 写成 `../design/`（少一级）→ 链接 404。"""
    _write(tree / "docs" / "a.md", "# 甲\n\n[断链](missing.md)\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "不存在的文件" in result.stdout, result.stdout
    assert "missing.md" in result.stdout


def test_missing_anchor_is_reported(tree: Path) -> None:
    _write(tree / "docs" / "a.md", "# 甲\n\n[错锚点](b.md#不存在的标题)\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "锚点" in result.stdout, result.stdout


def test_out_of_range_line_reference_is_reported(tree: Path) -> None:
    """`b.md` 只有 4 行，引用 `#L40` 必须变红。"""
    _write(tree / "docs" / "a.md", "# 甲\n\n[行号越界](b.md#L40)\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "行号越界" in result.stdout, result.stdout


def test_in_range_line_reference_passes(tree: Path) -> None:
    _write(tree / "docs" / "a.md", "# 甲\n\n[第 3 行](b.md#L3) · [2 到 4 行](b.md#L2-L4)\n")
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_module_line_count_is_reported(tree: Path) -> None:
    """S4 真事故：模块索引表的手工行数会过期。表里写 99，实际 3 行。"""
    _write(
        tree / "docs" / "codemap" / "03-modules" / "README.md",
        "| L0 | [x](x.md) | `src/x.py` | 99 |\n",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "行数过期" in result.stdout, result.stdout
    assert "99" in result.stdout


def test_js_referencing_unknown_id_is_reported(tree: Path) -> None:
    """页面改 id 不报错、只在浏览器里静默拿到 null——门禁必须拦住。"""
    _write(
        tree / "src" / "postel_studio" / "web" / "assets.py",
        ASSETS.replace('getElementById("repos")', 'getElementById("repos-typo")'),
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "JS 取不到这个 id" in result.stdout, result.stdout
    assert "repos-typo" in result.stdout


def test_css_hex_colour_is_not_treated_as_id(tree: Path) -> None:
    """假阳性守卫：`#d29922` 是颜色不是 id（S4 审计时我自己的脚本就在这里误报过）。"""
    _write(
        tree / "src" / "postel_studio" / "web" / "assets.py",
        ASSETS.replace(
            '<section id="repos">',
            '<section id="repos" style="color:#d29922">',
        ),
    )
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_inline_code_is_not_treated_as_a_link(tree: Path) -> None:
    """文档里常把「链接长什么样」连同行内代码写进例子：那**不是**链接，不许误报。

    误报比漏报更危险——被误报烦到的人会绕过门禁（S4.5 自测时就真踩过这一条）。
    """
    _write(tree / "docs" / "a.md", "# 甲\n\n写成 `[示例](missing.md)` 这样即可\n")
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fenced_code_block_is_not_scanned(tree: Path) -> None:
    """围栏代码块里的伪链接同样不算（命令示例、diff 片段里全是这种东西）。"""
    _write(
        tree / "docs" / "a.md",
        "# 甲\n\n```bash\ncurl [x](missing.md)\n```\n\n[真链接](b.md)\n",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_external_links_are_ignored(tree: Path) -> None:
    """网络不该进门禁：外链一律跳过（断网时门禁也必须能过）。"""
    _write(tree / "docs" / "a.md", "# 甲\n\n[外部](https://example.invalid/不存在)\n")
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_assets_file_is_reported(tree: Path) -> None:
    """页面资产消失必须报，而不是静默跳过这一项检查。"""
    (tree / "src" / "postel_studio" / "web" / "assets.py").unlink()
    result = _run(tree)
    assert result.returncode == 1
    assert "页面资产文件不存在" in result.stdout, result.stdout


def test_root_without_docs_is_usage_error(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "docs" in result.stderr


def test_real_repository_is_clean() -> None:
    """门禁加进 `check.sh` 的前提：**当前仓库本身**必须过（否则人人一开工就是红的）。"""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
